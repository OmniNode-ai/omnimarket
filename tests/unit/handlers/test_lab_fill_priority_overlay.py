# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab_fill_priority and lab_fill_rank overlays load strictly (OMN-21904).

Every value below is a neutral placeholder. A missing file refuses naming the key, and each
malformed declaration fails the load; none falls back to a default.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.lab_fill_priority_overlay import (
    PRIORITY_OVERLAY_ID,
    RANK_OVERLAY_ID,
    load_priority_declaration,
    load_rank_config,
)
from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 2)

RANK: dict[str, Any] = {
    "version": 1,
    "mode": "shadow",
    "blocks_cap": 5,
    "age_days_cap": 90,
    "redispatch_cap": 3,
    "description_read_cap": 50,
    "max_boost_days": 30,
    "boost_multiplier_min": "1.0",
    "boost_multiplier_max": "3.0",
    "priority_share": "0.6",
    "age_share": "0.4",
    "priority_map": {
        1: "1.0",
        2: "0.75",
        3: "0.5",
        4: "0.25",
        0: "0.1",
    },
    "size_buckets": [
        {"max_estimate": 1, "size": "1.0"},
        {"max_estimate": 3, "size": "1.5"},
        {"size": "2.0"},
    ],
    "size_unestimated": "1.5",
    "throughput_tags": ["tag-a"],
}

DECLARATION: dict[str, Any] = {
    "version": 1,
    "weights": {
        "unblock": "0.30",
        "beta_gate": "0.25",
        "throughput": "0.20",
        "priority_age": "0.15",
        "rework": "0.20",
    },
    "boost": [
        {
            "match": {"ticket": "OMN-1"},
            "multiplier": "2.0",
            "expires": date(2026, 11, 15),
            "source": "row-a",
        }
    ],
    "exclude": [{"match": {"repo": "repo-a"}, "reason": "not for the factory"}],
}


def write(
    tmp_path: Path, rank: Any = RANK, declaration: Any = DECLARATION
) -> dict[str, str]:
    """Write both overlays under one root and return the environment that names it."""
    for overlay_id, document in (
        (RANK_OVERLAY_ID, rank),
        (PRIORITY_OVERLAY_ID, declaration),
    ):
        if document is None:
            continue
        target = tmp_path / overlay_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "overlay.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
    return {OVERLAY_ROOTS_ENV: str(tmp_path)}


def edited(
    base: dict[str, Any], path: tuple[str | int, ...], value: Any
) -> dict[str, Any]:
    document = deepcopy(base)
    node: Any = document
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return document


def dropped(base: dict[str, Any], path: tuple[str | int, ...]) -> dict[str, Any]:
    document = deepcopy(base)
    node: Any = document
    for step in path[:-1]:
        node = node[step]
    del node[path[-1]]
    return document


def test_lab_fill_priority_overlay_loads_exact_decimals(tmp_path: Path) -> None:
    env = write(tmp_path)
    declaration = load_priority_declaration(TODAY, env)
    assert declaration.weights.unblock == Fraction(3, 10)
    assert declaration.weights.rework == Fraction(1, 5)
    assert declaration.boost[0].multiplier == Fraction(2)
    assert declaration.exclude[0].match.repo == "repo-a"
    assert load_rank_config(env).mode == "shadow"
    assert declaration.boosts_expired(TODAY) == 0


def test_lab_fill_priority_overlay_missing_file_refuses_naming_the_key(
    tmp_path: Path,
) -> None:
    env = {OVERLAY_ROOTS_ENV: str(tmp_path / "empty")}
    with pytest.raises(NodeOverlayError, match=r"lab_fill_priority\.weights"):
        load_priority_declaration(TODAY, env, load_rank_config(write(tmp_path)))
    with pytest.raises(NodeOverlayError, match=r"lab_fill_rank\.mode"):
        load_rank_config(env)


