"""Build the ViewModel graph once, for the app, the preview and the tests."""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from openweir.desktop import links
from openweir.desktop.login import LoginItem, UnavailableLoginItem
from openweir.host.base import TunnelHost
from openweir.models.backup import Backups
from openweir.models.backup_auto import BackupTargets, Changes
from openweir.models.backup_google import GoogleDriveProvider
from openweir.models.backup_icloud import ICloudProvider
from openweir.models.credentials import CredentialStore, KeyringCredentials, ProviderTokens
from openweir.models.geodata import GeoDownloader, GeoUpdates
from openweir.models.library import ProfileLibrary
from openweir.models.provider.store import ProviderStore
from openweir.models.settings import DatabaseSettingsStore, Settings, SettingsStore
from openweir.models.store import ProfileStore
from openweir.models.subscription import Fetcher
from openweir.update.updater import Updater
from openweir.viewmodels.about import AboutViewModel
from openweir.viewmodels.account import AccountViewModel
from openweir.viewmodels.appearance import Appearance
from openweir.viewmodels.backup import BackupViewModel
from openweir.viewmodels.common.async_runner import AsyncRunner
from openweir.viewmodels.connections import ConnectionsViewModel
from openweir.viewmodels.desktop import DesktopViewModel
from openweir.viewmodels.logs import LogsViewModel
from openweir.viewmodels.overview import OverviewViewModel
from openweir.viewmodels.profiles import ProfilesViewModel
from openweir.viewmodels.provider_sign_in import ProviderSignInViewModel
from openweir.viewmodels.proxies import ProxiesViewModel
from openweir.viewmodels.routing import RoutingViewModel
from openweir.viewmodels.settings import SettingsViewModel
from openweir.viewmodels.shell import ShellViewModel
from openweir.viewmodels.system_extension import SystemExtensionViewModel


@dataclass
class ViewModels:
    runner: AsyncRunner
    host: TunnelHost
    library: ProfileLibrary
    settings_store: SettingsStore
    shell: ShellViewModel
    overview: OverviewViewModel
    proxies: ProxiesViewModel
    profiles: ProfilesViewModel
    routing: RoutingViewModel
    connections: ConnectionsViewModel
    logs: LogsViewModel
    settings: SettingsViewModel
    desktop: DesktopViewModel
    appearance: Appearance
    about: AboutViewModel
    backup: BackupViewModel
    system_extension: SystemExtensionViewModel
    provider_sign_in: ProviderSignInViewModel
    account: AccountViewModel
    geo_updates: GeoUpdates

    def initial_properties(self) -> dict[str, object]:
        """Main.qml's required properties."""
        return {
            "shellViewModel": self.shell,
            "overviewViewModel": self.overview,
            "proxiesViewModel": self.proxies,
            "profilesViewModel": self.profiles,
            "routingViewModel": self.routing,
            "connectionsViewModel": self.connections,
            "logsViewModel": self.logs,
            "appearance": self.appearance,
            "settingsViewModel": self.settings,
            "desktopViewModel": self.desktop,
            "aboutViewModel": self.about,
            "backupViewModel": self.backup,
            "systemExtensionViewModel": self.system_extension,
            "providerSignInViewModel": self.provider_sign_in,
            "accountViewModel": self.account,
        }

    def start(self) -> None:
        self.runner.start()
        self.runner.submit(self.geo_updates.run())
        self.shell.start()
        self.overview.start()
        self.proxies.refresh()
        self.profiles.start()
        self.routing.refresh()
        self.logs.start()
        self.settings.start()
        self.desktop.start()
        self.about.start()
        self.backup.start()
        self.account.start()

    def retranslate(self) -> None:
        """Re-read every text the ViewModels make, after the UI language changed."""
        self.shell.retranslate()
        self.overview.retranslate()
        self.proxies.retranslate()
        self.profiles.retranslate()
        self.routing.retranslate()
        self.connections.retranslate()
        self.logs.retranslate()
        self.settings.retranslate()
        self.about.retranslate()
        self.backup.retranslate()
        self.system_extension.retranslate()
        self.provider_sign_in.retranslate()
        self.account.retranslate()

    def open_link(self, link: str) -> None:
        """An install link arrived (docs/release.md §7): show the window and the import form
        with its subscription filled in; the user imports from there. A provider's sign-in
        link (docs/accounts.md §2) opens the sign-in dialog with its address filled in, over
        the page that shows; the user signs in from there."""
        self.desktop.showWindow()
        parsed = links.parse(link)
        if isinstance(parsed, links.ProviderLink):
            self.provider_sign_in.open(parsed.origin)
            return
        self.shell.setPage("profiles")
        if parsed is None:
            self.profiles.rejectLink()
        else:
            self.profiles.prefillImport(parsed.url, parsed.name)

    def shutdown(self) -> None:
        self.shell.stop()
        self.overview.stop()
        self.profiles.stop()
        self.connections.stop()
        self.logs.stop()
        self.desktop.stop()
        self.about.stop()
        self.backup.stop()
        self.provider_sign_in.stop()
        self.account.stop()
        self.runner.shutdown(finalize=self.host.close)


