# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A burned-down baseline may not come back (OMN-18013).

WHY THIS EXISTS
---------------
Every ratchet in this repo is a file listing (contract, topic) pairs that are allowed to
be broken. A ratchet is the right shape while a backlog is being burned down and the
wrong shape the moment it is empty: an empty-but-present baseline is a one-line edit away
from re-authorizing the exact defect class it was created to remove, and that edit looks
like a normal diff.

So the burn-down ends by DELETING the file, and this gate is what makes the deletion
stick. It is a pure filesystem assertion with no scan, no derivation and no baseline of
its own -- there is nothing here to soften.

WHAT IT ENFORCES
----------------
1. Every path in :data:`DELETED_BASELINES` is ABSENT.
2. Every path in :data:`PEER_FENCED_BASELINES` contains ONLY the exact rows it is pinned
   to. These are the rows another lane owns (see the file's own header) -- the gate holds
   them to that list so the file cannot quietly grow back into a general exemption list
   while it waits for that lane, and names the ticket that deletes it.

There is no flag to skip either check.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

# Burned to zero and deleted by OMN-18013. Re-creating any of these re-opens the defect
# class; the fix for a new violation is always the contract, never the list.
DELETED_BASELINES: tuple[tuple[str, str], ...] = (
    (
        "src/omnimarket/validators/data/contract_topic_graph_baseline.yaml",
        "the contract-topic-graph ratchet: 688 accepted defects plus the "
        "external_producers / external_consumers maps. Burned to zero by OMN-18013 -- "
        "the graph gate is now HARD --scope with no baseline at all, and external "
        "producers/consumers are declared IN the contract that consumes or publishes "
        "them (externally_produced_topics / externally_consumed_topics)",
    ),
    (
        "src/omnimarket/validators/data/contract_topic_graph_orphan_classification.yaml",
        "a generated triage artifact that its own header says the gate never reads; "
        "keeping it invites treating a classification as an exemption",
    ),
    # OMN-17888. Its own header set this exit condition -- deleted the moment its single
    # row lands -- and that row (node_redeploy_deploy_effect / HandlerDeployPublishMonitor)
    # has landed: the contract routes by topic with one explicit message_category per
    # entry. Neither the pre-commit hook nor the CI job ever passed --baseline, and the
    # mixed-category gate reads OK over 403 contracts with the file gone, so what the file
    # carried was an exemption with nothing left to exempt.
    (
        "config/validation/mixed_category_routing_omnimarket_baseline.yaml",
        "the omnimarket mixed-category routing ratchet: burned from 22 entries to one, "
        "and that last entry is fixed. An exemption file holding zero live rows is a "
        "one-line edit away from re-authorizing the class it was created to remove",
    ),
)

# Baselines that still exist because every remaining row is owned elsewhere -- by another
# lane, or by a node refactor outside this ticket's five gate items. Each maps to the exact
# (contract, topic-or-handler) pairs it is allowed to carry and the ticket that deletes it.
# The topic literals below are the PIN itself: reading them out of the file they pin would
# defeat the pin, so they are spelled here deliberately.
PEER_FENCED_BASELINES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "config/validation/subscriber_dispatcher_resolution_baseline.yaml": (
        "OMN-16939 / OMN-17888 (lane dev-lane-fsm-residuals)",
        (
            # OMN-17888: the node_redeploy_deploy_effect row is gone. It recorded a
            # completion-event topic that resolved cleanly yet still dead-lettered,
            # because the routing entry declared the COMMAND model for an EVENT. That
            # entry now declares the deploy agent's own completion model and the handler
            # branches on it, so a pin allowing the row would license re-freezing a
            # residual that is closed. Asserted directly instead, in
            # tests/test_deploy_publish_monitor_rebuild_completed_dispatch.py, and
            # enforced repo-wide by omnimarket.validators.routing_input_model_fit.
            # OMN-17296 AC2: the four node_redeploy_orchestrator rows are gone. Their
            # subscriptions were deleted from that contract by omnimarket#2375
            # (OMN-18026), so a pin allowing them would license re-freezing a row about
            # a subscription that does not exist. The drop is now asserted directly, in
            # tests/test_redeploy_orchestrator_dispatch_resolution.py, against the
            # handler's own parsed branch set.
            # Not peer-owned: node_e2e_orchestrator needs a handler class extracted from
            # its standalone consumer.py before either topic can get a route. A node
            # refactor, not a category or alias fix -- residual on OMN-18013.
            (
                "node_e2e_orchestrator",
                "onex.evt.omnimarket.build-loop-orchestrator-completed.v1",  # onex-topic-allow: pinned baseline row
            ),
            (
                "node_e2e_orchestrator",
                "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1",  # onex-topic-allow: pinned baseline row
            ),
        ),
    ),
    # OMN-17888: config/validation/mixed_category_routing_omnimarket_baseline.yaml is
    # DELETED, which is the exit condition that file wrote for itself -- "deleted the
    # moment that row lands", the row being this lane's node_redeploy_deploy_effect
    # entry. Neither the hook nor the CI job ever passed --baseline, and the gate reads
    # OK at 403 contracts with the file gone.
}


def _rows(path: Path) -> tuple[tuple[str, str], ...]:
    """Every (contract, topic-or-entry) pair a baseline file declares."""
    data = yaml.safe_load(path.read_text(errors="replace")) or {}
    if not isinstance(data, dict):
        return ()
    out: list[tuple[str, str]] = []
    for value in data.values():
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            contract = str(row.get("contract", ""))
            key = str(row.get("topic") or row.get("handler") or "")
            out.append((contract, key))
    return tuple(sorted(out))


def check(repo_root: Path) -> list[str]:
    """Every violation, as a human-readable line. Empty means the gate passes."""
    problems: list[str] = []

    for relative, why in DELETED_BASELINES:
        path = repo_root / relative
        if path.exists():
            problems.append(
                f"{relative} is BACK. It was burned to zero and deleted by OMN-18013 "
                f"and must not be re-created -- {why}."
            )

    for relative, (owner, pinned) in PEER_FENCED_BASELINES.items():
        path = repo_root / relative
        if not path.is_file():
            problems.append(
                f"{relative} is missing. If {owner} landed its rows, delete this entry "
                f"from PEER_FENCED_BASELINES and drop --baseline from the gate's "
                f"invocation in the same change -- do not leave a dangling pin."
            )
            continue
        actual = _rows(path)
        expected = tuple(sorted(pinned))
        for row in sorted(set(actual) - set(expected)):
            problems.append(
                f"{relative} carries a row that is NOT peer-fenced: "
                f"{row[0]} :: {row[1]}. This baseline is pinned to the rows owned by "
                f"{owner} and may not grow. Fix the contract instead."
            )
        for row in sorted(set(expected) - set(actual)):
            problems.append(
                f"{relative} no longer carries the peer-fenced row {row[0]} :: "
                f"{row[1]}. If {owner} fixed it, remove the row from "
                f"PEER_FENCED_BASELINES here too, and delete the file entirely once the "
                f"last row is gone."
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path.cwd()
    problems = check(root)
    if problems:
        sys.stderr.write(
            "[no-baseline-refreeze] FAIL: a burned-down baseline was re-created or a "
            "peer-fenced baseline grew (OMN-18013):\n"
        )
        for line in problems:
            sys.stderr.write(f"  - {line}\n")
        return 1
    sys.stderr.write(
        f"[no-baseline-refreeze] OK: {len(DELETED_BASELINES)} deleted baseline(s) still "
        f"absent, {len(PEER_FENCED_BASELINES)} peer-fenced baseline(s) hold only their "
        f"pinned rows.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
