"""Downloading the pinned GEO data (scripts/fetch_geodata.py): a mirror's passing failure is
tried again after a wait, and only the pinned file is kept."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest

from scripts import fetch_geodata

CONTENT = b"pinned geosite"
DATA = fetch_geodata.DataFile(
    "geosite.dat", "GeoSite.dat", len(CONTENT), hashlib.sha256(CONTENT).hexdigest()
)


def serve(answers: list[bytes | None]) -> tuple[list[str], Any]:
    """urlopen answering in turn from ``answers``: bytes to serve, or None for an error."""
    urls: list[str] = []

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        urls.append(url)
        answer = answers.pop(0)
        if answer is None:
            raise OSError("HTTP Error 503: Service Unavailable")
        return io.BytesIO(answer)

    return urls, urlopen


def test_every_mirror_failing_is_tried_again_after_a_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirrors = len(fetch_geodata.MIRRORS)
    # Every mirror fails in the first round; the second round's first mirror serves the file.
    urls, urlopen = serve([None] * mirrors + [CONTENT])
    waits: list[float] = []
    monkeypatch.setattr(fetch_geodata.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(fetch_geodata.time, "sleep", waits.append)
    target = tmp_path / DATA.home_name
    fetch_geodata._download(DATA, target)
    assert target.read_bytes() == CONTENT
    assert len(urls) == mirrors + 1
    assert waits == [fetch_geodata.BACKOFF_S]
    assert [path.name for path in tmp_path.iterdir()] == [DATA.home_name]


def test_a_file_that_is_not_the_pinned_one_is_never_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rounds = fetch_geodata.ATTEMPTS * len(fetch_geodata.MIRRORS)
    urls, urlopen = serve([b"something else"] * rounds)
    waits: list[float] = []
    monkeypatch.setattr(fetch_geodata.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(fetch_geodata.time, "sleep", waits.append)
    with pytest.raises(RuntimeError, match=r"not the pinned geosite\.dat"):
        fetch_geodata._download(DATA, tmp_path / DATA.home_name)
    assert len(urls) == rounds
    assert waits == [fetch_geodata.BACKOFF_S * 2**i for i in range(fetch_geodata.ATTEMPTS - 1)]
    assert not list(tmp_path.iterdir())
