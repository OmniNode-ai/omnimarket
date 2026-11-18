# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the graph, admitted exemplars, and committed test evidence for a lane."""

from __future__ import annotations

import json
import re
from uuid import uuid4

import yaml
from pydantic import TypeAdapter, ValidationError

from omnimarket.models.context_pack_orchestrator.model_lane_brief_context import (
    ModelLaneBriefContextRequest,
    ModelLaneBriefExemplarCandidate,
)
from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    ModelCodeGraphLookupCommand,
)
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import answer_lookup
from omnimarket.nodes.node_context_source_repo_doctrine_effect import (
    HandlerContextSourceRepoDoctrine,
)
from omnimarket.nodes.node_exemplar_registry_reconcile_compute import (
    ModelExemplarRegistryRow,
)

_REGISTRY_PATH = "config/context/exemplar_registry.json"
_MAX_CANDIDATES = 8
_MAX_CANDIDATE_CHARS = 8_000


class LaneBriefSources:
    def __init__(
        self, doctrine: HandlerContextSourceRepoDoctrine | None = None
    ) -> None:
        self._doctrine = doctrine or HandlerContextSourceRepoDoctrine()

    def exemplars(
        self,
        request: ModelLaneBriefContextRequest,
    ) -> tuple[tuple[ModelLaneBriefExemplarCandidate, ...], str | None]:
        if request.exemplar_candidates:
            return request.exemplar_candidates, None
        repo = request.repository.rsplit("/", 1)[-1]
        raw = self._doctrine.read_committed(repo, _REGISTRY_PATH)
        if raw is None:
            return (), "committed exemplar registry is absent or unreadable"
        try:
            rows = TypeAdapter(tuple[ModelExemplarRegistryRow, ...]).validate_python(
                json.loads(raw)
            )
        except (ValidationError, ValueError):
            return (), "committed exemplar registry is invalid"

        candidates: list[ModelLaneBriefExemplarCandidate] = []
        for row in rows:
            if row.lifecycle != "active" or not re.fullmatch(
                r"node_[a-z0-9_]+", row.node_id
            ):
                continue
            root = f"src/{repo.replace('-', '_')}/nodes/{row.node_id}"
            contract = self._doctrine.read_committed(
                repo, f"{root}/contract.yaml", revision=row.commit
            )
            try:
                node = yaml.safe_load(contract) if contract else None
            except yaml.YAMLError:
                continue
            descriptor = node.get("descriptor") if isinstance(node, dict) else None
            if not isinstance(descriptor, dict) or (
                request.node_archetype
                and descriptor.get("node_archetype") != request.node_archetype
            ):
                continue
            if contract and len(contract) <= _MAX_CANDIDATE_CHARS:
                candidates.append(
                    ModelLaneBriefExemplarCandidate(
                        path=f"{root}/contract.yaml", text=contract
                    )
                )
            if len(candidates) == _MAX_CANDIDATES:
                break
        return tuple(
            candidates
        ), None if candidates else "no admitted exemplars match the lane archetype"

    @staticmethod
    def graph(request: ModelLaneBriefContextRequest) -> tuple[str, str | None]:
        if not request.files_to_touch:
            return "", None
        result = answer_lookup(
            ModelCodeGraphLookupCommand(
                correlation_id=uuid4().hex,
                question=EnumCodeGraphQuestion.REACH,
                repo=request.repository.rsplit("/", 1)[-1],
                paths=request.files_to_touch,
                as_of=request.revision,
            )
        )
        if result.status == "failed" or result.nearest:
            return (
                "",
                result.reason or "code graph has no version for the requested revision",
            )
        return "\n".join(f"- {match.path}" for match in result.matches), None

    def tests_and_ci(
        self, request: ModelLaneBriefContextRequest
    ) -> tuple[str, str | None]:
        repo = request.repository.rsplit("/", 1)[-1]
        contract = (
            self._doctrine.read_committed(
                repo,
                f"contracts/{request.ticket}.yaml",
                revision=request.revision,
            )
            if re.fullmatch(r"OMN-[0-9]+", request.ticket)
            else None
        )
        lines: list[str] = []
        reasons: list[str] = []
        if request.ticket_criteria:
            lines.extend(("### Ticket criteria", *request.ticket_criteria))
        if contract:
            try:
                work = yaml.safe_load(contract)
            except yaml.YAMLError:
                work = None
            if isinstance(work, dict):
                evidence = work.get("evidence_requirements")
                if isinstance(evidence, list):
                    lines.extend(
                        f"- {row['command']}"
                        for row in evidence
                        if isinstance(row, dict) and isinstance(row.get("command"), str)
                    )
            else:
                reasons.append("committed test evidence is unreadable")
        else:
            reasons.append("committed test evidence is absent")
        if request.required_ci_contexts:
            lines.extend(
                (
                    "### Required CI contexts",
                    *(f"- {c}" for c in request.required_ci_contexts),
                )
            )
        else:
            reasons.append("dispatcher supplied no required CI contexts")
        return "\n".join(lines), "; ".join(reasons) or None
