"""Entry point compiled by Nuitka in standalone mode.

Kept separate from ``openweir/__main__.py`` because Nuitka names its output after
the main script. Never build onefile: the Windows service only accepts the UI from its
install directory (docs/architecture.md §5.1).
"""

# Compiled translations; i18n.py looks for them beside the package (docs/ui.md §5).
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/../src/openweir/translations/*.qm=openweir/translations/
# The QML, tray icons and all fonts (Latin, Chinese, symbols and emoji) with their OFL.txt;
# fonts.py registers the fonts before app.py loads QML.
# nuitka-project: --include-data-dir={MAIN_DIRECTORY}/../src/openweir/ui=openweir/ui
# The license manifest the settings page lists, and the project's own license
# (docs/release.md §5, §6).
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/../src/openweir/licenses/third_party.json=openweir/licenses/
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/../src/openweir/licenses/THIRD_PARTY_NOTICES.txt=openweir/licenses/
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/../LICENSE=LICENSE
# openweir.__version__ reads the package's metadata; without it every release looks newer.
# nuitka-project: --include-distribution-metadata=openweir

from openweir.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
