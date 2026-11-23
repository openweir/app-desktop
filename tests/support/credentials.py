"""Credential stores in memory, so the tests never touch the system's keychain, Credential
Manager or Secret Service."""

from __future__ import annotations

from keyring.errors import KeyringError

from openweir.models import credentials
from openweir.models.credentials import CredentialError, Keyring, ProviderTokens

# The real one, for the tests of it: tests/conftest.py puts refuse_system_keyring in its place
# for the whole session as it starts, before any test module is imported.
SYSTEM_KEYRING = credentials.system_keyring


def refuse_system_keyring() -> Keyring:
    """The system's credential store outside a test's keyring fixture, as for a worker a test
    left running that reads a credential after the test ended: never the real one. On Linux
    that one may wait forever for a prompt to unlock it, which keeps the test run from
    exiting."""
    raise KeyringError("the tests never use the system's credential store")


class MemoryKeyring:
    """A keyring backend in memory; tests/conftest.py makes it the system's."""

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.secrets.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        self.secrets[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        del self.secrets[(service, username)]


class MemoryCredentials:
    """A CredentialStore in memory; set ``fail`` to have every call fail as a locked
    keychain does."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise CredentialError("credentials_unavailable", "the keychain is locked")

    def read(self, key: str) -> str | None:
        self._check()
        return self.secrets.get(key)

    def write(self, key: str, secret: str) -> None:
        self._check()
        self.secrets[key] = secret

    def delete(self, key: str) -> None:
        self._check()
        self.secrets.pop(key, None)


class MemoryProviderTokens(ProviderTokens):
    """ProviderTokens in memory, stored as the system's store would keep them:
    ``credentials.secrets`` holds the JSON under ``provider:<id>``, and ``credentials.fail``
    locks the keychain."""

    def __init__(self) -> None:
        self.credentials = MemoryCredentials()
        super().__init__(self.credentials)
