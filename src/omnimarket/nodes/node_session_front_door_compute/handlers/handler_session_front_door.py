# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A session's front door, bound to its principal through the team identity declaration first.

The binding (``omnimarket.handlers.session_scope``) decides the profile: ``operator`` only for a
positive, authenticated match of the declaration's one operator entry; ``team`` for every other
session, including an unknown, unavailable, mismatched or environment-only identity and a missing or
malformed declaration. An environment ``principal=`` string is never operator identification.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from omnimarket.handlers.session_scope import bind_session_scope

from ..models.model_session_front_door import (
    ModelSessionFrontDoorRequest,
    ModelSessionFrontDoorResult,
    Profile,
)
from . import receipt as rc
from . import session_files as sf
from .entry import run_entry


def _need(value: str | None, name: str) -> str:
    if not value:
        raise sf.RefusedError(f"operation needs {name}")
    return value


class HandlerSessionFrontDoor:
    def handle(
        self, request: ModelSessionFrontDoorRequest
    ) -> ModelSessionFrontDoorResult:
        scope = bind_session_scope(request.identity_claims, request.overlay_roots)
        profile: Profile = "operator" if scope.is_operator else "team"
        reason = (
            "the authenticated identity matched the operator entry"
            if scope.is_operator
            else (scope.diagnostic or f"matched member entry {scope.entry_id}")
        )
        env = (
            dict(os.environ)
            if request.environment is None
            else dict(request.environment)
        )
        base: dict[str, Any] = {
            "operation": request.operation,
            "scope": scope,
            "profile": profile,
            "profile_reason": reason,
        }
        op = request.operation
        try:
            if op == "profile":
                return ModelSessionFrontDoorResult(
                    **base, status="ok", exit_code=0, lines=(profile,), message=reason
                )
            if op in ("assert_receipt", "host_mode"):
                path = Path(_need(request.receipt, "receipt"))
                try:
                    data = rc.read_receipt(path)
                except rc.ReceiptError as exc:
                    raise sf.RefusedError(str(exc)) from exc
                if op == "assert_receipt":
                    assertion = rc.assert_required_checks(
                        data,
                        profile,
                        expected_overlay_sha256=request.expected_overlay_sha256,
                    )
                    ran = len(assertion.required) - len(assertion.missing)
                    line = (
                        f"ledger-checks: ran {ran}/{len(assertion.required)} profile={profile}"
                        if assertion.ok
                        else f"ledger-checks: REFUSED profile={profile}: "
                        + "; ".join(assertion.findings)
                    )
                    return ModelSessionFrontDoorResult(
                        **base,
                        status="ok" if assertion.ok else "refused",
                        exit_code=0 if assertion.ok else 1,
                        receipt=str(path),
                        assertion=assertion,
                        lines=(line,),
                    )
                verdict = rc.decide_host_mode(
                    data,
                    profile=profile,
                    receipt_path=path,
                    claims=request.identity_claims,
                    scope=scope,
                    env=env,
                    max_age_seconds=request.receipt_max_age_seconds,
                    expected_overlay_sha256=request.expected_overlay_sha256,
                )
                return ModelSessionFrontDoorResult(
                    **base,
                    status="ok",
                    exit_code=0,
                    receipt=str(path),
                    host_mode=verdict,
                    lines=(verdict.mode,),
                    message="; ".join(verdict.reasons),
                )
            if op == "name":
                lane = sf.lane_name(request.purpose)
                return ModelSessionFrontDoorResult(
                    **base, status="ok", exit_code=0, lane=lane, lines=(lane,)
                )
            if op in ("claim_row", "status_row", "terminal_row"):
                if request.row is None:
                    raise sf.RefusedError("operation needs row")
                build = {
                    "claim_row": sf.claim_row,
                    "status_row": sf.status_row,
                    "terminal_row": sf.terminal_row,
                }[op]
                return ModelSessionFrontDoorResult(
                    **base, status="ok", exit_code=0, lines=(build(request.row),)
                )
            root = sf.sessions_root(request.sessions_root, env)
            if op == "init":
                if request.host_mode is None:
                    raise sf.RefusedError("operation init needs host_mode")
                lane = _need(request.lane, "lane")
                body = sf.session_env_body(
                    lane=lane,
                    claim_ts=_need(request.claim_ts, "claim_ts"),
                    goal_ticket=_need(request.ticket, "ticket"),
                    host_mode=request.host_mode,
                    scope=scope,
                    env=env,
                )
                lines, written = sf.init(root, lane, body, dry_run=request.dry_run)
                return ModelSessionFrontDoorResult(
                    **base,
                    status="ok",
                    exit_code=0,
                    lane=lane,
                    session_env=str(written) if written else None,
                    lines=tuple(lines),
                )
            if op == "show":
                return ModelSessionFrontDoorResult(
                    **base,
                    status="ok",
                    exit_code=0,
                    lines=tuple(sf.show(root, _need(request.lane, "lane"))),
                )
            if op == "lanes":
                names = sf.lanes(root, _need(request.lane, "lane"), request.state)
                return ModelSessionFrontDoorResult(
                    **base, status="ok", exit_code=0, lines=tuple(names)
                )
            if op == "boundary":
                lines = sf.boundary(
                    root, _need(request.lane, "lane"), _need(request.digest, "digest")
                )
                return ModelSessionFrontDoorResult(
                    **base, status="ok", exit_code=0, lines=tuple(lines)
                )
            if request.start is None:
                raise sf.RefusedError("operation start needs start")
            outcome = run_entry(
                request.start,
                claims=request.identity_claims,
                scope=scope,
                profile=profile,
                env=env,
                root=root,
                max_age_seconds=request.receipt_max_age_seconds,
                expected_overlay_sha256=request.expected_overlay_sha256,
            )
            return ModelSessionFrontDoorResult(
                **base,
                status=outcome.status,
                exit_code=outcome.exit_code,
                lane=outcome.lane,
                claim_ts=outcome.claim_ts,
                receipt=outcome.receipt,
                preflight_argv=outcome.preflight_argv,
                assertion=outcome.assertion,
                host_mode=outcome.host_mode,
                session_env=outcome.session_env,
                lines=tuple(outcome.lines),
                rule=outcome.rule,
                message=outcome.message,
                fix=outcome.fix,
            )
        except sf.RefusedError as exc:
            return ModelSessionFrontDoorResult(
                **base, status="refused", exit_code=exc.code, message=str(exc)
            )
