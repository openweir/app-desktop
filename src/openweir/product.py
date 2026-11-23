"""Product identifiers, read from product.json at the repository root (docs/decisions.md D1,
D49).

The packaged app carries its own copy, written by the build with the signing identities the
release environment sets (``render``); a development checkout reads the root file. The running
app never takes identities from its environment: they decide which signatures it trusts.
src/core/internal/product/product_gen.go is generated from the same file
(scripts/gen_product.py).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[2] / "product.json"
BUNDLED = Path(__file__).with_name("product.json")
FILE_NAME = "product.json"

# The release environment's signing identities, which replace the file's empty placeholders.
SIGNING_ENVIRONMENT = {
    "appleTeamId": "OPENWEIR_APPLE_TEAM_ID",
    "windowsSigner": "OPENWEIR_WINDOWS_SIGNER",
    "windowsSignerEku": "OPENWEIR_WINDOWS_SIGNER_EKU",
    "linuxUpdateKey": "OPENWEIR_LINUX_UPDATE_KEY",
}


def load(path: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The product data, with ``environ``'s signing identities applied when given (builds)."""
    data = json.loads((path or (BUNDLED if BUNDLED.is_file() else SOURCE)).read_text("utf-8"))
    if environ is not None:
        for key, name in SIGNING_ENVIRONMENT.items():
            value = environ.get(name, "").strip()
            if value:
                data["signing"][key] = value
    return data


def render(directory: Path, environ: Mapping[str, str] = os.environ) -> Path:
    """Write the copy a build bundles beside the package, and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FILE_NAME
    path.write_text(json.dumps(load(SOURCE, environ), indent=2) + "\n", encoding="utf-8")
    return path


def require_signing(data: Mapping[str, Any], *keys: str) -> None:
    """Stop a signed build that lacks an identity, rather than ship one that trusts nothing."""
    missing = [SIGNING_ENVIRONMENT[key] for key in keys if not data["signing"][key]]
    if missing:
        raise RuntimeError(f"a signed build needs {', '.join(missing)} in the environment")


_DATA = load()

DISPLAY_NAME: str = _DATA["displayName"]  # also names the user data directory
PRODUCT_ID: str = _DATA["productId"]  # the update check's User-Agent, the instance socket
BUNDLE_ID: str = _DATA["bundleId"]
TUNNEL_BUNDLE_ID = f"{BUNDLE_ID}.tunnel"
REPOSITORY: str = _DATA["repository"]
URL_SCHEMES: tuple[str, ...] = tuple(_DATA["urlSchemes"])

SERVICE_NAME: str = _DATA["windows"]["serviceName"]
UI_EXECUTABLE: str = _DATA["windows"]["uiExecutable"]
SERVICE_EXECUTABLE: str = _DATA["windows"]["serviceExecutable"]
PIPE_NAME = rf"\\.\pipe\ProtectedPrefix\Administrators\{PRODUCT_ID}"

# The Linux package (docs/architecture.md "Linux (in progress)", D60 to D67): installed to
# LINUX_INSTALL_DIR, its service runs as the systemd units <LINUX_UNIT>.service and .socket.
_LINUX: dict[str, Any] = _DATA["linux"]
LINUX_INSTALL_DIR: str = _LINUX["installDir"]
LINUX_UI_EXECUTABLE: str = _LINUX["uiExecutable"]
LINUX_SERVICE_EXECUTABLE: str = _LINUX["serviceExecutable"]
LINUX_UI_PATH = f"{LINUX_INSTALL_DIR}/{LINUX_UI_EXECUTABLE}"
LINUX_SERVICE_PATH = f"{LINUX_INSTALL_DIR}/{LINUX_SERVICE_EXECUTABLE}"
LINUX_UNIT: str = _LINUX["unit"]
LINUX_SOCKET: str = _LINUX["socket"]  # the service's control socket
LINUX_STATE_DIR: str = _LINUX["stateDir"]
LINUX_DESKTOP_ID: str = _LINUX["desktopId"]  # the .desktop file's name, without .desktop
LINUX_TUN_DEVICE: str = _LINUX["tunDevice"]
LINUX_ROUTE_TABLE: int = _LINUX["routeTable"]
LINUX_RULE_INDEX: int = _LINUX["ruleIndex"]
# The package writes its format, deb or rpm, here; the updater downloads the same format.
LINUX_PACKAGE_FORMAT = f"{LINUX_INSTALL_DIR}/package-format"

# Empty until OpenWeir has its Developer ID team and Artifact Signing profile; then the release
# environment sets them (docs/decisions.md D47).
APPLE_TEAM_ID: str = _DATA["signing"]["appleTeamId"]
WINDOWS_SIGNER: str = _DATA["signing"]["windowsSigner"]
WINDOWS_SIGNER_EKU: str = _DATA["signing"]["windowsSignerEku"]
# The Ed25519 public key that checks a Linux release's SHA256SUMS.sig (D62); without it the
# updater offers no installer.
LINUX_UPDATE_KEY: str = _DATA["signing"]["linuxUpdateKey"]

PROTOCOL_VERSION = 1
FRAME_MAGIC = b"WEIR"
