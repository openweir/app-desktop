"""Sign a Linux release's SHA256SUMS with the update key, check a signature, or make a new key
pair (docs/decisions.md D62).

    uv run python scripts/sign_sums.py SHA256SUMS            # write SHA256SUMS.sig
    uv run python scripts/sign_sums.py --verify SHA256SUMS   # check SHA256SUMS.sig
    uv run python scripts/sign_sums.py --generate            # print a new key pair

The signature is Ed25519 (RFC 8032) over SHA256SUMS's bytes exactly, and SHA256SUMS.sig holds
its 64 bytes as they are, without armour, so OpenSSL can check it too (``openssl pkeyutl
-verify -pubin -inkey <PEM> -rawin -in SHA256SUMS -sigfile SHA256SUMS.sig``). The UI checks it
before it trusts SHA256SUMS (src/openweir/update/signature.py), and so does the service's
install helper as root (src/core/service/update_linux.go).

The keys are environment variables of the release environment, both in standard base64 with
padding, 44 characters:

- OPENWEIR_LINUX_UPDATE_SIGNING_KEY, a secret: the private key's 32-byte seed (RFC 8032's
  private key; cryptography's ``private_bytes_raw``, Go's ``ed25519.PrivateKey.Seed``).
- OPENWEIR_LINUX_UPDATE_KEY, a variable: the public key's 32 bytes. The builds write it into
  the bundled product.json's ``signing.linuxUpdateKey`` (product.render) and link it into the
  service as ``product.LinuxUpdateKey`` with -X; a build without it offers no installer.

Signing refuses a signing key whose public half is not OPENWEIR_LINUX_UPDATE_KEY, when that is
set, so that a release never carries a signature its own builds refuse. --generate prints both
lines for a new pair; run it on a machine you trust, put the values into the environment and
keep no other copy of the private one. cryptography comes with keyring's Secret Service
backend, which ``uv sync`` installs only on Linux, so this runs on Linux.

The release job, on a Linux runner, once SHA256SUMS lists every other asset:

    uv run --frozen python scripts/sign_sums.py dist/SHA256SUMS
    uv run --frozen python scripts/sign_sums.py --verify dist/SHA256SUMS

with OPENWEIR_LINUX_UPDATE_SIGNING_KEY from the secret and OPENWEIR_LINUX_UPDATE_KEY from the
variable, then uploads dist/SHA256SUMS.sig with the rest. SHA256SUMS does not list
SHA256SUMS.sig, and nothing may change SHA256SUMS after it is signed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import sys
from pathlib import Path
from typing import Any

SIGNING_KEY_ENV = "OPENWEIR_LINUX_UPDATE_SIGNING_KEY"
PUBLIC_KEY_ENV = "OPENWEIR_LINUX_UPDATE_KEY"
KEY_BYTES = 32
SIGNATURE_BYTES = 64
SIGNATURE_SUFFIX = ".sig"


class SigningError(Exception):
    pass


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import (  # pyright: ignore[reportMissingImports]
            ed25519,
        )
    except ImportError as error:
        raise SigningError(
            "cryptography is not installed: run this on Linux after uv sync"
        ) from error
    return ed25519


def decode_key(text: str, name: str) -> bytes:
    """A key's 32 bytes from its base64; surrounding whitespace, as a pasted secret may have,
    does not count."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except binascii.Error as error:
        raise SigningError(f"{name} is not base64: {error}") from error
    if len(raw) != KEY_BYTES:
        raise SigningError(f"{name} has {len(raw)} bytes, not an Ed25519 key's {KEY_BYTES}")
    return raw


def encode_key(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def signature_path(sums: Path) -> Path:
    return sums.with_name(sums.name + SIGNATURE_SUFFIX)


def sign(sums: Path, signing_key: str, public_key: str = "") -> Path:
    """Write SHA256SUMS.sig beside ``sums`` and return its path. With ``public_key``, the
    signing key must be its pair."""
    private = _ed25519().Ed25519PrivateKey.from_private_bytes(
        decode_key(signing_key, SIGNING_KEY_ENV)
    )
    if public_key.strip():
        expected = decode_key(public_key, PUBLIC_KEY_ENV)
        if private.public_key().public_bytes_raw() != expected:
            raise SigningError(f"{SIGNING_KEY_ENV} is not the private key of {PUBLIC_KEY_ENV}")
    path = signature_path(sums)
    path.write_bytes(private.sign(sums.read_bytes()))
    return path


def verify(sums: Path, public_key: str, signature: Path | None = None) -> None:
    """Return when ``signature``, SHA256SUMS.sig beside ``sums`` by default, is the key's
    signature of ``sums``."""
    ed25519 = _ed25519()
    from cryptography.exceptions import (  # pyright: ignore[reportMissingImports]
        InvalidSignature,
    )

    key = ed25519.Ed25519PublicKey.from_public_bytes(decode_key(public_key, PUBLIC_KEY_ENV))
    path = signature or signature_path(sums)
    data = path.read_bytes()
    if len(data) != SIGNATURE_BYTES:
        raise SigningError(f"{path} has {len(data)} bytes, not an Ed25519 signature's 64")
    try:
        key.verify(data, sums.read_bytes())
    except InvalidSignature as error:
        raise SigningError(f"{path} is not {PUBLIC_KEY_ENV}'s signature of {sums}") from error


def generate() -> tuple[str, str]:
    """A new key pair: OPENWEIR_LINUX_UPDATE_KEY and OPENWEIR_LINUX_UPDATE_SIGNING_KEY."""
    private = _ed25519().Ed25519PrivateKey.generate()
    return encode_key(private.public_key().public_bytes_raw()), encode_key(
        private.private_bytes_raw()
    )


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ) if environ is None else environ
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n\n")[0])
    parser.add_argument("sums", nargs="?", type=Path, help="the release's SHA256SUMS")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help=f"check it with {PUBLIC_KEY_ENV}")
    mode.add_argument("--generate", action="store_true", help="print a new key pair")
    args = parser.parse_args(argv)
    try:
        if args.generate:
            if args.sums is not None:
                parser.error("--generate takes no SHA256SUMS")
            public, private = generate()
            print(f"{PUBLIC_KEY_ENV}={public}")
            print(f"{SIGNING_KEY_ENV}={private}")
            return 0
        if args.sums is None:
            parser.error("SHA256SUMS is required")
        if args.verify:
            public = environ.get(PUBLIC_KEY_ENV, "")
            if not public.strip():
                raise SigningError(f"{PUBLIC_KEY_ENV} is not set")
            verify(args.sums, public)
            print(f"{signature_path(args.sums)}: good signature by {PUBLIC_KEY_ENV}")
            return 0
        signing = environ.get(SIGNING_KEY_ENV, "")
        if not signing.strip():
            raise SigningError(f"{SIGNING_KEY_ENV} is not set")
        written = sign(args.sums, signing, environ.get(PUBLIC_KEY_ENV, ""))
        print(f"wrote {written}")
        return 0
    except (SigningError, OSError) as error:
        print(f"sign_sums: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
