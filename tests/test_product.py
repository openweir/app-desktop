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


def test_the_repository_leaves_signing_to_the_release_environment() -> None:
    signing = json.loads(product.SOURCE.read_text("utf-8"))["signing"]
    assert signing == {"appleTeamId": "", "windowsSigner": "", "windowsSignerEku": ""}


def test_a_build_bundles_the_release_signing_identities(tmp_path: Path) -> None:
    environ = {
        "OPENWEIR_APPLE_TEAM_ID": "ABCDE12345",
        "OPENWEIR_WINDOWS_SIGNER": "OpenWeir Ltd",
        "OPENWEIR_WINDOWS_SIGNER_EKU": " 1.3.6.1.4.1.311.97.1 ",
    }
    bundled = product.load(product.render(tmp_path, environ))
    assert bundled["signing"] == {
        "appleTeamId": "ABCDE12345",
        "windowsSigner": "OpenWeir Ltd",
        "windowsSignerEku": "1.3.6.1.4.1.311.97.1",
    }
    assert bundled["bundleId"] == product.BUNDLE_ID
    product.require_signing(bundled, "appleTeamId", "windowsSigner", "windowsSignerEku")


def test_a_signed_build_without_identities_stops() -> None:
    with pytest.raises(RuntimeError, match="OPENWEIR_WINDOWS_SIGNER_EKU"):
        product.require_signing(product.load(product.SOURCE, {}), "windowsSignerEku")
