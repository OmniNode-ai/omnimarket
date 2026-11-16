# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the lane-brief context pack: seven tiers, each inside its own budget, one hash, every rung recorded.

Six tiers are read by code and never call a model: graph answers and tests and required CI contexts through
injected readers (the graph query nodes, the repository), review findings from the posted review text (whole,
never cut: one that outgrows its budget refuses the pack), the files to touch, the lane history from the
ledger rows that name the ticket or a file, and the risks from ``lane_brief_risks.yaml``. The exemplar is
model-selected: the rungs run local first, then GLM, then Sonnet, and each fallback is recorded on the pack
with the rung and why. A choice a rung makes is checked against the candidates it was given: a path that is
not a candidate, or an excerpt the file never held, falls back like a failed call.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.enums.enum_lane_brief_tier import (
    EnumLaneBriefRung,
    EnumLaneBriefTier,
    EnumLaneBriefTierMode,
)
from omnimarket.models.context_pack_orchestrator.model_lane_brief_context import (
    ModelLaneBriefContextPack,
    ModelLaneBriefContextRequest,
    ModelLaneBriefExemplarChoice,
    ModelLaneBriefRungAttempt,
    ModelLaneBriefTierRecord,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols.protocol_lane_brief_context import (
    ProtocolLaneBriefRung,
    ProtocolLaneBriefTierReader,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)

_RISKS_PATH = Path(__file__).resolve().parents[1] / "lane_brief_risks.yaml"
_RUNG_ORDER = (EnumLaneBriefRung.LOCAL, EnumLaneBriefRung.GLM, EnumLaneBriefRung.SONNET)
_REQUIRED_RISK_CLASSES = frozenset(
    {"frozen-repo-growth", "branch-name-refusal", "install-refusal", "suite-timeout"}
)


class LaneBriefContextRefusedError(ValueError):
    """The pack is refused: a tier, a reader, a rung, a budget or the profile is missing or wrong."""


class LaneBriefRungError(Exception):
    """A rung could not choose (a timeout, an unreadable answer); the chain falls back to the next."""


class ModelLaneBriefRisk(BaseModel):
    """One known refusal, with the live text or row it comes from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_class: str
    rule: str
    source: str


def load_lane_brief_risks(path: Path | None = None) -> tuple[ModelLaneBriefRisk, ...]:
    """The risks tier's entries; one with no source, or a missing class, is refused."""
    loaded = yaml.safe_load((path or _RISKS_PATH).read_text(encoding="utf-8"))
    try:
        risks = tuple(ModelLaneBriefRisk.model_validate(r) for r in loaded["risks"])
    except (KeyError, TypeError, ValidationError) as exc:
        raise LaneBriefContextRefusedError(
            f"risks tier is not readable: {exc}"
        ) from exc
    if empty := [r.risk_class for r in risks if not r.source.strip()]:
        raise LaneBriefContextRefusedError(f"risks {empty} cite no source")
    if path is None and (
        gone := _REQUIRED_RISK_CLASSES - {r.risk_class for r in risks}
    ):
        raise LaneBriefContextRefusedError(f"risks tier lacks classes {sorted(gone)}")
    return risks


def _tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)  # heuristic_chars, the profile default


def _lane_history(request: ModelLaneBriefContextRequest) -> str:
    needles = (request.ticket, *request.files_to_touch)
    rows = [row for row in request.ledger_rows if any(n in row for n in needles)]
    return (
        "\n".join(rows) if rows else "no ledger row names the ticket or a file to touch"
    )


