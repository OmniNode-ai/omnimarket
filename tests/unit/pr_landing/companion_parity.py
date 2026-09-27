# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compute-versus-emitter parity harness for OCC companions (OMN-19827, T5 AC3).

Each fixture under ``tests/fixtures/pr_landing/companion_parity/`` holds one
companion merged into onex_change_control on 2026-09-26: the request the
producer's inputs reconstruct (``request``), the files the producer committed
(``committed_files``, read by git from the producer's own commits), and the
provenance of every input.

The harness renders each request through ``node_occ_companion_compute`` and
compares the deterministic subset of every file (the observed facts -- run
timestamp, probe command and output, exit code, created_at -- projected out by
the compute node's own projection) against what the producer committed. The
verdict per companion is ``byte-equal``, ``differs`` with the list of differing
files, or ``error`` when the compute refused the request.

A difference is a finding, never a failure: it is the measurement that decides
when the derivation can move from the emitter to the compute path (OMN-15192).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from omnimarket.events.occ_companion import ModelOccCompanionRequest
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    _strip_observed_facts,
    compute_companion_plan,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "pr_landing"
    / "companion_parity"
)

Verdict = Literal["byte-equal", "differs", "error"]


@dataclass(frozen=True)
class CompanionParity:
    """The parity verdict for one recorded companion."""

    occ_pr: int
    producer: str
    product: str
    verdict: Verdict
    differing_files: tuple[str, ...] = ()
    error: str = ""
    compared_files: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, object]:
        return {
            "occ_pr": self.occ_pr,
            "producer": self.producer,
            "product": self.product,
            "verdict": self.verdict,
            "differing_files": list(self.differing_files),
            "error": self.error,
            "compared_files": len(self.compared_files),
        }


def load_fixtures() -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE_DIR.glob("occ_*.json"))
    ]


def parity_of(fixture: dict[str, object]) -> CompanionParity:
    """Render one fixture's request and diff it against the committed files."""
    product = fixture["product"]
    assert isinstance(product, dict)
    label = f"{product['repository']}#{product['pr_number']}"
    occ_pr = int(str(fixture["occ_pr"]))
    producer = str(fixture["producer"])
    committed = fixture["committed_files"]
    assert isinstance(committed, dict)
    try:
        request = ModelOccCompanionRequest.model_validate(fixture["request"])
        plan = compute_companion_plan(request)
    except (ValueError, TypeError) as exc:
        return CompanionParity(
            occ_pr=occ_pr,
            producer=producer,
            product=label,
            verdict="error",
            error=f"{type(exc).__name__}: {exc}",
        )
    rendered = {f.path: f.content for f in plan.companion_files}
    paths = tuple(sorted(set(rendered) | set(committed)))
    differing = tuple(
        path
        for path in paths
        if path not in rendered
        or path not in committed
        or _strip_observed_facts(rendered[path])
        != _strip_observed_facts(str(committed[path]))
    )
    return CompanionParity(
        occ_pr=occ_pr,
        producer=producer,
        product=label,
        verdict="differs" if differing else "byte-equal",
        differing_files=differing,
        compared_files=paths,
    )


def render_report(results: list[CompanionParity]) -> str:
    lines = ["companion parity (compute vs committed, deterministic subset):"]
    for result in results:
        head = (
            f"  OCC#{result.occ_pr} {result.product} producer={result.producer} "
            f"verdict={result.verdict}"
        )
        lines.append(head)
        lines.extend(f"    differs: {path}" for path in result.differing_files)
        if result.error:
            lines.append(f"    error: {result.error}")
    return "\n".join(lines)
