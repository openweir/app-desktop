"""The credential store (decision D14), the WARP account in it (docs/exits.md §3.2) and the
provider accounts' sessions (docs/accounts.md §4.1)."""

import json
import logging
import sys

import pytest

from openweir.host.base import WarpAccount
from openweir.models import credentials
from openweir.models.credentials import (
    SERVICE,
    WARP_ACCOUNT,
    CredentialError,
    KeyringCredentials,
    ProviderToken,
    ProviderTokens,
    WarpAccounts,
)
from tests.support.credentials import (
    SYSTEM_KEYRING,
    MemoryCredentials,
    MemoryKeyring,
    MemoryProviderTokens,
)

ACCOUNT = WarpAccount(
    device_id="a1b2c3d4-0000-4000-8000-000000000000",
    token="token",
    protocol="masque-h2",
    private_key="private",
    peer_public_key="peer",
    endpoint="162.159.198.1:443",
    address_v4="172.16.0.2",
    address_v6="2606:4700:110:8a36::2",
    client_id="AAAA",
    registered_at_unix_ms=1_793_318_400_000,
)


def test_the_account_round_trips() -> None:
    store = MemoryCredentials()
    accounts = WarpAccounts(store)
    assert accounts.load() is None
    accounts.save(ACCOUNT)
    assert accounts.load() == ACCOUNT
    assert len(store.secrets[WARP_ACCOUNT].encode()) < 1280  # Windows keeps 2560 bytes
    accounts.delete()
    assert accounts.load() is None
    accounts.delete()  # nothing to delete


@pytest.mark.parametrize("text", ["not json", "{}", '{"device_id": 1}'])
def test_an_account_this_version_cannot_read_is_none(text: str) -> None:
    store = MemoryCredentials()
    store.secrets[WARP_ACCOUNT] = text
    assert WarpAccounts(store).load() is None


def test_an_unknown_protocol_is_none() -> None:
    store = MemoryCredentials()
    accounts = WarpAccounts(store)
    accounts.save(ACCOUNT)
    store.secrets[WARP_ACCOUNT] = store.secrets[WARP_ACCOUNT].replace("masque-h2", "quic")
    assert accounts.load() is None


def test_the_system_store_goes_through_keyring(keyring: MemoryKeyring) -> None:
    """tests/conftest.py puts a keyring in memory where the system's would be."""
    store = KeyringCredentials()
    store.write("k", "secret")
    assert keyring.secrets == {(SERVICE, "k"): "secret"}
    assert store.read("k") == "secret"
    store.delete("k")
    store.delete("k")
    assert store.read("k") is None


def test_keyring_failures_become_credential_errors(keyring: MemoryKeyring) -> None:
    from keyring.errors import KeyringLocked

    def locked(*_: object) -> None:
        raise KeyringLocked("the keychain is locked")

    keyring.set_password = locked  # type: ignore[method-assign]
    with pytest.raises(CredentialError) as raised:
        KeyringCredentials().write("k", "secret")
    assert raised.value.code == "credentials_unavailable"


# -- provider accounts ----------------------------------------------------------------------
SESSION = ProviderToken("bearer-secret-0123456789abcdef", "user-1")


def test_a_provider_session_round_trips_under_its_provider(keyring: MemoryKeyring) -> None:
    tokens = ProviderTokens(KeyringCredentials())
    assert tokens.load("p1") is None
    tokens.save("p1", SESSION)
    tokens.save("p2", ProviderToken("other", "user-9"))
    # Compact JSON under provider:<provider id> in the OpenWeir service (D56).
    stored = keyring.secrets[(SERVICE, "provider:p1")]
    assert stored == '{"token":"bearer-secret-0123456789abcdef","user_id":"user-1"}'
    assert len(stored.encode("utf-16-le")) < 2560  # what Windows keeps of a credential
    assert tokens.load("p1") == SESSION
    tokens.save("p1", ProviderToken("renewed", "user-1"))
    assert tokens.load("p1") == ProviderToken("renewed", "user-1")
    tokens.delete("p1")
    tokens.delete("p1")  # nothing to delete
    assert tokens.load("p1") is None
    assert tokens.load("p2") == ProviderToken("other", "user-9")


def test_a_provider_token_is_never_shown(caplog: pytest.LogCaptureFixture) -> None:
    tokens = MemoryProviderTokens()
    with caplog.at_level(logging.DEBUG):
        tokens.save("p1", SESSION)
        loaded = tokens.load("p1")
    assert SESSION.token not in repr(loaded)
    assert SESSION.token not in repr(SESSION)
    assert "user-1" in repr(SESSION)
    assert SESSION.token not in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "{}",
        '{"token": "t"}',
        '{"user_id": "u"}',
        '{"token": "", "user_id": "u"}',
        '{"token": "t", "user_id": ""}',
        '{"token": 1, "user_id": "u"}',
    ],
)
def test_a_session_this_version_cannot_read_is_none(text: str) -> None:
    tokens = MemoryProviderTokens()
    tokens.credentials.secrets["provider:p1"] = text
    assert tokens.load("p1") is None


def test_later_fields_of_a_session_are_ignored() -> None:
    tokens = MemoryProviderTokens()
    value = {"token": "t", "user_id": "u", "later": True}
    tokens.credentials.secrets["provider:p1"] = json.dumps(value)
    assert tokens.load("p1") == ProviderToken("t", "u")


@pytest.mark.parametrize("session", [ProviderToken("", "u"), ProviderToken("t", "")])
def test_an_empty_session_is_not_stored(session: ProviderToken) -> None:
    tokens = MemoryProviderTokens()
    with pytest.raises(ValueError, match="a token and a user id"):
        tokens.save("p1", session)
    assert tokens.credentials.secrets == {}