class HandlerLaneBriefContextPack:
    """The lane-brief context handler of the pack orchestrator: ``handle(request) -> pack``."""

    def __init__(
        self,
        *,
        rungs: Sequence[ProtocolLaneBriefRung],
        readers: Mapping[EnumLaneBriefTier, ProtocolLaneBriefTierReader],
        profiles_dir: Path | None = None,
    ) -> None:
        self._rungs = tuple(rungs)
        self._readers = dict(readers)
        self._resolver = HandlerContextProfileResolver(profiles_dir)

    def handle(
        self, request: ModelLaneBriefContextRequest
    ) -> ModelLaneBriefContextPack:
        resolved = self._resolver.handle(
            ModelContextProfileResolveRequest(profile_name=request.profile_name)
        )
        if (
            resolved.status is not EnumContextProfileResolveStatus.OK
            or resolved.profile is None
        ):
            raise LaneBriefContextRefusedError(
                f"profile {request.profile_name!r}: {'; '.join(resolved.errors)}"
            )
        profile = resolved.profile
        if not profile.tier_specs:
            raise LaneBriefContextRefusedError(
                f"profile {request.profile_name!r} declares no tier_specs"
            )
        if tuple(r.name for r in self._rungs) != _RUNG_ORDER:
            raise LaneBriefContextRefusedError(
                "rungs must be local, glm, sonnet in that order: "
                f"{[r.name.value for r in self._rungs]}"
            )
        sections: list[str] = []
        records: list[ModelLaneBriefTierRecord] = []
        for spec in profile.tier_specs:
            rungs: tuple[ModelLaneBriefRungAttempt, ...] = ()
            if spec.mode is EnumLaneBriefTierMode.MODEL_SELECTED:
                text, rungs = self._select_exemplar(request)
            else:
                text = self._read(spec.tier, request)
            tokens = _tokens(text)
            if tokens > spec.token_budget:
                raise LaneBriefContextRefusedError(
                    f"tier {spec.tier.value} needs {tokens} tokens, over its budget "
                    f"{spec.token_budget}; a tier is never cut to fit"
                )
            sections.append(f"## {spec.tier.value}\n{text}")
            records.append(
                ModelLaneBriefTierRecord(
                    tier=spec.tier,
                    mode=spec.mode,
                    model_called=bool(rungs),
                    rungs=rungs,
                    token_estimate=tokens,
                    token_budget=spec.token_budget,
                )
            )
        body = "\n\n".join(sections) + "\n"
        return ModelLaneBriefContextPack(
            profile_name=request.profile_name,
            profile_version=profile.profile_version,
            profile_content_hash=resolved.profile_content_hash
            or profile.content_hash(),
            body=body,
            final_pack_hash=hashlib.sha256(body.encode()).hexdigest(),
            tiers=tuple(records),
        )

    def _read(
        self, tier: EnumLaneBriefTier, request: ModelLaneBriefContextRequest
    ) -> str:
        if tier is EnumLaneBriefTier.REVIEW_FINDINGS:
            return "\n\n".join(
                f"[{c.comment_id}]\n{c.text}" for c in request.review_comments
            )
        if tier is EnumLaneBriefTier.FILES_TO_TOUCH:
            return "\n".join(request.files_to_touch)
        if tier is EnumLaneBriefTier.LANE_HISTORY:
            return _lane_history(request)
        if tier is EnumLaneBriefTier.RISKS:
            return "\n".join(
                f"- {r.risk_class}: {' '.join(r.rule.split())} (source: {r.source})"
                for r in load_lane_brief_risks()
            )
        reader = self._readers.get(tier)
        if reader is None:
            raise LaneBriefContextRefusedError(
                f"no reader is bound for tier {tier.value}"
            )
        return reader.read(request)

    def _select_exemplar(
        self, request: ModelLaneBriefContextRequest
    ) -> tuple[str, tuple[ModelLaneBriefRungAttempt, ...]]:
        by_path = {c.path: c.text for c in request.exemplar_candidates}
        attempts: list[ModelLaneBriefRungAttempt] = []
        for rung in self._rungs:
            try:
                choice = rung.select(request, request.exemplar_candidates)
                self._check_choice(choice, by_path)
            except (LaneBriefRungError, ValueError) as exc:
                attempts.append(
                    ModelLaneBriefRungAttempt(
                        rung=rung.name,
                        outcome="fell_back",
                        reason=str(exc) or type(exc).__name__,
                    )
                )
                continue
            attempts.append(
                ModelLaneBriefRungAttempt(rung=rung.name, outcome="selected")
            )
            return f"{choice.path}\n{choice.excerpt}", tuple(attempts)
        raise LaneBriefContextRefusedError(
            "tier exemplar: every rung fell back: "
            + "; ".join(f"{a.rung.value}: {a.reason}" for a in attempts)
        )

    @staticmethod
    def _check_choice(
        choice: ModelLaneBriefExemplarChoice, by_path: Mapping[str, str]
    ) -> None:
        if choice.path not in by_path:
            raise ValueError(f"chose {choice.path!r}, which is not a candidate")
        if choice.excerpt not in by_path[choice.path]:
            raise ValueError(f"excerpt is not verbatim from {choice.path!r}")


__all__ = [
    "HandlerLaneBriefContextPack",
    "LaneBriefContextRefusedError",
    "LaneBriefRungError",
    "ModelLaneBriefRisk",
    "load_lane_brief_risks",
]
