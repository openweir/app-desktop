"""Signing a Linux release's SHA256SUMS (scripts/sign_sums.py, D62): the key formats, the
signature the UI and the service's install helper check, and the command line the release job
runs. Every key here is made for the test, or RFC 8032's published test vector."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from openweir import product
from openweir.update.signature import linux_verifier
from scripts import sign_sums

SUMS = (
    f"{'a' * 64}  OpenWeir-1.1.0-linux-x64.deb\n{'b' * 64}  OpenWeir-1.1.0-linux-x64.rpm\n"
).encode()
# RFC 8032 §7.1, TEST 2: the seed, its public key, the message 0x72 and the signature. The Go
# helper's tests check the same (update_linux_test.go).
RFC_SEED = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
RFC_PUBLIC = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
RFC_SIGNATURE = (
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)


@pytest.fixture
def keys() -> tuple[str, str]:
    """OPENWEIR_LINUX_UPDATE_KEY and OPENWEIR_LINUX_UPDATE_SIGNING_KEY, made afresh. cryptography
    comes with keyring's Secret Service backend, only on Linux; elsewhere these tests skip."""
    pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    return sign_sums.generate()


@pytest.fixture
def sums(tmp_path: Path) -> Path:
    path = tmp_path / "SHA256SUMS"
    path.write_bytes(SUMS)
    return path


def b64(hex_text: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_text)).decode()


def test_a_new_pair_is_two_keys_in_base64(keys: tuple[str, str]) -> None:
    public, private = keys
    for key in (public, private):
        assert len(key) == 44
        assert len(base64.b64decode(key, validate=True)) == 32
    assert public != private
    assert sign_sums.generate() != keys


def test_the_signature_is_what_the_ui_checks(sums: Path, keys: tuple[str, str]) -> None:
    public, private = keys
    path = sign_sums.sign(sums, private, public)
    assert path == sums.with_name("SHA256SUMS.sig")
    signature = path.read_bytes()
    assert len(signature) == 64  # as it is, no armour
    sign_sums.verify(sums, public)
    verifier = linux_verifier(public)
    assert verifier is not None
    verifier.verify_checksums(SUMS, signature)


def test_the_format_is_rfc_8032s(tmp_path: Path) -> None:
    pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    message = tmp_path / "SHA256SUMS"
    message.write_bytes(bytes.fromhex("72"))
    sign_sums.sign(message, b64(RFC_SEED), b64(RFC_PUBLIC))
    assert message.with_name("SHA256SUMS.sig").read_bytes().hex() == RFC_SIGNATURE
    verifier = linux_verifier(b64(RFC_PUBLIC))
    assert verifier is not None
    verifier.verify_checksums(bytes.fromhex("72"), bytes.fromhex(RFC_SIGNATURE))


def test_a_signature_of_something_else_does_not_verify(
    sums: Path, keys: tuple[str, str], tmp_path: Path
) -> None:
    public, private = keys
    signature = sign_sums.sign(sums, private)
    sums.write_bytes(SUMS + b"\n")
    with pytest.raises(sign_sums.SigningError, match="is not OPENWEIR_LINUX_UPDATE_KEY's"):
        sign_sums.verify(sums, public)
    sums.write_bytes(SUMS)
    other, _ = sign_sums.generate()
    with pytest.raises(sign_sums.SigningError, match="signature of"):
        sign_sums.verify(sums, other)
    armoured = tmp_path / "armoured.sig"
    armoured.write_bytes(base64.b64encode(signature.read_bytes()))
    with pytest.raises(sign_sums.SigningError, match="88 bytes"):
        sign_sums.verify(sums, public, armoured)


def test_the_signing_key_must_be_the_public_keys_pair(sums: Path, keys: tuple[str, str]) -> None:
    _, private = keys
    other, _ = sign_sums.generate()
    with pytest.raises(sign_sums.SigningError, match="is not the private key of"):
        sign_sums.sign(sums, private, other)
    assert not sums.with_name("SHA256SUMS.sig").exists()


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("not base64!", "not base64"),
        (base64.b64encode(bytes(31)).decode(), "31 bytes"),
        (base64.b64encode(bytes(64)).decode(), "64 bytes"),
    ],
)
def test_keys_are_32_bytes_in_base64(text: str, says: str) -> None:
    with pytest.raises(sign_sums.SigningError, match=says):
        sign_sums.decode_key(text, sign_sums.SIGNING_KEY_ENV)


def test_a_pasted_secrets_whitespace_does_not_count() -> None:
    raw = bytes(range(32))
    text = base64.b64encode(raw).decode()
    assert sign_sums.decode_key(f" {text}\n", sign_sums.SIGNING_KEY_ENV) == raw


def test_the_release_jobs_command_lines(
    sums: Path, keys: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    public, private = keys
    environ = {sign_sums.SIGNING_KEY_ENV: private + "\n", sign_sums.PUBLIC_KEY_ENV: public}
    assert sign_sums.main([str(sums)], environ) == 0
    assert sign_sums.main(["--verify", str(sums)], environ) == 0
    assert "good signature" in capsys.readouterr().out
    sums.write_bytes(SUMS.replace(b"a", b"c"))
    assert sign_sums.main(["--verify", str(sums)], environ) == 1
    assert "sign_sums:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "environ"),
    [
        (["SHA256SUMS"], {}),  # no signing key
        (["--verify", "SHA256SUMS"], {}),  # no public key
        (["missing/SHA256SUMS"], {sign_sums.SIGNING_KEY_ENV: base64.b64encode(bytes(32)).decode()}),
    ],
)
def test_the_command_line_says_what_is_missing(
    args: list[str], environ: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "SHA256SUMS").write_bytes(SUMS)
    assert sign_sums.main(args, environ) == 1


def test_generate_prints_both_variables(
    keys: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert sign_sums.main(["--generate"], {}) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [
        sign_sums.PUBLIC_KEY_ENV,
        sign_sums.SIGNING_KEY_ENV,
    ]
    public, private = (line.split("=", 1)[1] for line in lines)
    assert len(base64.b64decode(public)) == len(base64.b64decode(private)) == 32


def test_the_public_key_is_the_one_builds_bundle() -> None:
    assert product.SIGNING_ENVIRONMENT["linuxUpdateKey"] == sign_sums.PUBLIC_KEY_ENV
