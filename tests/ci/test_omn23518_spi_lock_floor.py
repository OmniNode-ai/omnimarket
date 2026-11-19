# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill orchestrator resolves omnibase-spi at or above 0.23.15 (OMN-23518).

The floor is held in ``project.dependencies`` and in ``tool.uv.override-dependencies``
of ``pyproject.toml``, once each. ``uv.lock`` does not record those specifiers: for a
package carrying a ``[tool.uv.sources]`` git source it records the git source in
``manifest.overrides`` and in the root ``requires-dist``, and the resolved version in
its package entry. So the lock is tied to the floor through that locked version, and
the installed distribution is tied to the lock through ``importlib.metadata``.
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_FLOOR = Version("0.23.15")
_CEILING = Version("0.24")
_SPI_SPEC = "omnibase-spi>=0.23.15,<0.24.0"
_SPI = "omnibase-spi"
_RANGE = SpecifierSet(">=0.23.15,<0.24.0")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
# The last omnibase-spi release whose omnibase_spi.contracts.services lacks the three lane
# process names (the v0.23.15 CHANGELOG lists them under "Included Since v0.23.14").
_LAST_RELEASE_WITHOUT_LANE_TYPES = Version("0.23.14")


def _in_range(version: str) -> bool:
    """True when ``version`` is at or above the floor and below 0.24 (rc/dev safe)."""
    parsed = Version(version)
    return parsed >= _FLOOR and Version(parsed.base_version) < _CEILING


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _lock() -> dict[str, Any]:
    return tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))


def _spi_entries(entries: list[str]) -> list[str]:
    """Every entry that names omnibase-spi, whatever its operator, extra or URL form."""
    return [e for e in entries if canonicalize_name(Requirement(e).name) == _SPI]


def _lock_source_matches(lock_git: str, locked_source: str) -> bool:
    """The lock appends ``#<commit>`` to the declared source; a longer tag must not match."""
    return lock_git.startswith(locked_source + "#")


def _assert_installed_is_locked() -> None:
    installed = importlib.metadata.version(_SPI)
    assert [installed] == _locked_spi_versions(), (
        f"installed omnibase-spi {installed} differs from the locked version"
    )


def _just_above(version: Version) -> Version:
    """The release one step above ``version`` in its last release component."""
    release = list(version.release)
    release[-1] += 1
    return Version(".".join(str(part) for part in release))


def published_pin_admits_floor_range(requires_dist: list[str]) -> bool:
    """True when the one omnibase-spi pin in ``requires_dist`` admits a release in the floor range.

    ``requires_dist`` is the published package's dependency list. The pin is compared with
    the floor range through ``packaging``: it admits the range when some version named by the
    pin, the release just above one of its bounds, or the floor itself, satisfies both the pin
    and the floor range. The probe just above a bound is what lets ``>0.23.15`` and
    ``!=0.23.15`` read as admitting 0.23.16.
    """
    pins = [
        r
        for r in map(Requirement, requires_dist)
        if canonicalize_name(r.name) == _SPI
        and (r.marker is None or r.marker.evaluate({"extra": ""}))
    ]
    assert len(pins) == 1, pins
    spec = pins[0].specifier
    bounds = [Version(s.version.rstrip(".*")) for s in spec]
    candidates = [*bounds, *(_just_above(b) for b in bounds), _FLOOR]
    return any(
        spec.contains(v, prereleases=True) and _RANGE.contains(v, prereleases=True)
        for v in candidates
    )


def lab_comment_head_is_full(comments: list[str], head: str) -> bool:
    """True when the newest ``Lab: head=`` comment names exactly the 40-character ``head``."""
    labs = [c for c in comments if c.startswith("Lab: head=")]
    if not labs:
        return False
    named = re.match(r"Lab: head=(\S+)", labs[-1])
    return (
        named is not None and named.group(1) == head and bool(_FULL_SHA.fullmatch(head))
    )


def _locked_spi_versions() -> list[str]:
    return [p["version"] for p in _lock()["package"] if p["name"] == _SPI]


@pytest.mark.parametrize("version", ["0.23.15", "0.23.16", "0.23.16rc1", "0.23.99"])
def test_in_range_accepts_the_floor_and_later_0_23_releases(version: str) -> None:
    assert _in_range(version), version


@pytest.mark.parametrize(
    "version", ["0.23.14", "0.23.3", "0.24.0", "0.24.0rc1", "0.24.0.dev1", "1.0.0"]
)
def test_in_range_rejects_below_the_floor_and_the_0_24_line(version: str) -> None:
    assert not _in_range(version), version


def test_pyproject_floor_held_in_both_places() -> None:
    doc = _pyproject()
    in_deps = _spi_entries(doc["project"]["dependencies"])
    in_overrides = _spi_entries(doc["tool"]["uv"]["override-dependencies"])
    assert in_deps == [_SPI_SPEC], in_deps
    assert in_overrides == [_SPI_SPEC], in_overrides


def test_declared_floor_excludes_the_release_without_lane_types() -> None:
    """The floor must keep out 0.23.14, which lacks the types the fill orchestrator imports."""
    doc = _pyproject()
    for entries in (
        doc["project"]["dependencies"],
        doc["tool"]["uv"]["override-dependencies"],
    ):
        (entry,) = _spi_entries(entries)
        spec = Requirement(entry).specifier
        assert not spec.contains(_LAST_RELEASE_WITHOUT_LANE_TYPES, prereleases=True), (
            entry
        )
        assert spec.contains(_FLOOR, prereleases=True), entry


