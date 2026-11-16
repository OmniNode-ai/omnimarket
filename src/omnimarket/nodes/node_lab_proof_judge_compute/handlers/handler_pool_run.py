# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPoolRun: read the pool driver's clone and probe text, then judge changed-path-live.

Definition-B compute: ``handle(request: ModelPoolRunRequest) -> ModelChangedPathLiveResult``. The pull request under proof
and the paths it changes are the clone phase's ``pr-subject`` and ``pr-changed`` lines; a run that names no single pull
request has no changed-path check. The rule itself is HandlerChangedPathLive's.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_changed_path_live import (
    HandlerChangedPathLive,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.enum_judge_verdict import (
    EnumJudgeVerdict,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_changed_path_live import (
    ModelChangedPathLiveRequest,
    ModelChangedPathLiveResult,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_pool_run import (
    ModelPoolRunRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_substitute_receipt import (
    ModelSubstituteReceipt,
)

ReceiptsForHead = Callable[[str, str], tuple[ModelSubstituteReceipt, ...]]

_SUBJECT = re.compile(r"^pr-subject (\S+)#(\d+) head=([0-9a-f]{40}) ", re.M)
_CHANGED = re.compile(r"^pr-changed (\S+)#(\d+) (\S.*)$", re.M)


class HandlerPoolRun:
    """Judge changed-path-live from the pool driver's phase text: pure, no I/O, no clock."""

    def __init__(
        self, judge: HandlerChangedPathLive, receipts_for_head: ReceiptsForHead
    ) -> None:
        self._judge = judge
        self._receipts_for_head = receipts_for_head

    def handle(self, request: ModelPoolRunRequest) -> ModelChangedPathLiveResult:
        """Answer for the one pull request the clone phase names; NO_CHECK for any other run."""
        subjects = _SUBJECT.findall(request.clone)
        if len(subjects) != 1:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.NO_CHECK,
                ok=False,
                evidence=f"the clone phase names {len(subjects)} pull requests, not one",
            )
        bare, number, head = subjects[0]
        repo = self._judge.qualify(bare)
        paths = tuple(
            path.strip()
            for r, n, path in _CHANGED.findall(request.clone)
            if (r, n) == (bare, number)
        )
        if not paths:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.NO_CHECK,
                ok=False,
                evidence="the clone phase printed no changed path for the pull request",
            )
        return self._judge.handle(
            ModelChangedPathLiveRequest(
                repo=repo,
                pr_number=int(number),
                head=head,
                changed_paths=paths,
                probe=request.probe,
                substitute_receipts=self._receipts_for_head(repo, head),
            )
        )
