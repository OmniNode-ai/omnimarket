# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge a lab-fill eval catalog: every eval names the readiness scenario it feeds (OMN-23178).

Pure: no file read, no clock. An eval whose `feeds` is missing or outside {R1 .. R9, experiment} is a defect
named by its id; an empty catalog is a defect too, so a catalog that failed to load cannot read as passing.
"""

from __future__ import annotations

from ..models import (
    EnumReadinessScenario,
    ModelEvalCatalogRequest,
    ModelEvalCatalogResult,
    ModelEvalFeedsDefect,
)

ALLOWED = tuple(s.value for s in EnumReadinessScenario)


class HandlerLabFillEvalCatalog:
    def handle(self, request: ModelEvalCatalogRequest) -> ModelEvalCatalogResult:
        defects: list[ModelEvalFeedsDefect] = []
        seen: set[str] = set()
        for decl in request.evals:
            if decl.id in seen:
                defects.append(
                    ModelEvalFeedsDefect(
                        eval_id=decl.id, feeds=decl.feeds, reason="duplicate eval id"
                    )
                )
            seen.add(decl.id)
            if decl.feeds is None:
                defects.append(
                    ModelEvalFeedsDefect(
                        eval_id=decl.id, feeds=None, reason="feeds is missing"
                    )
                )
            elif decl.feeds not in ALLOWED:
                defects.append(
                    ModelEvalFeedsDefect(
                        eval_id=decl.id,
                        feeds=decl.feeds,
                        reason=f"feeds {decl.feeds!r} is not one of {', '.join(ALLOWED)}",
                    )
                )
        if not request.evals:
            defects.append(
                ModelEvalFeedsDefect(
                    eval_id="", feeds=None, reason="the catalog lists no eval"
                )
            )
        ok = not defects
        summary = (
            f"{len(request.evals)} eval(s) checked; all declare an allowed feeds value"
            if ok
            else f"{len(request.evals)} eval(s) checked; {len(defects)} defect(s): "
            + "; ".join(f"{d.eval_id or '<catalog>'}: {d.reason}" for d in defects)
        )
        return ModelEvalCatalogResult(
            ok=ok,
            checked=len(request.evals),
            allowed=ALLOWED,
            defects=tuple(defects),
            summary=summary,
        )
