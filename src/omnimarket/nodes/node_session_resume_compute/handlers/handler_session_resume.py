# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session recovery over the rolling ledger, bound to the session's principals.

The session is bound first, through the team identity declaration (``omnimarket.session_scope``).
Then one operation runs:

* ``dangling``: every CLAIM no later row closes, each with the principal that wrote it and an
  ``owner`` of ``own`` or ``foreign``; the PRs they name; and the reconciler mode the session may
  run (``apply`` for an operator session outside a dry run, ``report`` otherwise and always for a
  member).
* ``close``: the TERMINAL that closes one stopped lane's CLAIM on its behalf.
* ``relaunch``: the relaunched lane's CLAIM template, or the landing-lane route.

A member session is refused ``close`` and ``relaunch`` (and so the landing notice the route leads
to) for any CLAIM that is not its own. Nothing here appends to the ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from omnimarket.session_scope import ModelSessionScope, bind_session_scope

from ..models.model_session_resume import (
    ModelSessionResumeRequest,
    ModelSessionResumeResult,
)
from .dangling import dangling, dangling_prs, read_ledger
from .rows import RefusalError, close_row, landing_route, relaunch_row, require_owned

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def reconciler_mode(
    scope: ModelSessionScope, *, dry_run: bool
) -> Literal["apply", "report"]:
    """``apply`` only for an operator session outside a dry run; a member reports, never applies."""
    return "apply" if scope.is_operator and not dry_run else "report"


class HandlerSessionResume:
    def handle(self, request: ModelSessionResumeRequest) -> ModelSessionResumeResult:
        scope = bind_session_scope(request.identity_claims, request.overlay_roots)
        base = {
            "operation": request.operation,
            "scope": scope,
            "reconciler_mode": reconciler_mode(scope, dry_run=request.dry_run),
        }
        ledger_path = Path(request.ledger_path)
        if not ledger_path.is_file():
            return ModelSessionResumeResult(
                **base, status="error", reason=f"ledger not found: {ledger_path}"
            )
        ledger = read_ledger(ledger_path)
        now = request.now or datetime.now(UTC).strftime(STAMP_FORMAT)
        if request.operation == "dangling":
            since = request.since or (
                datetime.strptime(now, STAMP_FORMAT).replace(tzinfo=UTC)
                - timedelta(days=7)
            ).strftime(STAMP_FORMAT)
            items, notes, summary = dangling(ledger, scope, since=since, t0=request.t0)
            prs, pr_notes = dangling_prs(items)
            return ModelSessionResumeResult(
                **base,
                status="ok",
                dangling=tuple(items),
                prs=tuple(prs),
                notes=tuple(notes + pr_notes),
                summary=summary,
            )
        try:
            if request.operation == "close":
                if request.close is None:
                    raise RefusalError("operation close needs a close spec")
                spec = request.close
                require_owned(scope, ledger, spec.dead_claim, spec.dead_lane)
                return ModelSessionResumeResult(
                    **base, status="ok", row=close_row(spec, now)
                )
            if request.relaunch is None:
                raise RefusalError("operation relaunch needs a relaunch spec")
            relaunch = request.relaunch
            require_owned(scope, ledger, relaunch.dead_claim, relaunch.dead_lane)
            route = landing_route(relaunch)
            if route:
                return ModelSessionResumeResult(
                    **base,
                    status="ok",
                    route="landing-lane",
                    reason=f"{route}; do not relaunch and do not close its CLAIM. Send "
                    f"{relaunch.landing_lane} one notice MSG (verdict landing-notified)",
                )
            return ModelSessionResumeResult(
                **base, status="ok", route="relaunch", row=relaunch_row(relaunch)
            )
        except RefusalError as exc:
            return ModelSessionResumeResult(**base, status="refused", reason=str(exc))
