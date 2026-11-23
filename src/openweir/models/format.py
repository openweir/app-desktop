"""Text for numbers the screens show: rates, sizes, durations, latency, dates, versions and
money.

Dates follow the UI language: the patterns are translated and QLocale's default (set by
i18n.Translations) names the months."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QDate, QDateTime, QLocale

if TYPE_CHECKING:
    from openweir.models.provider.types import Currency

_UNITS = ("B", "KB", "MB", "GB", "TB")
# Decimal places of an amount in a currency other than the provider's own, which the site
# does not describe: most currencies' minor unit is a hundredth.
MONEY_DIGITS = 2
# The timestamp and commit a Go pseudo-version adds to vX.Y.Z, vX.Y.Z-pre or vX.0.0.
_PSEUDO_VERSION = re.compile(r"[-.](?:0\.)?\d{14}-[0-9A-Za-z]+$")


def size(byte_count: int) -> str:
    value = float(max(byte_count, 0))
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_UNITS[unit]}" if value < 100 else f"{value:.0f} {_UNITS[unit]}"


def rate(bytes_per_second: int) -> str:
    return size(bytes_per_second) + "/s"


def uptime(seconds: int) -> str:
    seconds = max(seconds, 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


GOOD_MS = 150
FAIR_MS = 300


def latency(delay_ms: int | None) -> tuple[str, str]:
    """Return the text and level ("good", "fair", "poor", "timeout", or "none" when not
    tested) of a delay; 0 is a failed test."""
    if delay_ms is None:
        return "—", "none"
    if delay_ms == 0:
        return QCoreApplication.translate("Format", "超时"), "timeout"
    if delay_ms < GOOD_MS:
        return f"{delay_ms} ms", "good"
    if delay_ms < FAIR_MS:
        return f"{delay_ms} ms", "fair"
    return f"{delay_ms} ms", "poor"


def usage(used_bytes: int, total_bytes: int) -> str:
    """``38.2 / 200 GB``; the unit is written once when both sides share it."""
    used, total = size(used_bytes), size(total_bytes)
    used_value, _, used_unit = used.partition(" ")
    total_value, _, total_unit = total.partition(" ")
    if used_unit == total_unit:
        return f"{used_value} / {total_value} {total_unit}"
    return f"{used} / {total}"


def _local_date(timestamp: float) -> QDate:
    return QDateTime.fromSecsSinceEpoch(int(timestamp)).date()


def date(timestamp: float) -> str:
    # A QDate format pattern (MMM is the month's short name), so translate it as one.
    pattern = QCoreApplication.translate("Format", "yyyy-MM-dd", "date pattern")
    return QLocale().toString(_local_date(timestamp), pattern)


def ago(timestamp: float, now: float) -> str:
    """When something happened: ``刚刚``, ``5 分钟前``, ``2 小时前``, ``3 天前``, then a date."""
    seconds = max(now - timestamp, 0)
    if seconds < 60:
        return QCoreApplication.translate("Format", "刚刚")
    if seconds < 3600:
        return QCoreApplication.translate("Format", "%n 分钟前", None, int(seconds // 60))
    if seconds < 86400:
        return QCoreApplication.translate("Format", "%n 小时前", None, int(seconds // 3600))
    if seconds < 7 * 86400:
        return QCoreApplication.translate("Format", "%n 天前", None, int(seconds // 86400))
    then = _local_date(timestamp)
    if then.year() == _local_date(now).year():
        pattern = QCoreApplication.translate("Format", "M 月 d 日", "date pattern, this year")
        return QLocale().toString(then, pattern)
    return date(timestamp)


def version_number(version: str) -> str:
    """The release number in a version, without a pseudo-version's timestamp and commit or
    build metadata: ``openweir-v1.0.0`` for ``openweir-v1.0.0+v1.19.34-0.2026…-01b2f212fab3``,
    which the core reports for the pinned kernel release (D38), and ``v1.19.33`` for a bare
    ``v1.19.33-0.20261103155416-c2de0f865c7e``."""
    return _PSEUDO_VERSION.sub("", version.strip().partition("+")[0])


def money(amount: int, code: str, currency: Currency | None) -> str:
    """An amount of minor units of the currency ``code`` as the account page shows it
    (DESIGN.md "金额"): ``¥15.00`` with the provider's symbol and decimal places when it is the
    provider's own ``currency`` (the site's), and ``USD 15.00`` by its code otherwise, as an
    old order may be after the provider changed currency, or before the site was read. Thousands
    are grouped: ``¥1,500.00``."""
    own = currency is not None and currency.code == code
    digits = currency.digits if own and currency is not None else MONEY_DIGITS
    # Decimal, so that minor units never pick up a float's rounding.
    value = f"{Decimal(abs(amount)).scaleb(-digits):,.{digits}f}"
    sign = "-" if amount < 0 else ""
    if own and currency is not None:
        return f"{sign}{currency.symbol}{value}"
    return f"{code} {sign}{value}" if code else f"{sign}{value}"
