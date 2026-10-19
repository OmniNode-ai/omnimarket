# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mechanical occurrence accounting for friction-sweep handoffs.

Only enumerated (source, anchor) pairs count. Model-authored analysis may enrich
this output, but --verify refuses changes to the accounting before adjudication.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..models.model_friction_synthesis_request import ModelFrictionSynthesisRequest
from ..models.model_friction_synthesis_result import ModelFrictionSynthesisResult


def merge_handoffs(handoffs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Fold repeated harvests; unsupported assertions contribute zero."""
    candidates: dict[str, dict[str, Any]] = {}
    anchors: dict[str, set[tuple[str, str]]] = {}
    unproven: list[dict[str, Any]] = []
    unknown: list[str] = []
    for index, handoff in enumerate(handoffs):
        source_id = handoff.get("source_id", f"handoff-{index + 1}")
        if handoff.get("verdict") == "UNKNOWN":
            unknown.append(f"{source_id}: {handoff.get('freshness', 'UNKNOWN')}")
            continue
        for candidate in handoff["candidates"]:
            fingerprint = candidate["fingerprint"]
            if not isinstance(fingerprint, str) or not fingerprint.strip():
                raise ValueError("candidate fingerprint must be a nonempty string")
            evidence = candidate.get("evidence", [])
            if not isinstance(evidence, list):
                raise ValueError(f"{fingerprint}: evidence must be an array")
            valid = [
                entry
                for entry in evidence
                if isinstance(entry, dict)
                and all(
                    isinstance(entry.get(key), str) and entry[key].strip()
                    for key in ("source", "anchor")
                )
            ]
            asserted = candidate.get("occurrences_this_run")
            if (
                type(asserted) is not int
                or asserted < 0
                or asserted != len(evidence)
                or len(valid) != len(evidence)
            ):
                unproven.append(
                    {
                        "verdict": "UNPROVEN",
                        "source_id": source_id,
                        "fingerprint": fingerprint,
                        "asserted_count": asserted,
                        "enumerated_count": len(valid),
                        "unsupported_contribution": 0,
                        "reason": (
                            "count must equal evidence.length; every entry needs source and anchor"
                        ),
                    }
                )
            if fingerprint not in candidates:
                candidates[fingerprint] = {
                    **candidate,
                    "evidence": [],
                    "sources": [],
                    "occurrences_this_run": 0,
                }
                anchors[fingerprint] = set()
            merged = candidates[fingerprint]
            if source_id not in merged["sources"]:
                merged["sources"].append(source_id)
            for entry in valid:
                key = (entry["source"], entry["anchor"])
                if key not in anchors[fingerprint]:
                    anchors[fingerprint].add(key)
                    merged["evidence"].append(dict(entry))
            merged["occurrences_this_run"] = len(merged["evidence"])
    return {
        "candidates": list(candidates.values()),
        "unproven": unproven,
        "sources_unknown": unknown,
    }


def verify_accounting(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    """Permit analysis enrichment, refuse changes to evidence/counts/refusals."""
    if (
        actual.get("unproven") != expected["unproven"]
        or actual.get("sources_unknown") != expected["sources_unknown"]
    ):
        raise ValueError("UNPROVEN: refusal or UNKNOWN notes changed")
    incoming = actual.get("candidates")
    if not isinstance(incoming, list) or len(incoming) != len(expected["candidates"]):
        raise ValueError("UNPROVEN: candidate set changed")
    by_fingerprint = {entry["fingerprint"]: entry for entry in incoming}
    if len(by_fingerprint) != len(incoming):
        raise ValueError("UNPROVEN: duplicate fingerprint in merged handoff")
    for candidate in expected["candidates"]:
        observed = by_fingerprint.get(candidate["fingerprint"], {})
        for key in ("occurrences_this_run", "evidence", "sources"):
            if observed.get(key) != candidate[key] or type(
                observed.get(key)
            ) is not type(candidate[key]):
                raise ValueError(f"UNPROVEN: {candidate['fingerprint']} {key} changed")


class HandlerFrictionSynthesis:
    """Read and write host-local handoffs behind the contract's bus route."""

    def handle(
        self, request: ModelFrictionSynthesisRequest
    ) -> ModelFrictionSynthesisResult:
        stderr = ""
        try:
            expected = merge_handoffs(
                [json.loads(path.read_text()) for path in request.handoffs]
            )
            stderr = "".join(
                json.dumps(note, sort_keys=True) + "\n" for note in expected["unproven"]
            )
            if request.verify is not None:
                verify_accounting(expected, json.loads(request.verify.read_text()))
            elif request.output is not None:
                request.output.write_text(json.dumps(expected, indent=2) + "\n")
        except (OSError, ValueError, KeyError, TypeError) as error:
            return ModelFrictionSynthesisResult(
                correlation_id=request.correlation_id,
                exit_code=1,
                stderr=stderr + f"UNPROVEN: synthesis refused: {error}\n",
            )
        return ModelFrictionSynthesisResult(
            correlation_id=request.correlation_id, exit_code=0, stderr=stderr
        )
