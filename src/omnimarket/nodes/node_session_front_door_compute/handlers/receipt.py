# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The preflight receipt: the required-check assertion, the bus configuration and the host mode.

**The required-check assertion** (once the ledger preflight's ``LEDGER_CHECKS``). A preflight exit 0
says every blocker of the overlay that resolved passed; it does not say which overlay resolved, and
a stale copy that predates the ledger checks is a silent green. So the receipt must show the ledger
checks ran. Under ``operator`` that is all five, ``coordination_host`` included, exactly as before.
Under ``team`` it is the four ledger checks without ``coordination_host`` (an operator-only check),
and the team receipt is held to more: its profile is ``team``, its overlay hash names the bytes at
its overlay path now (and the expected hash, when one is given), each required check passed and the
overall verdict is ``OK``.

**The host mode.** An operator session reads ``coordination_host`` as before: ``coordination`` when
it passed, ``detached`` when it failed, ``unknown`` when it did not run. A team session is
``bus-member`` only when its current team receipt passes the assertion and the bus configuration
holds: a bound lane login (the session's credential-bound broker principal, matched to its declared
entry), a declared host, the bus transport with an executable append command, and a verified
ledger mirror with its ``archive/`` directory. ``ONEX_REMOTE_LANE`` plays no part: a member's
mirror lives outside the registry ledger path with it unset. A team receipt that does not pass is
``unknown``; a passing one without the bus is ``detached``. ``bus-member`` is a startup verdict from
configuration, never proof that the machine is attached.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omnimarket.models.session_scope import ModelIdentityClaim, ModelSessionScope

from ..models.model_session_front_door import (
    HostMode,
    ModelHostModeVerdict,
    ModelReceiptAssertion,
    Profile,
)

TEAM_REQUIRED_CHECKS: tuple[str, ...] = (
    "ledger_path",
    "ledger_basename",
    "ledger_is_registry_ledger",
    "ledger_grammar",
)
OPERATOR_REQUIRED_CHECKS: tuple[str, ...] = (*TEAM_REQUIRED_CHECKS, "coordination_host")
REQUIRED_CHECKS: Mapping[str, tuple[str, ...]] = {
    "operator": OPERATOR_REQUIRED_CHECKS,
    "team": TEAM_REQUIRED_CHECKS,
}
LEDGER_BASENAME = "ROLLING_WORK_LEDGER.md"
STALE_OVERLAY_FIX = (
    "the overlay that resolved predates the ledger checks: point the preflight at the current "
    "session_preflight overlay (SESSION_PREFLIGHT_OVERLAY_PATH or --overlay), or refresh the "
    "per-user copy with session_preflight.py --install-overlay <that overlay>, then rerun"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReceiptError(Exception):
    """The receipt is missing, empty or unreadable: the run refused or never started."""


def read_receipt(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReceiptError(f"preflight receipt {path} is unreadable: {exc}") from exc
    if not text.strip():
        raise ReceiptError(
            f"preflight receipt {path} is empty: the preflight run refused or never started"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"preflight receipt {path} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        raise ReceiptError(f"preflight receipt {path} carries no checks list")
    return data


def _checks(data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for check in data.get("checks") or []:
        if isinstance(check, Mapping) and isinstance(check.get("check_id"), str):
            out[check["check_id"]] = check
    return out


def receipt_profile(data: Mapping[str, Any]) -> str | None:
    """The receipt's profile; a receipt from before profiles is an operator run."""
    value = data.get("profile")
    return value if isinstance(value, str) else None


def assert_required_checks(
    data: Mapping[str, Any],
    profile: Profile,
    *,
    expected_overlay_sha256: str | None = None,
) -> ModelReceiptAssertion:
    """The kept required-check assertion for ``profile``."""
    required = REQUIRED_CHECKS[profile]
    checks = _checks(data)
    ran = tuple(checks)
    missing = tuple(c for c in required if c not in checks)
    findings: list[str] = []
    seen = receipt_profile(data)
    digest = data.get("overlay_sha256")
    digest = digest if isinstance(digest, str) else None
    failed: tuple[str, ...] = ()
    if profile == "operator":
        if seen not in (None, "operator"):
            findings.append(
                f"the receipt's profile is {seen!r}, but this session runs the operator profile"
            )
    else:
        if seen != "team":
            findings.append(
                f"the receipt's profile is {seen!r}, but this session runs the team profile"
            )
        if digest is None or not _SHA256.match(digest):
            findings.append(
                "the receipt names no overlay hash, so what ran cannot be validated"
            )
        else:
            overlay_path = data.get("overlay_path")
            try:
                now = hashlib.sha256(Path(str(overlay_path)).read_bytes()).hexdigest()
            except (OSError, TypeError, ValueError):
                findings.append(
                    f"the receipt's overlay {overlay_path!r} is unreadable now"
                )
            else:
                if now != digest:
                    findings.append(
                        f"the overlay at {overlay_path} changed since the run (stale receipt)"
                    )
        failed = tuple(
            c for c in required if c in checks and checks[c].get("ok") is not True
        )
        if failed:
            findings.append(f"required checks failed: {', '.join(failed)}")
        if data.get("verdict") != "OK":
            findings.append(
                f"the receipt's verdict is {data.get('verdict')!r}, not 'OK'"
            )
    if expected_overlay_sha256 is not None and digest != expected_overlay_sha256:
        findings.append("the receipt's overlay hash is not the expected overlay's")
    if missing:
        findings.append(
            f"NOT RUN {len(missing)}/{len(required)} ({', '.join(missing)}); {STALE_OVERLAY_FIX}"
        )
    return ModelReceiptAssertion(
        ok=not findings,
        profile=profile,
        required=required,
        ran=ran,
        missing=missing,
        failed=failed,
        overlay_sha256=digest,
        findings=tuple(findings),
    )


def bound_lane_login(
    claims: Sequence[ModelIdentityClaim], scope: ModelSessionScope
) -> str | None:
    """The credential-bound broker principal, when it is one of the session's declared principals."""
    for claim in claims:
        if claim.source == "broker_binding" and claim.value in scope.principals:
            return claim.value
    return None


def bus_configuration_reasons(
    env: Mapping[str, str], bound: str | None
) -> tuple[str, ...]:
    """Why this machine's bus configuration does not hold; empty when it does."""
    reasons: list[str] = []
    if bound is None:
        reasons.append(
            "no bound lane login: no credential-bound broker principal matched the session's entry"
        )
    writer = env.get("ONEX_WORK_LEDGER_PRINCIPAL", "")
    if bound is not None and writer and writer != bound:
        reasons.append(
            "ONEX_WORK_LEDGER_PRINCIPAL names another principal than the bound lane login"
        )
    if not env.get("ONEX_LEDGER_HOST_NAME", "").strip():
        reasons.append("no declared host: ONEX_LEDGER_HOST_NAME is unset")
    if env.get("ONEX_LEDGER_WRITE_VIA", "").strip().lower() != "bus":
        reasons.append(
            "the ledger transport is not the bus: ONEX_LEDGER_WRITE_VIA is not 'bus'"
        )
    try:
        command = shlex.split(env.get("ONEX_LEDGER_BUS_APPEND_COMMAND", ""))
    except ValueError:
        command = []
        reasons.append("ONEX_LEDGER_BUS_APPEND_COMMAND does not parse")
    else:
        if not command:
            reasons.append("no append command: ONEX_LEDGER_BUS_APPEND_COMMAND is unset")
        elif shutil.which(command[0], path=env.get("PATH")) is None:
            reasons.append(
                "the append command in ONEX_LEDGER_BUS_APPEND_COMMAND is not executable"
            )
    raw = env.get("ONEX_LEDGER_PATH", "")
    if not raw:
        reasons.append("no ledger mirror: ONEX_LEDGER_PATH is unset")
    else:
        mirror = Path(raw)
        if mirror.name != LEDGER_BASENAME:
            reasons.append(f"the ledger mirror is not named {LEDGER_BASENAME}")
        if not mirror.is_file():
            reasons.append("the ledger mirror named by ONEX_LEDGER_PATH is not a file")
        if not (mirror.parent / "archive").is_dir():
            reasons.append("no archive/ directory beside the ledger mirror")
    return tuple(reasons)


def decide_host_mode(
    data: Mapping[str, Any],
    *,
    profile: Profile,
    receipt_path: Path,
    claims: Sequence[ModelIdentityClaim],
    scope: ModelSessionScope,
    env: Mapping[str, str],
    max_age_seconds: int,
    expected_overlay_sha256: str | None = None,
    now: float | None = None,
) -> ModelHostModeVerdict:
    """The host mode for this session's profile; see the module docstring."""
    seen = receipt_profile(data) or "operator"
    mode: HostMode
    if profile == "operator":
        if seen != "operator":
            return ModelHostModeVerdict(
                mode="unknown",
                profile=profile,
                reasons=(f"an operator session read a {seen!r} receipt",),
            )
        check = _checks(data).get("coordination_host")
        if check is None:
            return ModelHostModeVerdict(
                mode="unknown",
                profile=profile,
                reasons=("coordination_host did not run",),
            )
        mode = "coordination" if check.get("ok") else "detached"
        reasons = () if mode == "coordination" else ("coordination_host failed",)
        return ModelHostModeVerdict(mode=mode, profile=profile, reasons=reasons)

    receipt_reasons = list(
        assert_required_checks(
            data, "team", expected_overlay_sha256=expected_overlay_sha256
        ).findings
    )
    try:
        age = (time.time() if now is None else now) - receipt_path.stat().st_mtime
    except OSError:
        age = float("inf")
    if not -60 <= age <= max_age_seconds:
        receipt_reasons.append(
            f"the receipt is not current (written {int(age)}s ago; at most {max_age_seconds}s)"
        )
    if receipt_reasons:
        return ModelHostModeVerdict(
            mode="unknown", profile=profile, reasons=tuple(receipt_reasons)
        )
    bus = bus_configuration_reasons(env, bound_lane_login(claims, scope))
    mode = "detached" if bus else "bus-member"
    return ModelHostModeVerdict(mode=mode, profile=profile, reasons=bus)