def test_lab_fill_priority_overlay_has_no_default_mode(tmp_path: Path) -> None:
    env = write(tmp_path, rank=dropped(RANK, ("mode",)))
    with pytest.raises(NodeOverlayError, match=r"fields \['mode'\]"):
        load_rank_config(env)


@pytest.mark.parametrize("mode", ["", "ON", "enabled", True, 1])
def test_lab_fill_priority_overlay_mode_is_off_shadow_or_on(
    tmp_path: Path, mode: Any
) -> None:
    env = write(tmp_path, rank=edited(RANK, ("mode",), mode))
    with pytest.raises(NodeOverlayError, match=r"fields \['mode'\]"):
        load_rank_config(env)


@pytest.mark.parametrize(
    ("label", "declaration", "field"),
    [
        (
            "boost without expires",
            dropped(DECLARATION, ("boost", 0, "expires")),
            "boost.0.expires",
        ),
        (
            "unknown key",
            {**DECLARATION, "score": "1.0"},
            "score",
        ),
        (
            "unknown key inside a boost",
            edited(DECLARATION, ("boost", 0, "rank"), 1),
            "boost.0.rank",
        ),
        (
            "negative weight",
            edited(DECLARATION, ("weights", "unblock"), "-0.30"),
            "weights.unblock",
        ),
        (
            "missing weight",
            dropped(DECLARATION, ("weights", "rework")),
            "weights.rework",
        ),
        (
            "unquoted float weight",
            edited(DECLARATION, ("weights", "unblock"), 0.30),
            "weights.unblock",
        ),
        (
            "integer weight",
            edited(DECLARATION, ("weights", "beta_gate"), 1),
            "weights.beta_gate",
        ),
        (
            "unquoted float multiplier",
            edited(DECLARATION, ("boost", 0, "multiplier"), 2.0),
            "boost.0.multiplier",
        ),
        (
            "expires as a quoted string",
            edited(DECLARATION, ("boost", 0, "expires"), "2026-11-15"),
            "boost.0.expires",
        ),
        (
            "boost matching a repo",
            edited(DECLARATION, ("boost", 0, "match"), {"repo": "repo-a"}),
            "boost.0",
        ),
        (
            "exclude matching a label",
            edited(DECLARATION, ("exclude", 0, "match"), {"label": "tag-a"}),
            "exclude.0",
        ),
        (
            "match naming two keys",
            edited(
                DECLARATION,
                ("boost", 0, "match"),
                {"ticket": "OMN-1", "label": "tag-a"},
            ),
            "boost.0.match",
        ),
        (
            "exclude without a reason",
            edited(DECLARATION, ("exclude", 0, "reason"), ""),
            "exclude.0.reason",
        ),
    ],
)
def test_lab_fill_priority_overlay_declaration_refusals(
    tmp_path: Path, label: str, declaration: dict[str, Any], field: str
) -> None:
    env = write(tmp_path, declaration=declaration)
    with pytest.raises(NodeOverlayError, match=rf"lab_fill_priority.*{field}"):
        load_priority_declaration(TODAY, env)


