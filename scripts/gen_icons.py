"""Generate OpenWeir's icons from the OpenWeir mark (DESIGN.md "Shapes").

    uv run python scripts/gen_icons.py

Writes the app icons (src/openweir/ui/brand/*.svg, package/icons/openweir.ico,
package/icons/openweir.icns and the Linux packages' hicolor theme icons in package/icons/hicolor/)
and the six tray SVGs in src/openweir/ui/tray/, all from the
OpenWeir mark (assets/openweir.svg) and the design repository's brand colours.
Everything is committed, so a build needs no renderer; run this after changing the
geometry, and check the result by eye.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

from openweir.product import LINUX_DESKTOP_ID

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "src" / "openweir" / "ui" / "brand"
TRAY = ROOT / "src" / "openweir" / "ui" / "tray"
PACKAGE = ROOT / "package" / "icons"
# The icon theme's directories under /usr/share/icons/hicolor, named after the .desktop file.
HICOLOR = PACKAGE / "hicolor"
HICOLOR_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)

# The OpenWeir mark from the assets repository (assets/openweir.svg), on its 100-unit view.
MARK = (
    "M15.56 54.92L15.56 34.01Q15.56 32.042 17.036 30.566L30.566 17.036Q32.042 15.56 34.01 15.56"
    "L65.99 15.56Q67.958 15.56 69.434 17.036L83.456 31.058Q85.178 32.78 82.718 32.78L62.054 32.78"
    "Q59.348 32.78 57.134 34.871L39.176 52.829Q37.085 54.92 34.256 54.92Z"
    "M84.44 45.08L84.44 65.99Q84.44 67.958 82.964 69.434L69.434 82.964Q67.958 84.44 65.99 84.44"
    "L34.01 84.44Q32.042 84.44 30.566 82.964L16.544 68.942Q14.822 67.22 17.282 67.22L37.946 67.22"
    "Q40.652 67.22 42.866 65.129L60.824 47.171Q62.915 45.08 65.744 45.08Z"
)
# The design repository's brand rules (ComponentsMore "品牌标"): the app icon is the mark in
# brand.onPrimary on a brand.fill rounded square; trays and menu bars take the mark in one colour.
TILE = "#00B3A4"  # brand.fill
ON_TILE = "#00201D"  # brand.onPrimary
MONO_LIGHT = "#0E0E0E"  # the one-colour mark on light ground
MONO_DARK = "#EDEDED"  # and on dark ground


def tile_svg(size: int, inset: float) -> str:
    """An app icon: the mark on a brand-coloured rounded square. ``inset`` is the transparent
    margin as a share of the canvas (macOS draws its icons on 824 of 1024 units)."""
    body = size * (1 - 2 * inset)
    x = size * inset
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<rect x="{x:g}" y="{x:g}" width="{body:g}" height="{body:g}" rx="{body * 0.22:g}" '
        f'fill="{TILE}"/>'
        f'<path transform="translate({x:g} {x:g}) scale({body / 100:g})" fill="{ON_TILE}" '
        f'd="{MARK}"/>'
        "</svg>\n"
    )


def tray_svg(color: str, opacity: float) -> str:
    """The mark in one colour for the tray and the menu bar, 32 px. The mark keeps the
    assets' margin, which is more than the eighth of the icon's width the rules ask for."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 100 100">\n'
        f'  <path fill="{color}" fill-opacity="{opacity:g}" d="{MARK}"/>\n'
        "</svg>\n"
    )


def render(svg: str, size: int) -> bytes:
    """PNG bytes of ``svg`` at ``size`` pixels, through Qt's SVG renderer."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage, QImageWriter, QPainter
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not QImageWriter(buffer, QByteArray(b"png")).write(image):
        raise RuntimeError(f"cannot encode a {size} px PNG")
    return bytes(buffer.data().data())


def ico(images: dict[int, bytes]) -> bytes:
    """A Windows icon holding one PNG per size (PNG entries work since Windows Vista)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    entries, data = b"", b""
    offset = len(header) + 16 * len(images)
    for size, png in sorted(images.items()):
        side = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


# The PNG entries of an .icns file by pixel size; a size names both its 1x and 2x slots.
ICNS = {
    16: [b"icp4"],
    32: [b"icp5", b"ic11"],
    64: [b"icp6", b"ic12"],
    128: [b"ic07"],
    256: [b"ic08", b"ic13"],
    512: [b"ic09", b"ic14"],
    1024: [b"ic10"],
}


def icns(images: dict[int, bytes]) -> bytes:
    """A macOS icon file of PNG entries, as iconutil writes them (macOS 10.7 and later), so the
    icons build on any system."""
    entries = b"".join(
        kind + struct.pack(">I", 8 + len(png)) + png
        for size, png in sorted(images.items())
        for kind in ICNS[size]
    )
    return b"icns" + struct.pack(">I", 8 + len(entries)) + entries


def hicolor_paths() -> dict[str, Path]:
    """Each icon the Linux packages install, by its path under the hicolor theme."""
    relatives = [f"{size}x{size}/apps/{LINUX_DESKTOP_ID}.png" for size in HICOLOR_SIZES]
    relatives.append(f"scalable/apps/{LINUX_DESKTOP_ID}.svg")
    return {relative: HICOLOR / relative for relative in relatives}


def main() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication(sys.argv[:1])
    BRAND.mkdir(parents=True, exist_ok=True)
    PACKAGE.mkdir(parents=True, exist_ok=True)

    # macOS: 824 of 1024 units, as Apple's icon grid; Windows and the window icon: nearly
    # full bleed, since Windows draws no shape of its own around an icon.
    macos = tile_svg(1024, 100 / 1024)
    windows = tile_svg(256, 8 / 256)
    (BRAND / "app-icon-macos.svg").write_text(macos)
    (BRAND / "app-icon.svg").write_text(windows)

    (PACKAGE / "openweir.ico").write_bytes(
        ico({s: render(windows, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)})
    )

    for variant, color in (
        ("template", "#000000"),
        ("light-taskbar", MONO_LIGHT),
        ("dark-taskbar", MONO_DARK),
    ):
        for state, opacity in (("on", 1.0), ("off", 0.45)):
            (TRAY / f"tray-{variant}-{state}.svg").write_text(tray_svg(color, opacity))

    (PACKAGE / "openweir.icns").write_bytes(icns({size: render(macos, size) for size in ICNS}))

    # Linux: GNOME's grid keeps a sixteenth of the canvas free around the shape, and docks
    # show the icon as it is drawn.
    linux = tile_svg(128, 8 / 128)
    for relative, path in hicolor_paths().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.startswith("scalable/"):
            path.write_text(linux)
        else:
            size = int(relative.split("x", 1)[0])
            path.write_bytes(render(linux, size))
    print("wrote", ", ".join(str(p.relative_to(ROOT)) for p in (BRAND, PACKAGE, TRAY)))


if __name__ == "__main__":
    main()
