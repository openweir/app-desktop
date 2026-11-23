"""product.json is the one source of the product identifiers (docs/decisions.md D49)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openweir import product
from scripts import gen_product


def test_the_go_identifiers_are_generated_from_product_json() -> None:
    expected = gen_product.render(json.loads(gen_product.SOURCE.read_text("utf-8")))
    assert gen_product.OUTPUT.read_text("utf-8") == expected


def test_the_package_reads_product_json() -> None:
    data = json.loads(product.SOURCE.read_text("utf-8"))
    assert data["displayName"] == product.DISPLAY_NAME
    assert data["bundleId"] == product.BUNDLE_ID
    assert f"{data['bundleId']}.tunnel" == product.TUNNEL_BUNDLE_ID
    assert tuple(data["urlSchemes"]) == product.URL_SCHEMES
    assert data["windows"]["serviceExecutable"] == product.SERVICE_EXECUTABLE
    assert product.PIPE_NAME.endswith(f"\\{data['productId']}")


def test_the_package_reads_the_linux_identifiers() -> None:
    linux = json.loads(product.SOURCE.read_text("utf-8"))["linux"]
    assert product.LINUX_INSTALL_DIR == linux["installDir"] == "/opt/openweir"
    assert product.LINUX_UI_PATH == "/opt/openweir/openweir-ui"
    assert product.LINUX_SERVICE_PATH == "/opt/openweir/openweir-service"
    assert product.LINUX_PACKAGE_FORMAT == "/opt/openweir/package-format"
    assert linux["unit"] == product.LINUX_UNIT
    assert linux["socket"] == product.LINUX_SOCKET
    assert linux["stateDir"] == product.LINUX_STATE_DIR
    assert linux["desktopId"] == product.LINUX_DESKTOP_ID == product.BUNDLE_ID
    assert linux["tunDevice"] == product.LINUX_TUN_DEVICE
    assert linux["routeTable"] == product.LINUX_ROUTE_TABLE
    assert linux["ruleIndex"] == product.LINUX_RULE_INDEX


def test_the_linux_tun_keeps_clear_of_other_tunnels() -> None:
    # The service's cleanup deletes every rule from the rule index to ten above it and flushes
    # the table (D67): none of them may be another tunnel's or the kernel's.
    rules = range(product.LINUX_RULE_INDEX, product.LINUX_RULE_INDEX + 11)
    for taken in (0, 220, *range(5210, 5271), *range(9000, 9011), 32765, 32766, 32767):
        assert taken not in rules
    assert rules.stop <= 32765
    assert product.LINUX_ROUTE_TABLE not in (0, 52, 100, 220, 253, 254, 255, 2022, 51820)
    # A Linux interface name has at most 15 bytes (IFNAMSIZ).
    assert 0 < len(product.LINUX_TUN_DEVICE.encode()) <= 15


def test_the_repository_leaves_signing_to_the_release_environment() -> None:
    signing = json.loads(product.SOURCE.read_text("utf-8"))["signing"]
    assert signing == {
        "appleTeamId": "",
        "windowsSigner": "",
        "windowsSignerEku": "",
        "linuxUpdateKey": "",
    }
    assert product.LINUX_UPDATE_KEY == ""


def test_a_build_bundles_the_release_signing_identities(tmp_path: Path) -> None:
    environ = {
        "OPENWEIR_APPLE_TEAM_ID": "ABCDE12345",
        "OPENWEIR_WINDOWS_SIGNER": "OpenWeir Ltd",
        "OPENWEIR_WINDOWS_SIGNER_EKU": " 1.3.6.1.4.1.311.97.1 ",
        "OPENWEIR_LINUX_UPDATE_KEY": "linux-update-key\n",
    }
    bundled = product.load(product.render(tmp_path, environ))
    assert bundled["signing"] == {
        "appleTeamId": "ABCDE12345",
        "windowsSigner": "OpenWeir Ltd",
        "windowsSignerEku": "1.3.6.1.4.1.311.97.1",
        "linuxUpdateKey": "linux-update-key",
    }
    assert bundled["bundleId"] == product.BUNDLE_ID
    product.require_signing(bundled, "appleTeamId", "windowsSigner", "windowsSignerEku")


def test_a_signed_build_without_identities_stops() -> None:
    with pytest.raises(RuntimeError, match="OPENWEIR_WINDOWS_SIGNER_EKU"):
        product.require_signing(product.load(product.SOURCE, {}), "windowsSignerEku")
