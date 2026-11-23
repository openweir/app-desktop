"""The app's subscription user agent gets api-edge's OpenWeir template on every platform the app
runs on (D48, docs/accounts.md §3.2): api-edge tries its presets by priority, the OpenWeir one
(``^openweir(/|$)``, without case) before Clash's (``clash``), whatever the platform in
parentheses says. tests/e2e/test_openweir_template.py reads the template on the real core."""

from __future__ import annotations

import re

import pytest

from openweir.models.provider.api import USER_AGENT as API_USER_AGENT
from openweir.models.subscription import USER_AGENT

# The presets of api-edge's src/features/proxy/subscription-presets.ts that could match the
# app, in the order it tries them; none matching gets the URI list.
PRESETS = (
    ("preset-openweir", (r"^openweir(/|$)",)),
    ("preset-clash", ("clash", "mihomo", "stash")),
)


def picked(agent: str) -> str:
    for preset, patterns in PRESETS:
        if any(re.search(pattern, agent, re.IGNORECASE) for pattern in patterns):
            return preset
    return "preset-uri-list"


@pytest.mark.parametrize("platform", ["Windows", "macOS", "Linux"])
def test_every_platforms_agent_gets_the_openweir_template(platform: str) -> None:
    for agent in (USER_AGENT, API_USER_AGENT):
        other, count = re.subn(r"\((Windows|macOS|Linux)\)", f"({platform})", agent)
        assert count == 1, agent
        assert picked(other) == "preset-openweir", other
    # A user agent without the leading OpenWeir/<version> gets the Clash preset.
    assert picked("clash.meta") == "preset-clash"
