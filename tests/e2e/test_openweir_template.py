"""api-edge's OpenWeir template (testdata/openweir-template.yaml) on the development service: a
provider account's subscription serves it from the signed-in config endpoint to the app's user
agent (D48), and the UI reads it through the real core as it stores the profile (docs/accounts.md
§5.4). src/core/internal/engine/openweir_template_test.go admits the same file, Naive included
in the native-tag builds; here it is the UI's side. The provider is the stand-in of
tests/support/provider_server.py, on 127.0.0.1."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from openweir.host.base import SourceInspection, SourceNode
from openweir.host.service import ServiceHost
from openweir.models.library import ProfileLibrary
from openweir.models.profiles import Profile, Usage
from openweir.models.provider import Session
from openweir.models.provider.api import HttpProviderApi
from openweir.models.routing import DEFAULT_INFO_PATTERN
from openweir.models.store import ProfileStore
from openweir.models.subscription import USER_AGENT, UrlFetcher
from scripts.dev import DevService
from scripts.fetch_geodata import seed
from tests.e2e.test_routing import on_service
from tests.support.credentials import MemoryProviderTokens
from tests.support.provider_server import EMAIL, GIB, NOW, PASSWORD, FakeProvider

pytestmark = pytest.mark.e2e

TEMPLATE = Path(__file__).parents[2] / "testdata" / "openweir-template.yaml"
# The template's placeholder nodes, one for every shape node.openweir writes, with the type and
# region the core gives them; its built-in routing's groups and rules.
NODES = (
    SourceNode("日本 01", "Vless", "JP", False),
    SourceNode("日本 02", "Vmess", "JP", False),
    SourceNode("日本 03", "Trojan", "JP", False),
    SourceNode("日本 04", "Vless", "JP", False),
    SourceNode("香港 01", "Shadowsocks", "HK", False),
    SourceNode("香港 02", "ShadowsocksR", "HK", False),
    SourceNode("香港 03", "Hysteria2", "HK", False),
    SourceNode("新加坡 01", "Tuic", "SG", False),
    SourceNode("新加坡 02", "AnyTLS", "SG", False),
    SourceNode("新加坡 03", "AnyTLS", "SG", False),
    SourceNode("美国 01", "Naive", "US", False),
    SourceNode("美国 02", "Naive", "US", False),
    SourceNode("美国 03", "Nowhere", "US", False),
    SourceNode("德国 01", "Mieru", "DE", False),
    SourceNode("德国 02", "Snell", "DE", False),
    SourceNode("德国 03", "Snell", "DE", False),
    SourceNode("英国 01", "Socks5", "GB", False),
    SourceNode("英国 02", "Http", "GB", False),
    SourceNode("英国 03", "Http", "GB", False),
)
GROUPS = 28
RULES = 69
# api-edge picks preset-openweir for a user agent that starts with OpenWeir/, whatever its case
# (src/features/proxy/subscription-presets.ts).
OPENWEIR_AGENT = re.compile(r"^openweir(/|$)", re.IGNORECASE)
NAIVE_LINE = re.compile(r'^  - \{"name":"([^"]+)",[^\n]*"type":"naive"[^\n]*\n', re.MULTILINE)


def without_naive(text: str) -> str:
    """The template without its Naive nodes and their group memberships: the development
    service is built without Naive (scripts/dev.py), so it refuses to run them, where the
    release builds have it."""
    for name in NAIVE_LINE.findall(text):
        text = NAIVE_LINE.sub("", text, count=1).replace(f',"{name}"', "")
        assert f'"{name}"' not in text
    return text


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    # Reach the loopback server directly even if the machine has a proxy configured.
    monkeypatch.setenv("no_proxy", "*")
    fake = FakeProvider()
    fake.config = without_naive(TEMPLATE.read_text()).encode()
    yield fake
    fake.close()


def test_a_providers_openweir_template_reads_whole(
    service: DevService, provider: FakeProvider, tmp_path: Path
) -> None:
    # The template's DNS reads GEOSITE categories: the core runs it on the GEO data the
    # installers ship.
    seed(service.home)
    store = ProfileStore(tmp_path / "profiles")

    async def scenario(host: ServiceHost) -> tuple[Profile, str, SourceInspection]:
        library = ProfileLibrary(
            store,
            host,
            UrlFetcher(),
            clock=lambda: NOW,
            provider_api=HttpProviderApi(language=lambda: "zh", clock=lambda: NOW),
            provider_tokens=MemoryProviderTokens(),
        )
        probe = await library.accounts.probe(provider.base)
        session = await library.accounts.sign_in_password(probe, EMAIL, PASSWORD)
        assert isinstance(session, Session)
        signed = await library.accounts.complete(probe, session)
        await library.accounts.sync(signed.provider.id)
        profile = next(p for p in library.snapshot().profiles if p.remote_id == 7)
        text = store.read_config(profile.id)
        # What the subscriptions page and the routing page read of the whole template, Naive
        # included, on the real core: reading needs no Naive.
        return profile, text, await host.inspect_source(TEMPLATE.read_text(), DEFAULT_INFO_PATTERN)

    try:
        profile, text, inspection = on_service(service, scenario)
    finally:
        store.close()

    (request,) = provider.requests_for("config")
    agent = request.header("user-agent") or ""
    assert agent == USER_AGENT
    assert OPENWEIR_AGENT.search(agent)
    assert text.encode() == provider.config
    # The answer's headers, as api-edge writes them.
    assert profile.usage == Usage(1 * GIB, 9 * GIB, 100 * GIB, 1802592000)
    assert profile.auto_update_hours == 24
    # The core ran it and counted every node it brings.
    naive = sum(node.type == "Naive" for node in NODES)
    assert profile.proxy_count == len(NODES) - naive
    assert profile.has_rules
    assert not profile.unread
    # Every node, the template's own routing whole, nothing left out.
    assert inspection.warnings == ()
    assert not inspection.template
    assert inspection.has_rules
    assert (inspection.group_count, inspection.rule_count) == (GROUPS, RULES)
    assert sorted(inspection.nodes, key=lambda node: node.name) == sorted(
        NODES, key=lambda node: node.name
    )