def compose(
    host: TunnelHost,
    store: ProfileStore,
    fetcher: Fetcher,
    clock: Callable[[], float] = time.time,
    *,
    settings: SettingsStore | None = None,
    login_item: LoginItem | None = None,
    tray_available: bool = False,
    start_hidden: bool = False,
    connect_at_login: bool = False,
    appearance: Appearance | None = None,
    updater: Updater | None = None,
    credentials: CredentialStore | None = None,
    icloud: ICloudProvider | None = None,
    google: GoogleDriveProvider | None = None,
    take_over: Callable[[], Coroutine[Any, Any, None]] | None = None,
    update_result: int | None = None,
) -> ViewModels:
    """``clock`` dates profile updates and subscription expiry. The settings live in the
    profiles' database unless given. ``tray_available`` and ``start_hidden``: see DesktopViewModel.
    ``connect_at_login`` requests one connection on an enabled Windows login launch.
    ``appearance`` defaults to the stored choice; the settings page changes it from then on.
    ``updater`` defaults to GitHub's releases, downloaded into the profiles' directory.
    ``credentials`` keeps the WARP account; the system's credential store by default.
    ``icloud`` is the iCloud backup of a packaged macOS app, ``google`` the Google Drive backup
    of a build with a Google OAuth client; automatic backup runs with either. ``take_over``
    is an administrator's take-over of another user's tunnel (OverviewViewModel).
    ``update_result`` is the Linux install helper's exit code, when the UI starts again after
    it: the window opens on the settings page, whose version row says how it went."""
    runner = AsyncRunner()
    settings_store = settings or DatabaseSettingsStore(store.database)
    # The library keeps the provider accounts' sessions in it; diagnostics tell whether there
    # is one (docs/accounts.md §8).
    credentials = credentials or KeyringCredentials()
    library = ProfileLibrary(
        store,
        host,
        fetcher,
        clock,
        settings=lambda: settings_store.current,
        credentials=credentials,
    )
    shell = ShellViewModel(host, runner)
    overview = OverviewViewModel(
        host,
        library,
        runner,
        settings_store,
        take_over=take_over,
        login_item=login_item if connect_at_login else None,
    )
    system_extension = SystemExtensionViewModel(
        host,
        library,
        runner,
        lambda: not overview.property("busy") and not overview.property("connectionChanging"),
    )
    system_extension.busyChanged.connect(overview.setMaintenanceBusy)
    system_extension.finished.connect(overview.refreshGroups)
    system_extension.finished.connect(shell.refreshService)
    overview.changed.connect(
        lambda: system_extension.tunnel_changed(bool(overview.property("connected")))
    )
    proxies = ProxiesViewModel(host, runner, settings_store)
    profiles = ProfilesViewModel(library, runner, clock)
    provider_sign_in = ProviderSignInViewModel(library, runner, clock)
    account = AccountViewModel(library, runner, clock)
    routing = RoutingViewModel(library, runner, settings_store)
    connections = ConnectionsViewModel(host, runner)
    logs = LogsViewModel(host, runner)
    settings_page = SettingsViewModel(
        settings_store,
        library,
        host,
        runner,
        login_item or UnavailableLoginItem(),
        tray_available=tray_available,
    )
    desktop = DesktopViewModel(
        settings_store,
        tray_available=tray_available,
        start_hidden=start_hidden,
        disconnect=overview.disconnect_to_quit,
    )
    about = AboutViewModel(
        updater or Updater(store.directory),
        settings_store,
        host,
        library,
        runner,
        logs.held,
        crash_directory=store.directory / "crashes",
        update_result=update_result,
        providers=ProviderStore(store.database),
        provider_tokens=ProviderTokens(credentials),
    )
    if update_result is not None:
        shell.setPage("settings")

    def installer_opened(service_keeps_tunnel: bool) -> None:
        """Go once the system has opened the update (D41, docs/release.md §3). Windows'
        installer would close the UI with --quit anyway, as the Linux package's scripts would,
        and the service keeps the tunnel through the upgrade (was_running). Nothing does that
        for a disk image: the old system extension's tunnel would run on with no UI while the
        app is replaced, so OpenWeir quits as 退出 OpenWeir does, disconnecting first."""
        if service_keeps_tunnel:
            desktop.leave()
        else:
            desktop.quit()

    about.installerOpened.connect(installer_opened)
    backup = BackupViewModel(
        Backups(store.database),
        library,
        settings_store,
        runner,
        clock=clock,
        targets=BackupTargets(store.database),
        changes=Changes(store.database),
        icloud=icloud,
        google=google,
    )

    def page_shown() -> None:
        if shell.currentPage == "proxies":
            proxies.refresh()
        elif shell.currentPage == "routing":
            routing.refresh()
        elif shell.currentPage == "profiles":
            profiles.pageShown()
        elif shell.currentPage == "settings":
            settings_page.refreshInfo()
            settings_page.refreshLoginItem()
            backup.refreshCloud()
        connections.setActive(desktop.window_shown and shell.currentPage == "connections")
        # The account page syncs as it shows, and reads the wallet, plans and orders.
        account.set_shown(shell.currentPage == "account")

    def window_shown() -> None:
        # While the window hides, only the tray's status is polled at full rate (ui.md §8).
        overview.set_window_shown(desktop.window_shown)
        logs.set_window_shown(desktop.window_shown)
        connections.setActive(desktop.window_shown and shell.currentPage == "connections")

    def profiles_applied() -> None:
        # The routing page lists the rules the core runs; shown later, it reads them then.
        if shell.currentPage == "routing":
            routing.refresh()

    shell.currentPageChanged.connect(page_shown)
    desktop.windowShownChanged.connect(window_shown)
    # Back from a provider's user center in the browser, its account syncs (D58).
    desktop.activated.connect(profiles.windowActivated)
    desktop.activated.connect(account.windowActivated)
    window_shown()
    proxies.selectionApplied.connect(overview.refreshGroups)
    profiles.applied.connect(overview.refreshGroups)
    profiles.applied.connect(proxies.refresh)
    profiles.applied.connect(profiles_applied)
    # Signing in to a provider adds its subscriptions as profiles, and using one of them
    # changes what the tunnel runs (docs/accounts.md §7.3).
    # 登录服务商… on the subscriptions page and the overview, and 重新登录… on a provider's row,
    # which names the provider's address.
    profiles.providerSignInRequested.connect(provider_sign_in.open)
    provider_sign_in.profilesChanged.connect(profiles.reload)
    provider_sign_in.profilesChanged.connect(routing.reload)
    provider_sign_in.applied.connect(overview.refreshGroups)
    provider_sign_in.applied.connect(proxies.refresh)
    provider_sign_in.applied.connect(profiles_applied)
    # The account page (docs/accounts.md §7.4): the sidebar lists it while there is an
    # account. It reads the accounts again whenever the profiles change, which a sync, a
    # sign-in or a sign-out on the subscriptions page does, and its own 使用, 退出登录 and
    # syncs change the profiles the other pages show.
    account.accountsChanged.connect(shell.has_accounts)
    account.signInRequested.connect(provider_sign_in.open)
    provider_sign_in.signedIn.connect(account.signed_in)
    provider_sign_in.profilesChanged.connect(account.reload)
    profiles.changed.connect(account.reload)
    account.profilesChanged.connect(profiles.reload)
    account.profilesChanged.connect(routing.reload)
    account.applied.connect(overview.refreshGroups)
    account.applied.connect(proxies.refresh)
    account.applied.connect(profiles_applied)

    def account_requested(provider_id: str) -> None:
        """The overview's 续费: the account page, on the subscription's provider."""
        account.selectProvider(provider_id)
        shell.setPage("account")

    profiles.accountRequested.connect(account_requested)
    # One page's change of the pool or the routing shows on the other (routing.md §4.2).
    profiles.routingChanged.connect(routing.reload)
    profiles.routingRequested.connect(lambda: shell.setPage("routing"))
    routing.routingChanged.connect(profiles.reload)
    routing.applied.connect(overview.refreshGroups)
    routing.applied.connect(proxies.refresh)
    # A restored backup changes the profiles and the routing under every page.
    backup.restored.connect(profiles.reload)
    backup.restored.connect(routing.reload)
    backup.restored.connect(overview.refreshGroups)
    backup.restored.connect(proxies.refresh)
    backup.restored.connect(account.reload)
    # The WARP exit registers once the tunnel runs (docs/exits.md §2).
    overview.changed.connect(lambda: routing.tunnel_running(bool(overview.property("connected"))))
    # The proxies page lists the running core's groups: it shows while the connection switch
    # is on and settled, not while it connects, disconnects or recovers (D27).
    overview.changed.connect(
        lambda: shell.tunnel_running(
            bool(overview.property("connected")) and not overview.property("connectionChanging")
        )
    )

    def routing_shown() -> None:
        proxies.set_pool(*routing.pool_sources())
        connections.set_entry_names(routing.entry_names())

    routing.changed.connect(routing_shown)
    routing_shown()
    appearance = appearance or Appearance(settings_store.current.appearance)

    def appearance_stored(before: Settings, after: Settings) -> None:
        if after.appearance != before.appearance:
            appearance.setMode(after.appearance)

    settings_store.subscribe(appearance_stored)
    return ViewModels(
        runner,
        host,
        library,
        settings_store,
        shell,
        overview,
        proxies,
        profiles,
        routing,
        connections,
        logs,
        settings_page,
        desktop,
        appearance,
        about,
        backup,
        system_extension,
        provider_sign_in,
        account,
        GeoUpdates(host, GeoDownloader(store.directory / "geodata-cache")),
    )
