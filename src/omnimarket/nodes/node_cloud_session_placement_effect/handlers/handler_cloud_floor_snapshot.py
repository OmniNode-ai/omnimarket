# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only, field-selected floor view of the runner's account state."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_factory_floor_input import ModelFloorCloudAccount


def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        else None
    )


def _stamp(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.isoformat() if parsed.tzinfo else ""


def read_floor_snapshot(placement_dir: str) -> tuple[ModelFloorCloudAccount, ...]:
    """Read an atomic committed file; never create, migrate, prune or rewrite account state.

    Raw login probes, identities, credentials, refusal text and arbitrary credit source text
    are deliberately absent from this projection and from its errors.
    """
    from .cloud_accounts import CLOUD_STATE_FILE

    if placement_dir:
        directory = Path(placement_dir)
    else:
        cfg = yaml.safe_load((Path(__file__).parents[1] / "contract.yaml").read_text())[
            "floor_source"
        ]
        directory = (
            Path(os.environ[cfg["placement_dir_env"]])
            if os.environ.get(cfg["placement_dir_env"])
            else Path.home() / cfg["placement_dir_fallback"]
        )
    state = json.loads((directory / CLOUD_STATE_FILE).read_text(encoding="utf-8"))
    accounts = state["accounts"]
    logins = state.get("logins", {})
    out = []
    for name, account in sorted(accounts.items()):
        credit = account.get("credit") or {}
        rate = account.get("rate") or {}
        five, seven = rate.get("five_hour") or {}, rate.get("seven_day") or {}
        observed = logins.get(name, {})
        login = (
            "ok"
            if any(v.get("cloud_ok") is True for v in observed.values())
            else "unreadable"
        )
        out.append(
            ModelFloorCloudAccount(
                name=name,
                identity="account",
                login=login,
                cap=account.get("cap") if isinstance(account.get("cap"), int) else None,
                active=tuple(sorted(account.get("active") or {})),
                credit_remaining=_number(credit.get("remaining")),
                credit_read_at=_stamp(credit.get("read_at")),
                credit_source="cloud credit record",
                rate_status=rate.get("status")
                if rate.get("status")
                in {"allowed", "allowed_warning", "rejected", "limited"}
                else "",
                rate_read_at=_stamp(rate.get("read_at")),
                rate_resets_at=_stamp(rate.get("resets_at")),
                five_hour_utilization=_number(five.get("utilization")),
                five_hour_resets_at=_stamp(five.get("resets_at")),
                seven_day_utilization=_number(seven.get("utilization")),
                seven_day_resets_at=_stamp(seven.get("resets_at")),
            )
        )
    return tuple(out)