def test_a_locked_store_is_unavailable_not_signed_out() -> None:
    """A locked keychain is no missing token: the accounts wait instead of asking for a new
    sign-in (ProviderState.CREDENTIALS_UNAVAILABLE)."""
    tokens = MemoryProviderTokens()
    tokens.save("p1", SESSION)
    tokens.credentials.fail = True
    for call in (
        lambda: tokens.load("p1"),
        lambda: tokens.save("p1", SESSION),
        lambda: tokens.delete("p1"),
    ):
        with pytest.raises(CredentialError) as raised:
            call()
        assert raised.value.code == "credentials_unavailable"
    tokens.credentials.fail = False
    assert tokens.load("p1") == SESSION


def test_a_locked_system_keyring_is_unavailable(keyring: MemoryKeyring) -> None:
    from keyring.errors import KeyringLocked

    tokens = ProviderTokens(KeyringCredentials())
    tokens.save("p1", SESSION)

    def locked(*_: object) -> None:
        raise KeyringLocked("the keychain is locked")

    keyring.get_password = locked  # type: ignore[method-assign]
    with pytest.raises(CredentialError) as raised:
        tokens.load("p1")
    assert raised.value.code == "credentials_unavailable"
    assert SESSION.token not in str(raised.value)


# -- Linux: the Secret Service ---------------------------------------------------------------

# The real one; tests/conftest.py puts others in its place.
REAL_SYSTEM_KEYRING = SYSTEM_KEYRING


def test_linux_names_the_secret_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not discovered through entry points, which a frozen build may lack."""
    if sys.platform != "linux":
        pytest.skip("the Secret Service is Linux's")
    import keyring

    def discovered() -> None:
        raise AssertionError("looked for a backend through entry points")

    monkeypatch.setattr(keyring, "get_keyring", discovered)
    assert isinstance(REAL_SYSTEM_KEYRING(), credentials.GuardedKeyring)


def test_no_session_bus_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """keyring's own backend lets secretstorage's exception through; it never reaches D-Bus
    here."""
    if sys.platform != "linux":
        pytest.skip("the Secret Service is Linux's")
    import secretstorage
    from secretstorage.exceptions import SecretServiceNotAvailableException

    def no_bus() -> None:
        raise SecretServiceNotAvailableException("DBUS_SESSION_BUS_ADDRESS is unset")

    monkeypatch.setattr(secretstorage, "dbus_init", no_bus)
    monkeypatch.setattr(credentials, "system_keyring", credentials.secret_service)
    store = KeyringCredentials()
    for call in (lambda: store.read("k"), lambda: store.write("k", "secret")):
        with pytest.raises(CredentialError) as raised:
            call()
        assert raised.value.code == "credentials_unavailable"
        assert "DBUS_SESSION_BUS_ADDRESS" in raised.value.message


class FailingBackend:
    """A Secret Service backend whose every call fails with ``error``."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def get_password(self, service: str, username: str, /) -> str | None:
        raise self.error

    def set_password(self, service: str, username: str, password: str, /) -> None:
        raise self.error

    def delete_password(self, service: str, username: str, /) -> None:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("Socket closed at the other end"),  # the bus went away
        "dbus",  # a D-Bus error answer, such as a daemon that cannot start
        "locked",  # the user dismissed the prompt to unlock the collection
    ],
)
def test_secret_service_failures_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException | str
) -> None:
    if sys.platform != "linux":
        pytest.skip("the Secret Service is Linux's")
    from jeepney.low_level import Endianness, Header, HeaderFields, Message, MessageType
    from jeepney.wrappers import DBusErrorResponse
    from keyring.errors import KeyringLocked

    if error == "dbus":
        fields = {HeaderFields.error_name: "org.freedesktop.DBus.Error.ServiceUnknown"}
        header = Header(Endianness.little, MessageType.error, 0, 1, 0, 1, fields)
        error = DBusErrorResponse(Message(header, ("no daemon",)))
    elif error == "locked":
        error = KeyringLocked("Failed to unlock the collection!")
    assert isinstance(error, BaseException)
    failing = FailingBackend(error)
    monkeypatch.setattr(
        credentials, "system_keyring", lambda: credentials.secret_service(lambda: failing)
    )
    store = KeyringCredentials()
    for call in (
        lambda: store.read("k"),
        lambda: store.write("k", "hunter2"),
        lambda: store.delete("k"),
    ):
        with pytest.raises(CredentialError) as raised:
            call()
        assert raised.value.code == "credentials_unavailable"
        assert "hunter2" not in raised.value.message


def test_a_build_without_secretstorage_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "linux":
        pytest.skip("the Secret Service is Linux's")
    monkeypatch.setitem(sys.modules, "secretstorage.exceptions", None)
    monkeypatch.setattr(credentials, "system_keyring", credentials.secret_service)
    with pytest.raises(CredentialError) as raised:
        KeyringCredentials().read("k")
    assert raised.value.code == "credentials_unavailable"
    assert "secretstorage" in raised.value.message


def test_other_programming_errors_are_not_hidden() -> None:
    if sys.platform != "linux":
        pytest.skip("the Secret Service is Linux's")
    guarded = credentials.secret_service(lambda: FailingBackend(TypeError("a bug")))
    with pytest.raises(TypeError):
        guarded.get_password(SERVICE, "k")


def test_outside_a_test_the_system_store_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker a test left running that reads a credential once the test is over finds no
    store, rather than the system's: on Linux that may wait forever for an unlock prompt."""
    monkeypatch.undo()  # as after this test's keyring fixture is gone
    with pytest.raises(CredentialError) as raised:
        KeyringCredentials().read("k")
    assert raised.value.code == "credentials_unavailable"
