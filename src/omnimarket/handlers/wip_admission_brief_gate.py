# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote-lane runner's seam to the WIP admission.

Before a lane is placed, the runner's brief gate asks ``dispatch_wip_refusal``. A lane that works
a named PR, or whose name carries a fix-forward prefix, is never refused. Any other lane that may
open a PR is new work and is refused while the started-but-unlanded count is at its limit. The
limits live in the ``wip_admission`` overlay, which only the launching host carries: a host with
no overlay judges nothing, and an overlay whose watcher state or ledger cannot be read refuses
new work and names why, because an unread count is not a count of zero.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from string import Template

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_wip_admission import (
    build_wip_read,
    claims_from_ledger,
    decide_wip_admission,
)
from omnimarket.handlers.wip_admission_overlay import (
    load_wip_admission_config,
    load_wip_admission_policy,
)
from omnimarket.models.wip_admission import (
    EnumWipFixClass,
    EnumWipOutcome,
    EnumWipWorkKind,
    ModelWipAdmissionRequest,
    ModelWipFix,
    ModelWipRead,
)

LEDGER_ENV = "ONEX_LEDGER_PATH"
_PR_REF = re.compile(r"(?:[\w.-]+/)?(?P<repo>[\w.-]+)#(?P<n>\d+)$")


def _target(pr: str) -> str:
    match = _PR_REF.match(pr.strip())
    return f"{match['repo']}#{match['n']}" if match else pr.strip()


def dispatch_wip_refusal(
    *,
    lane: str,
    repo: str,
    ticket: str,
    prs: Sequence[str],
    environ: Mapping[str, str],
    now: datetime | None = None,
) -> str | None:
    """Why this dispatch must not start now, or ``None`` to go on. ``now`` is the caller's clock read."""
    try:
        policy = load_wip_admission_policy(environ)
    except NodeOverlayError:
        return None
    if any(lane.startswith(p) for p in policy.fix_forward_lane_prefixes):
        return None
    config = load_wip_admission_config()
    stamp = now or datetime.now(UTC)
    problems: list[str] = []
    state = None
    try:
        # The overlay names the file with ${NAME} from the environment, so it holds no home path.
        where = Template(policy.watcher_state_path).substitute(environ)
        state = json.loads(Path(where).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        problems.append(f"watcher state unread: {type(exc).__name__}")
    claims = []
    try:
        claims = claims_from_ledger(
            Path(environ.get(LEDGER_ENV, "")).read_text(encoding="utf-8"), stamp, config
        )
    except OSError as exc:
        problems.append(f"ledger unread: {type(exc).__name__}")
    read = (
        ModelWipRead(readable=False, unreadable_reason="; ".join(problems))
        if problems
        else build_wip_read(
            pr_state=state, claims=claims, ticket_states=None, config=config
        )
    )
    fix = (
        ModelWipFix(fix_class=EnumWipFixClass.LANDING_BLOCKER, target=_target(prs[0]))
        if prs
        else None
    )
    decision = decide_wip_admission(
        ModelWipAdmissionRequest(
            kind=EnumWipWorkKind.FIX_FORWARD if prs else EnumWipWorkKind.NEW_WORK,
            repo=repo,
            ticket=ticket,
            now=stamp,
            read=read,
            policy=policy,
            config=config,
            fix=fix,
        )
    )
    if decision.outcome is EnumWipOutcome.ADMIT:
        return None
    why = f" ({read.unreadable_reason})" if read.unreadable_reason else ""
    return f"REMOTE-LANE-WIP-REFUSED lane={lane} {decision.line}{why}"