@pytest.mark.parametrize(
    ("label", "rank", "field"),
    [
        ("zero cap", edited(RANK, ("blocks_cap",), 0), "blocks_cap"),
        ("negative cap", edited(RANK, ("age_days_cap",), -1), "age_days_cap"),
        (
            "shares that do not sum to 1",
            edited(RANK, ("age_share",), "0.5"),
            "<document>",
        ),
        (
            "share above 1",
            edited(RANK, ("priority_share",), "1.5"),
            "<document>",
        ),
        (
            "boost cap above 30 days",
            edited(RANK, ("max_boost_days",), 31),
            "max_boost_days",
        ),
        (
            "multiplier bounds reversed",
            edited(RANK, ("boost_multiplier_min",), "4.0"),
            "<document>",
        ),
        (
            "priority map missing a key",
            dropped(RANK, ("priority_map", 4)),
            "<document>",
        ),
        (
            "size bucket that is not positive",
            edited(RANK, ("size_buckets", 0, "size"), "0"),
            "size_buckets.0",
        ),
        (
            "size buckets out of order",
            edited(RANK, ("size_buckets", 0, "max_estimate"), 5),
            "<document>",
        ),
        (
            "last size bucket bounded",
            edited(RANK, ("size_buckets", 2, "max_estimate"), 8),
            "<document>",
        ),
        (
            "unquoted float share",
            edited(RANK, ("priority_share",), 0.6),
            "priority_share",
        ),
    ],
)
def test_lab_fill_priority_overlay_rank_config_refusals(
    tmp_path: Path, label: str, rank: dict[str, Any], field: str
) -> None:
    env = write(tmp_path, rank=rank)
    with pytest.raises(NodeOverlayError, match=rf"lab_fill_rank.*{field}"):
        load_rank_config(env)


def test_lab_fill_priority_overlay_expired_boost_loads_and_is_counted(
    tmp_path: Path,
) -> None:
    expired = edited(DECLARATION, ("boost", 0, "expires"), date(2026, 11, 1))
    declaration = load_priority_declaration(TODAY, write(tmp_path, declaration=expired))
    assert declaration.boosts_expired(TODAY) == 1
    assert declaration.active_boosts(TODAY) == ()


def test_lab_fill_priority_overlay_boost_is_valid_through_its_expires_day(
    tmp_path: Path,
) -> None:
    declaration = load_priority_declaration(TODAY, write(tmp_path))
    last_day = declaration.boost[0].expires
    assert declaration.boosts_expired(last_day) == 0
    assert len(declaration.active_boosts(last_day)) == 1
    assert declaration.boosts_expired(date(2026, 11, 16)) == 1


@pytest.mark.parametrize(
    ("expires", "loads"),
    [(date(2026, 12, 2), True), (date(2026, 12, 3), False)],
)
def test_lab_fill_priority_overlay_expiry_is_capped_at_max_boost_days(
    tmp_path: Path, expires: date, loads: bool
) -> None:
    far = edited(DECLARATION, ("boost", 0, "expires"), expires)
    env = write(tmp_path, declaration=far)
    if loads:
        assert load_priority_declaration(TODAY, env).boost[0].expires == expires
    else:
        with pytest.raises(NodeOverlayError, match=r"fields \['boost\[0\]\.expires'\]"):
            load_priority_declaration(TODAY, env)


@pytest.mark.parametrize("multiplier", ["0.5", "3.5"])
def test_lab_fill_priority_overlay_multiplier_outside_bounds_fails(
    tmp_path: Path, multiplier: str
) -> None:
    out = edited(DECLARATION, ("boost", 0, "multiplier"), multiplier)
    with pytest.raises(NodeOverlayError, match=r"fields \['boost\[0\]\.multiplier'\]"):
        load_priority_declaration(TODAY, write(tmp_path, declaration=out))


def test_lab_fill_priority_overlay_refusal_never_prints_a_value(tmp_path: Path) -> None:
    secret = "operator-private-repo-name"
    bad = edited(DECLARATION, ("exclude", 0, "match"), {"repo": secret, "label": "x"})
    with pytest.raises(NodeOverlayError) as raised:
        load_priority_declaration(TODAY, write(tmp_path, declaration=bad))
    assert secret not in str(raised.value)


def test_lab_fill_priority_overlay_first_root_holding_one_wins(tmp_path: Path) -> None:
    empty, first, second = tmp_path / "empty", tmp_path / "first", tmp_path / "second"
    empty.mkdir()
    write(first, declaration=edited(DECLARATION, ("weights", "unblock"), "0.99"))
    write(second)
    env = {OVERLAY_ROOTS_ENV: f"{empty}:{first}:{second}"}
    assert load_priority_declaration(TODAY, env).weights.unblock == Fraction(99, 100)