@pytest.mark.parametrize(
    "entry",
    [
        "omnibase-spi==0.23.7",
        "omnibase-spi~=0.23.7",
        "omnibase-spi[extra]>=0.23.15",
        "omnibase-spi @ git+https://example.invalid/omnibase_spi.git",
        "omnibase_spi>=0.23.15,<0.24.0",
        "Omnibase.SPI>=0.23.15",
    ],
)
def test_spi_entries_sees_every_spelling_of_a_second_entry(entry: str) -> None:
    """A second spi entry, in any form, breaks the one-entry-each assertion."""
    entries = [_SPI_SPEC, entry, "pydantic>=2"]
    assert _spi_entries(entries) == [_SPI_SPEC, entry]


def test_spi_entries_ignores_other_packages() -> None:
    assert (
        _spi_entries(["omnibase-spi-extra>=1", "omnibase-core>=0.47", "pydantic"]) == []
    )


def test_lock_resolves_spi_in_range_from_the_declared_source() -> None:
    lock = _lock()
    source = _pyproject()["tool"]["uv"]["sources"][_SPI]
    locked_source = f"{source['git']}?tag={source['tag']}"
    pinned = [p for p in lock["package"] if p["name"] == _SPI]
    assert len(pinned) == 1, pinned
    assert _in_range(pinned[0]["version"]), pinned[0]["version"]
    assert _lock_source_matches(pinned[0]["source"]["git"], locked_source), pinned[0][
        "source"
    ]
    overrides = [o for o in lock["manifest"]["overrides"] if o["name"] == _SPI]
    assert [o["git"] for o in overrides] == [locked_source], overrides
    root = next(p for p in lock["package"] if p["name"] == "omnimarket")
    required = [r for r in root["metadata"]["requires-dist"] if r["name"] == _SPI]
    assert [r["git"] for r in required] == [locked_source], required


@pytest.mark.parametrize("suffix", ["-2", "x", ".1"])
def test_lock_source_match_rejects_a_longer_tag(suffix: str) -> None:
    declared = "https://example.invalid/omnibase_spi.git?tag=dev-1"
    assert _lock_source_matches(declared + "#abc123", declared)
    assert not _lock_source_matches(declared + suffix + "#abc123", declared)
    assert not _lock_source_matches(declared, declared)


def test_installed_version_helper_fails_when_it_differs_from_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4 falsifier: an installed 0.23.14 (which lacks the lane process types) is refused."""
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.23.14")
    with pytest.raises(AssertionError, match="differs from the locked version"):
        _assert_installed_is_locked()


@pytest.mark.parametrize(
    ("requires_dist", "admits"),
    [
        (["omnibase-compat==0.5.7", "omnibase-spi==0.23.7"], False),
        (["omnibase-spi==0.23.5"], False),
        (["omnibase-spi==0.24.0"], False),
        (["omnibase-spi==0.23.15"], True),
        (["omnibase-spi==0.23.16"], True),
        (["omnibase-spi>=0.23.15,<0.24.0"], True),
        (["omnibase-spi>=0.23.3,<0.24.0"], True),
        (["omnibase-spi>0.23.15"], True),
        (["omnibase-spi!=0.23.15"], True),
        (["omnibase-spi>0.23.15,<0.23.17"], True),
        (["omnibase-spi<0.23.15"], False),
        (["omnibase-spi>0.23.15,<0.23.16"], False),
        (["omnibase-spi==0.23.99; extra == 'dev'", "omnibase-spi==0.23.7"], False),
    ],
)
def test_published_pin_is_compared_to_the_floor_range(
    requires_dist: list[str], admits: bool
) -> None:
    """The red cause holds only while the published pin excludes the floor range.

    A pin equal to the floor admits it, so the contract's published-pin read, which asserts
    the pin does not admit the range, fails for ``omnibase-spi==0.23.15``.
    """
    assert published_pin_admits_floor_range(requires_dist) is admits


def test_lab_comment_head_must_be_the_full_head() -> None:
    head = "0123456789abcdef0123456789abcdef01234567"
    assert lab_comment_head_is_full([f"Lab: head={head} host h201"], head)
    assert not lab_comment_head_is_full([f"Lab: head={head[:12]} host h201"], head)
    assert not lab_comment_head_is_full([f"Lab: head={head[:12]} host h201"], head[:12])
    assert not lab_comment_head_is_full([f"Lab: head={'f' * 40} host h201"], head)
    assert not lab_comment_head_is_full(["no lab line here"], head)
    assert not lab_comment_head_is_full([], head)


def test_lab_comment_check_reads_the_newest_lab_comment() -> None:
    head = "0123456789abcdef0123456789abcdef01234567"
    newer_short = [f"Lab: head={head}", "other", f"Lab: head={head[:12]}"]
    assert not lab_comment_head_is_full(newer_short, head)
    assert lab_comment_head_is_full(
        ["Lab: head=" + head[:12], f"Lab: head={head}"], head
    )


def test_lane_process_types_import() -> None:
    _assert_installed_is_locked()
    from omnibase_spi.contracts.services import (
        ModelLaneProcessRunResult,
        ModelLaneProcessStartResult,
        ModelLaneStartSpec,
    )

    types = (ModelLaneProcessRunResult, ModelLaneProcessStartResult, ModelLaneStartSpec)
    assert all(isinstance(t, type) for t in types)
