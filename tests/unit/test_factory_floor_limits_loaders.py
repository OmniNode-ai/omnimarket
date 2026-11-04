# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Limits status mirrors preserve unread facts and never expose token values (OMN-22010)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorEffectContract,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_api_keys,
    load_cloud_accounts,
    load_engines,
    load_token_pools,
)

pytestmark = pytest.mark.unit
STAMP = "2026-11-03T12:00:00Z"


def write(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_cloud_accounts_loads_both_rate_windows_and_credit_source(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path / "cloud-accounts.json",
        {
            "accounts": {
                "a": {
                    "rate": {
                        "read_at": STAMP,
                        "five_hour": {"utilization": 0.42, "resets_at": "five-reset"},
                        "seven_day": {"utilization": 0, "resets_at": "seven-reset"},
                    },
                    "credit": {"remaining": 25.5, "read_at": STAMP, "source": "usage"},
                },
                "b": {},
            }
        },
    )
    accounts = load_cloud_accounts(path)
    assert accounts is not None
    a, b = accounts
    assert (a.rate_read_at, a.credit_read_at) == (STAMP, STAMP)
    assert (a.five_hour_utilization, a.five_hour_resets_at) == (0.42, "five-reset")
    assert (a.seven_day_utilization, a.seven_day_resets_at) == (0, "seven-reset")
    assert (a.credit_remaining, a.credit_source) == (25.5, "usage")
    assert (b.five_hour_utilization, b.seven_day_utilization) == (None, None)
    assert (b.rate_read_at, b.five_hour_resets_at, b.seven_day_resets_at) == (
        "",
        "",
        "",
    )
    assert b.credit_source == ""


def test_api_keys_loads_spend_source_and_optional_budget(tmp_path: Path) -> None:
    path = write(
        tmp_path / "api-credit-meter.json",
        {
            "keys": {
                "a": {
                    "month_spend_usd": 12.5,
                    "read_at": STAMP,
                    "source": "meter",
                    "budget_usd": 50,
                },
                "b": {"month_spend_usd": 0},
            }
        },
    )
    keys = load_api_keys(path)
    assert keys is not None
    a, b = keys
    assert (a.name, a.month_spend_usd, a.read_at, a.source, a.budget_usd) == (
        "a",
        12.5,
        STAMP,
        "meter",
        50,
    )
    assert (b.month_spend_usd, b.source, b.read_at, b.budget_usd) == (
        0,
        path.name,
        "",
        None,
    )


@pytest.mark.parametrize("spend", [None, "12.5", "unread", False, {}, []])
def test_api_keys_non_number_spend_is_unknown(tmp_path: Path, spend: Any) -> None:
    path = write(tmp_path / "api.json", {"keys": {"a": {"month_spend_usd": spend}}})
    keys = load_api_keys(path)
    assert keys is not None
    assert keys[0].month_spend_usd is None


def test_engines_loads_windows_and_lab_rungs(tmp_path: Path) -> None:
    path = write(
        tmp_path / "engines.json",
        {
            "codex": {
                "used_pct": 0.3,
                "window": "5h",
                "resets_at": "reset",
                "read_at": STAMP,
                "source": "codex usage",
            },
            "glm": {"used_pct": 0},
            "lab_rungs": {
                "a": {
                    "available": True,
                    "last_smoke_at": STAMP,
                    "last_smoke_ok": False,
                    "read_at": STAMP,
                    "source": "canary",
                },
                "b": {"available": False},
            },
        },
    )
    codex, glm, rungs = load_engines(path)
    assert codex is not None
    assert glm is not None
    assert rungs is not None
    assert codex.model_dump() == {
        "used_pct": 0.3,
        "window": "5h",
        "resets_at": "reset",
        "read_at": STAMP,
        "source": "codex usage",
    }
    assert (glm.used_pct, glm.source) == (0, path.name)
    assert (rungs[0].name, rungs[0].available, rungs[0].last_smoke_ok) == (
        "a",
        True,
        False,
    )
    assert (rungs[0].last_smoke_at, rungs[0].read_at, rungs[0].source) == (
        STAMP,
        STAMP,
        "canary",
    )
    assert (rungs[1].available, rungs[1].last_smoke_ok, rungs[1].source) == (
        False,
        None,
        path.name,
    )


@pytest.mark.parametrize("engine", ["codex", "glm"])
def test_engines_missing_keys_remain_unread(tmp_path: Path, engine: str) -> None:
    path = write(tmp_path / "engines.json", {engine: {}})
    codex, glm, rungs = load_engines(path)
    present, absent = (codex, glm) if engine == "codex" else (glm, codex)
    assert present is not None
    assert present.used_pct is None
    assert absent is None
    assert rungs is None
    write(path, {"lab_rungs": {}})
    assert load_engines(path) == (None, None, ())


def test_token_pools_keeps_only_status_fields_and_skips_invalid_files(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path / "claude-token-pool.h200.json",
        {
            "host": "h200",
            "at": STAMP,
            "ignored": "not emitted",
            "tokens": [
                {"label": "a", "until": None, "revoked": False, "value": "not emitted"},
                {"label": "b", "until": "future", "revoked": True},
            ],
        },
    )
    write(tmp_path / "claude-token-pool.h201.json", {"host": "h201", "tokens": []})
    (tmp_path / "claude-token-pool.bad.json").write_text("{bad", encoding="utf-8")
    write(tmp_path / "unmatched.json", {"host": "ignored", "tokens": []})
    pools = load_token_pools(tmp_path, "claude-token-pool.*.json")
    assert pools is not None
    assert [p.host for p in pools] == ["h200", "h201"]
    assert pools[0].model_dump() == {
        "host": "h200",
        "read_at": STAMP,
        "source": path.name,
        "tokens": (
            {"label": "a", "limited_until": "", "revoked": False},
            {"label": "b", "limited_until": "future", "revoked": True},
        ),
    }
    assert pools[1].tokens == ()


def test_loaders_missing_or_invalid_sources_are_unread(tmp_path: Path) -> None:
    path = tmp_path / "source.json"
    for invalid in (None, "{bad", "[]", "null"):
        if invalid is not None:
            path.write_text(invalid, encoding="utf-8")
        assert load_cloud_accounts(path) is None
        assert load_api_keys(path) is None
        assert load_engines(path) == (None, None, None)
        assert load_token_pools(tmp_path, "*.json") is None
    assert load_token_pools(tmp_path, "no-match.*.json") is None
    assert load_token_pools(tmp_path / "absent", "*.json") is None


def test_loaders_unreadable_files_are_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path / "source.json", {})

    def refused(*args: Any, **kwargs: Any) -> str:
        raise PermissionError("cannot read source")

    monkeypatch.setattr(Path, "read_text", refused)
    assert load_cloud_accounts(path) is None
    assert load_api_keys(path) is None
    assert load_engines(path) == (None, None, None)
    assert load_token_pools(tmp_path, "*.json") is None


def test_real_effect_contract_validates_with_limit_sources() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/omnimarket/nodes/node_factory_floor_effect/contract.yaml"
    )
    contract = ModelFactoryFloorEffectContract.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    assert contract.runtime.limit_sources.model_dump() == {
        "api_meter_relpath": ".claude_scratch/limits/api-credit-meter.json",
        "engines_relpath": ".claude_scratch/limits/engines.json",
        "token_pool_glob": ".claude_scratch/limits/claude-token-pool.*.json",
    }
