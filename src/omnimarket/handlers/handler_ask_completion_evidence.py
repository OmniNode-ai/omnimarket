# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read checkable completion facts; unreadable or stale sources cannot complete an item."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.handler_operator_ledger import parse_ledger_row
from omnimarket.models.model_ask_state import (
    ModelAskCompletionCheck,
    ModelAskStateRequest,
)
from omnimarket.models.model_ask_state_transition import (
    ModelAskCompletionReceipt,
    ModelAskCompletionSources,
    ModelAskPrWatcherSnapshot,
    ModelAskStateTransition,
)

_PR = re.compile(r"(?:[\w.-]+/)?(?P<repo>[\w.-]+)#(?P<number>\d+)")
_PR_URL = re.compile(
    r"https://github\.com/[\w.-]+/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)"
)


class FileCompletionChecker:
    """Production adapter over watcher facts, scoped receipt JSON and real ruling rows."""

    def __init__(self, sources: ModelAskCompletionSources) -> None:
        self._sources = sources

    def _merged_pr(self, reference: str, now: datetime) -> bool:
        match = _PR.fullmatch(reference) or _PR_URL.fullmatch(reference)
        path = self._sources.watcher_state_path
        if match is None or path is None:
            return False
        try:
            watcher = ModelAskPrWatcherSnapshot.model_validate_json(path.read_text())
        except (OSError, ValueError, ValidationError):
            return False
        age = (now - watcher.last_tick).total_seconds()
        if age < 0 or age > self._sources.max_age_seconds:
            return False
        record = watcher.prs.get(f"{match['repo']}#{match['number']}")
        if (
            record is None
            or record.facts.state != "MERGED"
            or not record.facts.merged_at
        ):
            return False
        try:
            merged_at = datetime.fromisoformat(
                record.facts.merged_at.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        return merged_at.tzinfo is not None and merged_at <= now

    def _receipt(
        self, reference: str, revision: str, run: str, outcomes: tuple[str, ...]
    ) -> bool:
        root = self._sources.receipt_root
        if not reference.startswith("receipt:") or root is None:
            return False
        path = (root / reference.removeprefix("receipt:")).resolve()
        if not path.is_relative_to(root.resolve()):
            return False
        try:
            receipt = ModelAskCompletionReceipt.model_validate_json(path.read_text())
        except (OSError, ValueError, ValidationError):
            return False
        return (
            receipt.revision == revision
            and receipt.run_id == run
            and receipt.outcome in outcomes
        )

    def _ruling(self, reference: str, snapshot: ModelAskStateRequest) -> bool:
        path = self._sources.ruling_ledger_path
        if not reference.startswith("RULING:") or path is None:
            return False
        stamp = reference.removeprefix("RULING:")
        try:
            rows = path.read_text().splitlines()
        except OSError:
            return False
        return any(
            parsed[0] == stamp
            and parsed[1] in snapshot.config.ruling_row_kinds
            and datetime.fromisoformat(parsed[0].replace("Z", "+00:00")) <= snapshot.now
            for raw in rows
            if (parsed := parse_ledger_row(raw)) is not None
        )

    def checks(
        self,
        snapshot: ModelAskStateRequest,
        transitions: tuple[ModelAskStateTransition, ...] = (),
    ) -> tuple[ModelAskCompletionCheck, ...]:
        policy = snapshot.config.transition
        candidates = {
            (change.evidence, change.revision, change.run_id)
            for change in transitions
            if change.evidence
        }
        for raw in snapshot.rows:
            parsed = parse_ledger_row(raw)
            if parsed is None:
                continue
            _, _, _, cells = parsed
            evidence = cells.get(policy.evidence_cell)
            revision = cells.get(policy.revision_cell)
            run = cells.get(policy.run_cell)
            if evidence and revision and run:
                candidates.add((evidence, revision, run))
        checks: list[ModelAskCompletionCheck] = []
        for reference, revision, run in sorted(candidates):
            evidence_class = "merged-pr"
            if reference.startswith("receipt:"):
                evidence_class = "receipt"
                verified = self._receipt(
                    reference, revision, run, snapshot.config.done_outcomes
                )
            elif reference.startswith("RULING:"):
                evidence_class = "ruling"
                verified = self._ruling(reference, snapshot)
            else:
                verified = self._merged_pr(reference, snapshot.now)
            checks.append(
                ModelAskCompletionCheck(
                    reference=reference,
                    evidence_class=evidence_class,
                    verified=verified,
                    revision=revision,
                    run_id=run,
                )
            )
        return tuple(checks)


class OverlayCompletionChecker:
    """The same file adapter, with its sources read from the node's deployment overlay."""

    def __init__(self, workspace_root_env: str, node_name: str) -> None:
        self._workspace_root_env = workspace_root_env
        self._node_name = node_name

    def checks(
        self,
        snapshot: ModelAskStateRequest,
        transitions: tuple[ModelAskStateTransition, ...] = (),
    ) -> tuple[ModelAskCompletionCheck, ...]:
        import os

        from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
            node_overlay_path,
        )

        root = os.environ.get(self._workspace_root_env)
        path = node_overlay_path(self._node_name, Path(root) if root else None)
        if path is None:
            raise RuntimeError("item transition completion_sources overlay is absent")
        overlay = yaml.safe_load(path.read_text())
        sources = ModelAskCompletionSources.model_validate(
            overlay["completion_sources"]
        )
        return FileCompletionChecker(sources).checks(snapshot, transitions)
