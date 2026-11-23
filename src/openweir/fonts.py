"""Application fonts and text/emoji fallbacks shared by both Qt platforms.

Register before creating QML objects: FontLoader alone does not override the platform's
fallback choices. The entire ui/fonts directory ships in both standalone packages.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QGuiApplication,
    QImage,
    QPainter,
    QRawFont,
)

DIRECTORY = Path(__file__).resolve().parent / "ui" / "fonts"
BUNDLED = (
    ("inter/Inter-Regular.ttf", "Inter"),
    ("inter/Inter-Medium.ttf", "Inter"),
    ("inter/Inter-SemiBold.ttf", "Inter"),
    ("jetbrains-mono/JetBrainsMono-Regular.ttf", "JetBrains Mono"),
    ("noto-sans-sc/NotoSansSC-VF.ttf", "Noto Sans SC"),
    ("noto-sans-symbols-2/NotoSansSymbols2-Regular.ttf", "Noto Sans Symbols 2"),
    # Keep the upstream hybrid font: macOS 26 needs its SVG color glyphs while
    # Windows and newer CoreText can use COLRv1. A COLRv1-only font can load and
    # shape correctly on older CoreText yet paint every emoji in monochrome.
    ("noto-color-emoji/NotoColorEmoji-Regular.ttf", "Noto Color Emoji"),
)
_REGISTERED = "_openweir_fonts_registered"
# Whether the bundled colour emoji font stayed registered (see colour_emoji_families).
EMOJI_BUNDLED = "_openweir_emoji_bundled"
_log = logging.getLogger(__name__)


def register_fonts() -> None:
    """Once per GUI application, including previews and tests with multiple QML engines.

    Missing assets are diagnosed and leave system fallback available; an installed font with
    the same name must not disguise a missing bundled asset in diagnostics.
    """
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        raise RuntimeError("register_fonts requires a QGuiApplication")
    if app.property(_REGISTERED):
        return

    loaded: dict[str, list[int]] = {}
    for relative, family in BUNDLED:
        path = DIRECTORY / relative
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0 or family not in QFontDatabase.applicationFontFamilies(font_id):
            _log.error("Cannot load bundled font %s from %s", family, path)
            if font_id >= 0:
                QFontDatabase.removeApplicationFont(font_id)
            continue
        loaded.setdefault(family, []).append(font_id)

    fallback = [name for name in ("Noto Sans SC", "Noto Sans Symbols 2") if name in loaded]
    # PySide does not expose QChar or QFontDatabase's script fallback methods. Family
    # substitutions supply this ordered fallback for every primary face the UI uses,
    # including the system face used if a Latin asset could not be registered.
    for family in ("Inter", "JetBrains Mono", app.font().family()):
        QFont.removeSubstitutions(family)
        QFont.insertSubstitutions(family, [name for name in fallback if name != family])
    emoji = loaded.get("Noto Color Emoji", [])
    QFontDatabase.setApplicationEmojiFontFamilies(colour_emoji_families(emoji))
    app.setProperty(
        EMOJI_BUNDLED, any(QFontDatabase.applicationFontFamilies(font_id) for font_id in emoji)
    )
    if "Inter" in loaded:
        font = app.font()
        font.setFamily("Inter")
        app.setFont(font)
    app.setProperty(_REGISTERED, True)


def bundled_family(family: str) -> str:
    """``family`` as Qt names the bundled file. Where the system has a font of the same name,
    as Linux often has Noto Color Emoji, fontconfig tells the two apart by foundry ("Noto Color
    Emoji [GOOG]") and either may answer the plain name, so the app names its own, as it gets
    on macOS and Windows. The 'head' table, with the file's dates and checksum, says which."""
    same = [name for name in QFontDatabase.families() if name.startswith(family + " [")]
    if not same:
        return family
    files = [relative for relative, name in BUNDLED if name == family]
    ours = {QRawFont(str(DIRECTORY / relative), 12).fontTable("head") for relative in files}
    for name in same:
        if QRawFont.fromFont(QFont(name)).fontTable("head") in ours:
            return name
    return family


def colour_emoji_families(bundled_ids: list[int]) -> list[str]:
    """The families emoji fall back to: the bundled Noto Color Emoji, unless this Qt cannot
    paint its colour glyphs. Not every Linux build of Qt paints the bundled font's COLRv1
    glyphs (on the x86_64 CI runner it drew nothing at all), while a system Noto Color Emoji
    in CBDT bitmaps still draws. Qt's emoji fallback goes by family name alone, so a bundled
    font it cannot paint is removed again, leaving the name to the system's font. macOS and
    Windows always paint the bundled font, so only Linux is asked."""
    if not bundled_ids:
        return []
    bundled = bundled_family("Noto Color Emoji")
    if not sys.platform.startswith("linux") or paints_in_colour(bundled):
        return [bundled]
    for font_id in bundled_ids:
        QFontDatabase.removeApplicationFont(font_id)
    _log.info("Qt cannot paint the bundled colour emoji; using a system colour emoji font")
    candidates = [name for name in QFontDatabase.families() if "emoji" in name.lower()]
    candidates.sort(key=lambda name: not name.startswith("Noto Color Emoji"))
    for family in candidates:
        if paints_in_colour(family):
            return [family]
    return []


def paints_in_colour(family: str) -> bool:
    """Whether Qt paints a coloured emoji with ``family``: a rainbow, drawn once off screen."""
    image = QImage(48, 48, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    font = QFont(family)
    font.setPixelSize(32)
    painter = QPainter(image)
    try:
        painter.setFont(font)
        painter.drawText(4, 38, "\U0001f308")
    finally:
        painter.end()
    coloured = 0
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            channels = (colour.red(), colour.green(), colour.blue())
            if colour.alpha() > 0 and max(channels) - min(channels) > 40:
                coloured += 1
                if coloured > 20:
                    return True
    return False
