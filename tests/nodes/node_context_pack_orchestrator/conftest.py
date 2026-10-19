# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture sources and profiles for context-pack orchestrator tests (OMN-21110, TA.8)."""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.models.context_pack_orchestrator import (
    ModelContextPackCommand,
    ModelContextPackRequest,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_render_compute.handlers.handler_context_pack_render import (
    RENDER_CONTRACT_VERSION,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)

SENTINEL = "SENTINEL-7f3a-package-text"
AS_OF = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
PROFILES_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "pack" / "profiles"
)
FIXED_IDENTITY = {"omnimarket.version": "0.0.0", "code.fixture": "a" * 64}

TEXTS: dict[EnumContextFactor, str] = {
    EnumContextFactor.OPERATOR_RULINGS: (
        "2026-10-15T08:14:15Z | RULING | lane=orchestrator-bb1a | "
        f'"every node is contract, node and handler" {SENTINEL}'
    ),
    EnumContextFactor.OPERATOR_WORDS_UNRECORDED: "rank the compaction mod high",
    EnumContextFactor.ORCHESTRATION_STATE: "lanes: cmod-ta8-packorch running",
    EnumContextFactor.OPEN_WORK: (
        "2026-10-17T22:36:14Z | CLAIM | lane=cmod-ta8-packorch | ticket=OMN-21110"
    ),
    EnumContextFactor.REPO_STATE: "omnimarket dev at f8eb8cdeb",
    EnumContextFactor.CONVERSATION_TAIL: "The lane read the plan section.",
    EnumContextFactor.LANE_RULES: "Never use --no-verify.\nSquash-only.",
    EnumContextFactor.PLAN_EXCERPTS: "TA.8 builds the pack orchestrator.",
}

COMPACTION_REQUIRED = (
    EnumContextFactor.OPERATOR_RULINGS,
    EnumContextFactor.OPERATOR_WORDS_UNRECORDED,
    EnumContextFactor.ORCHESTRATION_STATE,
    EnumContextFactor.OPEN_WORK,
)

LANE_BRIEF_YAML = """\
model_id: lane-brief
profile_version: 1.0.0
required_factors:
  - lane_rules
optional_factors:
  - plan_excerpts
  - operator_rulings
factor_precedence:
  - lane_rules
  - operator_rulings
  - plan_excerpts
stable_factors:
  - lane_rules
token_budget: 4000
task_kind_rules:
  - rule_id: node-work
    task_kind: node_build
    conditions:
      - {scope_field: node_archetype, operator: equals, value: orchestrator}
    factors: [plan_excerpts]
"""


def entry(
    factor: EnumContextFactor, text: str | None = None
) -> ModelContextSourceEntry:
    body = TEXTS[factor] if text is None else text
    return ModelContextSourceEntry(
        factor=factor,
        text=body,
        source_ref=f"fixture/{factor.value}@0000000",
        text_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        status=EnumContextSourceStatus.RESOLVED,
        as_of=AS_OF,
    )


class FixtureSource:
    """A ProtocolContextSource answering one fixed result and recording every request."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        factor: EnumContextFactor,
        status: EnumContextSourceStatus = EnumContextSourceStatus.RESOLVED,
        text: str | None = None,
    ) -> None:
        self.factor = factor
        self.status = status
        self.text = text
        self.requests: list[ModelContextSourceRequest] = []

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        self.requests.append(request)
        if self.status is EnumContextSourceStatus.RESOLVED:
            return ModelContextSourceResult(
                status=self.status, entries=(entry(self.factor, self.text),)
            )
        return ModelContextSourceResult(status=self.status)


def sources(
    factors: tuple[EnumContextFactor, ...] = tuple(TEXTS),
    **overrides: FixtureSource,
) -> dict[EnumContextFactor, FixtureSource]:
    bound = {factor: FixtureSource(factor) for factor in factors}
    for name, source in overrides.items():
        bound[EnumContextFactor(name)] = source
    return bound


def access(tenant: str = "tenant-a") -> ModelContextAccess:
    return ModelContextAccess(tenant_id=tenant)


def command(**fields: object) -> ModelContextPackCommand:
    payload: dict[str, object] = {
        "profile_name": "compaction_orchestrator",
        "session_id": "session-1",
        "newest_turn": "turn-9",
    }
    payload.update(fields)
    return ModelContextPackCommand(
        request=ModelContextPackRequest.model_validate(payload), access=access()
    )


def orchestrator(
    bound: dict[EnumContextFactor, FixtureSource] | None = None,
    profiles_dir: Path = PROFILES_DIR,
    identity: dict[str, str] | None = None,
    render_contract_version: str = RENDER_CONTRACT_VERSION,
) -> HandlerContextPackOrchestrator:
    fixed = dict(FIXED_IDENTITY if identity is None else identity)
    return HandlerContextPackOrchestrator(
        sources() if bound is None else bound,
        profile_resolver=HandlerContextProfileResolver(profiles_dir),
        implementation_identity=lambda: fixed,
        render_contract_version=render_contract_version,
    )


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    target = tmp_path / "profiles"
    shutil.copytree(PROFILES_DIR, target)
    (target / "lane_brief.yaml").write_text(LANE_BRIEF_YAML, encoding="utf-8")
    return target
