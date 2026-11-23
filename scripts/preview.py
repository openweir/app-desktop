"""Run the UI against the simulated host and sample profiles.

    uv run python scripts/preview.py             # the system's language
    uv run python scripts/preview.py --lang en   # or zh; OPENWEIR_LANG works too
    uv run python scripts/preview.py --appearance dark  # or light, system;
                                                        # OPENWEIR_APPEARANCE works too
    uv run python scripts/preview.py --crowd 2000  # a busy core: 2000 more connections
    uv run python scripts/preview.py --development  # the development service: neither TUN
                                                    # nor the system proxy
    uv run python scripts/preview.py --takeover settings  # the Windows service's switches;
                                                          # tun is the macOS extension's
    uv run python scripts/preview.py --plan  # route with my plan, the default template
    uv run python scripts/preview.py --update  # a newer release on a fake GitHub
    uv run python scripts/preview.py --google  # Google Drive backup to a fake Google
    uv run python scripts/preview.py --long-names  # long names in Chinese, English and emoji,
                                                   # and a plan of them that --plan routes with
    uv run python scripts/preview.py --provider  # signed in to a fake provider: the 账户 page

Profiles live in a temporary directory and links resolve to sample data, so the preview
touches neither the real profiles nor the network; updates come from a fake GitHub on a
loopback port, whose installer passes a fake signature check. With --google, Google Drive is a
fake Google on a loopback port too, whose browser consents at once, and its sign-in stays in
memory. Without it, OPENWEIR_GOOGLE_CLIENT_ID and OPENWEIR_GOOGLE_CLIENT_SECRET sign in to the real
Google (docs/maintenance.md §2.8). With --provider, an OpenWeir provider is a fake one on a
loopback port (tests/support/provider_server.py), signed in to as user@example.com with the
password "correct horse battery"; the session stays in memory, not in the system's credential
store. Its user center's links lead nowhere. Source-tree only: the preview's test doubles
never ship in the package.
"""

import argparse
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openweir.app import main
from openweir.host.base import TAKEOVER_NONE, TAKEOVER_SETTINGS, TAKEOVER_TUN
from openweir.models import credentials
from openweir.models.credentials import KeyringCredentials
from openweir.models.plans import default_plan
from openweir.models.routing import MODE_PLAN, RoutingStore
from openweir.models.store import ProfileStore
from openweir.update.releases import ReleaseSource
from openweir.update.updater import Updater
from openweir.viewmodels.appearance import MODES
from tests.support.credentials import MemoryCredentials, MemoryKeyring
from tests.support.google import FakeGoogle
from tests.support.provider_server import FakeProvider
from tests.support.releases import FakeVerifier, sample_github
from tests.support.simulated import (
    SAMPLE_CONFIG,
    SimulatedFetcher,
    SimulatedHost,
    seed_long_plan,
    seed_profiles,
    seed_provider,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the UI against the simulated host")
    parser.add_argument("--lang", choices=("zh", "en"), default="")
    parser.add_argument("--appearance", choices=MODES, default="")
    parser.add_argument("--crowd", type=int, default=0, help="extra quiet connections")
    parser.add_argument(
        "--development", action="store_true", help="refuse TUN and the system proxy"
    )
    parser.add_argument(
        "--takeover",
        choices=(TAKEOVER_SETTINGS, TAKEOVER_TUN),
        # This platform's host: the macOS extension always runs a TUN.
        default=TAKEOVER_TUN if sys.platform == "darwin" else TAKEOVER_SETTINGS,
        help="what connecting takes over",
    )
    parser.add_argument(
        "--plan", action="store_true", help="route with my plan instead of a profile's rules"
    )
    parser.add_argument("--update", action="store_true", help="offer a newer release")
    parser.add_argument("--google", action="store_true", help="back up to a fake Google Drive")
    parser.add_argument(
        "--long-names", action="store_true", help="name everything at length, to try the layout"
    )
    parser.add_argument(
        "--provider", action="store_true", help="sign in to a fake OpenWeir provider"
    )
    args, qt_args = parser.parse_known_args()
    sys.argv[1:] = qt_args
    google = FakeGoogle() if args.google else None
    provider = FakeProvider() if args.provider else None
    if google is not None or provider is not None:
        os.environ["no_proxy"] = "127.0.0.1"  # the fakes are on loopback ports
    if provider is not None:
        provider.config = SAMPLE_CONFIG.encode()  # rules of its own, so it can be used
        # The session goes to the system's credential store, which stays out of a preview.
        keyring = MemoryKeyring()
        credentials.system_keyring = lambda: keyring
    with (
        tempfile.TemporaryDirectory(prefix="openweir-preview-") as directory,
        sample_github(newer=args.update) as github,
        contextlib.closing(google) if google else contextlib.nullcontext(),
        contextlib.closing(provider) if provider else contextlib.nullcontext(),
    ):
        store = ProfileStore(Path(directory))
        seed_profiles(store, time.time(), long_names=args.long_names)
        # The plan of long names is there to choose; --plan routes with it.
        plan = seed_long_plan(store) if args.long_names else None
        if args.plan:
            routing = RoutingStore(store.database)
            if plan is None:
                plan = default_plan()
                routing.put_plan(plan)
            routing.update_state(mode=MODE_PLAN, plan_id=plan.id)
        if provider is not None:
            seed_provider(store, provider, KeyringCredentials(), time.time())
        host = SimulatedHost(
            health_check_s=0.8,
            crowd=args.crowd,
            development=args.development,
            takeover=TAKEOVER_NONE if args.development else args.takeover,
            long_names=args.long_names,
        )
        code = main(
            host,
            store,
            SimulatedFetcher(delay_s=0.8),
            language=args.lang,
            appearance=args.appearance,
            updater=Updater(Path(directory), ReleaseSource(github.latest_url), FakeVerifier()),
            google=google.provider(MemoryCredentials()) if google else None,
        )
    raise SystemExit(code)
