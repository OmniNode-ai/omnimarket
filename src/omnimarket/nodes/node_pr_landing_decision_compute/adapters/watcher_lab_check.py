# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the live J8 run's real pull requests come from: the PR watcher's state (OMN-23087).

The collector turns each open pull request of a lab-bound repository, with the check runs the watcher read on its head, into
the facts the landing decision judges: the PR's head, its CI and merge state, and a ``lab_check`` fact built from the watcher's
runs of the overlay's lab-bound check. Nothing is read from GitHub; the watcher's state is the read.

The attempt identity: the dispatch, order and overlay identity of J7 is not posted on a check run yet, so the identity of an
attempt here is the head sha the watcher read the check runs on. A run read on another head is not on this head; it reads as
missing. The join's own superseded case (a run of an earlier attempt on this very head) cannot arise from this source until
J7 posts an identity; the injected control and the unit tests carry it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from omnimarket.nodes.node_pr_landing_decision_compute.lab_check import (
    read_lab_check,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8_live import (
    EnumJ8Break,
    ModelJ8Subject,
    ModelJoinJ8LiveOverlay,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    SHA_PATTERN,
)

_MERGE_STATES = {"clean", "behind", "conflicting", "unknown", "blocked"}
_FORGED_ATTEMPT = "forged-by-injected-break"


class WatcherStateError(ValueError):
    """The watcher's state cannot be judged: stale, unparseable or not the shape the watcher writes."""


class ProtocolJ8Subjects(Protocol):
    def subjects(self, pr: str | None) -> tuple[ModelJ8Subject, ...]: ...


def _stamp(text: object, what: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise WatcherStateError(f"watcher {what} is not a timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise WatcherStateError(f"watcher {what} carries no zone: {text!r}")
    return parsed


class WatcherLabCheckCollector:
    def __init__(
        self,
        state: Mapping[str, Any],
        overlay: ModelJoinJ8LiveOverlay,
        now: datetime,
        inject: EnumJ8Break | None = None,
    ) -> None:
        self._overlay = overlay
        self._now = now
        self._inject = inject
        prs = state.get("prs")
        if not isinstance(prs, dict):
            raise WatcherStateError("watcher state holds no prs table")
        last_tick = state.get("last_tick")
        if last_tick is None:
            raise WatcherStateError("watcher state holds no last_tick")
        age = (now - _stamp(last_tick, "last_tick")).total_seconds()
        if age > overlay.watcher_max_age_seconds:
            raise WatcherStateError(
                f"watcher state is {int(age)} s old, past the {overlay.watcher_max_age_seconds} s bound"
            )
        self._prs: dict[str, Any] = prs

    def subjects(self, pr: str | None = None) -> tuple[ModelJ8Subject, ...]:
        found: list[tuple[str, ModelJ8Subject]] = []
        for key, record in self._prs.items():
            subject = self._subject(key, record)
            if subject is None or (pr is not None and subject.pr != pr):
                continue
            found.append((str(record["facts"].get("updated_at", "")), subject))
        found.sort(key=lambda row: (row[0], row[1].pr), reverse=True)
        return tuple(s for _, s in found[: self._overlay.max_candidates])

    def _subject(self, key: str, record: Mapping[str, Any]) -> ModelJ8Subject | None:
        facts = record.get("facts") or {}
        repo = facts.get("repo")
        head = facts.get("head_sha")
        if (
            repo not in self._overlay.lab_bound_repos
            or str(facts.get("state", "")).upper() != "OPEN"
            or not isinstance(head, str)
            or not re.fullmatch(SHA_PATTERN, head)
        ):
            return None
        ci = record.get("ci") or {}
        pr_key = f"{self._overlay.owner}/{repo}#{facts['number']}"
        review = record.get("review") or {}
        merge_state = str(review.get("merge_state") or "").lower()
        watcher_ci = str(ci.get("verdict") or "").upper()
        row: dict[str, Any] = {
            "pr": pr_key,
            "head_sha": head,
            "state": "open",
            "ci": {"GREEN": "green", "RED": "red"}.get(watcher_ci, "pending"),
            "merge_state": merge_state if merge_state in _MERGE_STATES else "unknown",
            "created_at": facts["created_at"],
        }
        if watcher_ci == "RED":
            # The watcher does not classify a red head; the join never reads the class (a red head holds either way).
            row["red_class"] = "product"
        if facts.get("draft") is True:
            row["suspensions"] = ["draft"]
        real = self._lab_check(ci, head)
        poster_facts = self._facts({**row, "lab_check": real})
        verdict = read_lab_check(poster_facts.prs[0], self._now, poster_facts.policy)
        if verdict is None:
            raise WatcherStateError(f"{pr_key} carries no lab_check fact")
        decision_check = real
        if self._inject is EnumJ8Break.BLIND_DECISION:
            decision_check = {
                **real,
                "current_attempt_id": _FORGED_ATTEMPT,
                "runs": [
                    {
                        "name": self._overlay.lab_check_name,
                        "head_sha": head,
                        "attempt_id": _FORGED_ATTEMPT,
                        "conclusion": "success",
                    }
                ],
            }
        return ModelJ8Subject(
            pr=pr_key,
            head_sha=head,
            facts=self._facts({**row, "lab_check": decision_check}),
            poster_facts=poster_facts,
            verdict=verdict.verdict,
        )

    def _lab_check(self, ci: Mapping[str, Any], head: str) -> dict[str, Any]:
        read_on = ci.get("sha")
        runs: list[dict[str, Any]] = []
        if isinstance(read_on, str) and re.fullmatch(SHA_PATTERN, read_on):
            for name, status, conclusion, _at in ci.get("runs") or ():
                if name != self._overlay.lab_check_name:
                    continue
                if status != "completed":
                    outcome = "pending"
                elif conclusion == "success":
                    outcome = "success"
                else:
                    outcome = "failure"
                runs.append(
                    {
                        "name": name,
                        "head_sha": read_on,
                        "attempt_id": read_on,
                        "conclusion": outcome,
                    }
                )
        return {
            "check_name": self._overlay.lab_check_name,
            "current_attempt_id": head,
            "runs": runs,
        }

    def _facts(self, row: dict[str, Any]) -> ModelLandingFacts:
        return ModelLandingFacts.model_validate(
            {
                "tick": 1,
                "observed_at": self._now.isoformat(),
                "prs": [row],
                "state": {"last_tick": 0, "records": []},
            }
        )


__all__ = ["ProtocolJ8Subjects", "WatcherLabCheckCollector", "WatcherStateError"]
