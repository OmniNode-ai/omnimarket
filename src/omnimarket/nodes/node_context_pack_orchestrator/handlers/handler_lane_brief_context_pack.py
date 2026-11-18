# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the seven budgeted tiers of a lane-brief context pack."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.enums.enum_lane_brief_tier import (
    EnumLaneBriefRefusal,
    EnumLaneBriefRung,
    EnumLaneBriefTier,
)
from omnimarket.models.context_pack_orchestrator.model_lane_brief_context import (
    ModelLaneBriefContextPack,
    ModelLaneBriefContextRequest,
    ModelLaneBriefJudgement,
    ModelLaneBriefRungAttempt,
    ModelLaneBriefTierRecord,
    is_claude_assembler,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.lane_brief_delegation import (
    LaneBriefDelegation,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.lane_brief_errors import (
    LaneBriefContextRefusedError,
    LaneBriefRungError,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.lane_brief_sources import (
    LaneBriefSources,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols.protocol_lane_brief_context import (
    ProtocolLaneBriefRung,
    ProtocolLaneBriefTierReader,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_source_ledger_effect import (
    HandlerContextSourceLedger,
    ModelLedgerHistoryReference,
    ModelLedgerHistoryScope,
)

_RISKS_PATH = Path(__file__).resolve().parents[1] / "lane_brief_risks.yaml"
_RUNG_ORDER = (EnumLaneBriefRung.LOCAL, EnumLaneBriefRung.JEV, EnumLaneBriefRung.GLM)
_REQUIRED_RISK_CLASSES = frozenset(
    {"frozen-repo-growth", "branch-name-refusal", "install-refusal", "suite-timeout"}
)


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
    if gone := _REQUIRED_RISK_CLASSES - {r.risk_class for r in risks}:
        raise LaneBriefContextRefusedError(f"risks tier lacks classes {sorted(gone)}")
    return risks


_SUPPORTED_ESTIMATION = "heuristic_chars"


def _tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)  # heuristic_chars, the profile default


def _hash_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _ledger_reference(reference: ModelLedgerHistoryReference) -> str:
    tags = reference.model_dump(exclude={"text", "source_ref"})
    metadata = " ".join(f"{key}={value}" for key, value in tags.items())
    return f"- reference={reference.source_ref} {metadata}\n  {reference.text}"


@dataclass(frozen=True)
class _Tier:
    text: str = ""
    reason: str | None = None
    rungs: tuple[ModelLaneBriefRungAttempt, ...] = ()
    judgement_reason: str | None = None
    omitted: int = 0


class HandlerLaneBriefContextPack:
    """Build the pack through configured repository, graph, ledger and delegation sources."""

    def __init__(
        self,
        *,
        rungs: Sequence[ProtocolLaneBriefRung] | None = None,
        readers: Mapping[EnumLaneBriefTier, ProtocolLaneBriefTierReader] | None = None,
        profiles_dir: Path | None = None,
        ledger: HandlerContextSourceLedger | None = None,
    ) -> None:
        if rungs is None:
            delegate = LaneBriefDelegation.from_environment()
            rungs = (delegate,) if delegate is not None else ()
        self._rungs = tuple(rungs)
        self._readers = dict(readers or {})
        self._resolver = HandlerContextProfileResolver(profiles_dir)
        self._sources = LaneBriefSources()
        self._ledger = ledger or HandlerContextSourceLedger()

    def handle(
        self,
        request: ModelLaneBriefContextRequest,
        *,
        tenant_id: str | None = None,
    ) -> ModelLaneBriefContextPack:
        resolved = self._resolver.handle(
            ModelContextProfileResolveRequest(profile_name=request.profile_name)
        )
        profile = resolved.profile
        if resolved.status is not EnumContextProfileResolveStatus.OK or profile is None:
            raise LaneBriefContextRefusedError(
                f"profile {request.profile_name!r}: {'; '.join(resolved.errors)}"
            )
        if profile.token_estimation_method != _SUPPORTED_ESTIMATION:
            raise LaneBriefContextRefusedError(
                f"profile {request.profile_name!r}: token_estimation_method "
                f"{profile.token_estimation_method!r} is not supported, only {_SUPPORTED_ESTIMATION}"
            )
        if not profile.tier_specs:
            raise LaneBriefContextRefusedError(
                f"profile {request.profile_name!r} declares no tier_specs"
            )

        names = tuple(r.name for r in self._rungs)
        if any(is_claude_assembler(name, None) for name in names):
            raise LaneBriefContextRefusedError(
                "Claude cannot assemble a lane-brief judgement",
                EnumLaneBriefRefusal.CLAUDE_ASSEMBLER,
            )
        if names != tuple(rung for rung in _RUNG_ORDER if rung in names):
            raise LaneBriefContextRefusedError("rungs must be in local, jev, glm order")

        sections: list[str] = []
        records: list[ModelLaneBriefTierRecord] = []
        for spec in profile.tier_specs:
            tier = self._read(spec.tier, request, spec.token_budget, tenant_id)
            text = tier.text
            status: Literal["assembled", "empty", "truncated", "unassembled"] = (
                "assembled"
            )
            if tier.reason:
                status = "unassembled"
                marker = f"unassembled ({tier.reason})"
                text = f"{marker}\n\n{text}" if text else marker
            elif tier.omitted:
                status = "truncated"
                text += f"\n\ntruncated ({tier.omitted} whole references exceeded the tier budget)"
            elif not text.strip():
                status = "empty"
                text = f"no {spec.tier.value} content for this brief"

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
                    model_called=bool(tier.rungs),
                    rungs=tier.rungs,
                    token_estimate=tokens,
                    token_budget=spec.token_budget,
                    status=status,
                    reason=tier.reason,
                    judgement_reason=tier.judgement_reason,
                )
            )
        body = "\n\n".join(sections)
        return ModelLaneBriefContextPack(
            profile_name=request.profile_name,
            profile_version=profile.profile_version,
            profile_content_hash=resolved.profile_content_hash
            or profile.content_hash(),
            body=body,
            final_pack_hash=_hash_body(body.encode()),
            tiers=tuple(records),
        )

    def _read(
        self,
        tier: EnumLaneBriefTier,
        request: ModelLaneBriefContextRequest,
        budget: int,
        tenant_id: str | None,
    ) -> _Tier:
        if tier is EnumLaneBriefTier.EXEMPLAR:
            return self._exemplar(request, budget)
        if tier is EnumLaneBriefTier.LANE_HISTORY:
            return self._history(request, budget, tenant_id)
        if tier is EnumLaneBriefTier.REVIEW_FINDINGS:
            if not request.review_comments:
                return _Tier(reason="dispatcher supplied no full review text")
            return _Tier(
                "\n\n".join(
                    f"[{c.comment_id}]\n{c.text}" for c in request.review_comments
                )
            )
        if tier is EnumLaneBriefTier.FILES_TO_TOUCH:
            return _Tier("\n".join(request.files_to_touch))
        if tier is EnumLaneBriefTier.RISKS:
            return _Tier(
                "\n".join(
                    f"- {r.risk_class}: {' '.join(r.rule.split())} (source: {r.source})"
                    for r in load_lane_brief_risks()
                )
            )
        if reader := self._readers.get(tier):
            return _Tier(reader.read(request))
        if tier is EnumLaneBriefTier.GRAPH_ANSWERS:
            return _Tier(*self._sources.graph(request))
        return _Tier(*self._sources.tests_and_ci(request))

    def _exemplar(self, request: ModelLaneBriefContextRequest, budget: int) -> _Tier:
        candidates, unavailable = self._sources.exemplars(request)
        if not candidates:
            missing = unavailable or "no admitted exemplar candidates"
            return _Tier(reason=missing, judgement_reason=missing)
        by_path = {candidate.path: candidate.text for candidate in candidates}

        def choose(rung: ProtocolLaneBriefRung) -> tuple[ModelLaneBriefJudgement, str]:
            choice = rung.select(request, candidates)
            self._check_assembler(choice)
            if choice.path not in by_path:
                raise ValueError(f"chose {choice.path!r}, which is not a candidate")
            if choice.excerpt not in by_path[choice.path]:
                raise ValueError(f"excerpt is not verbatim from {choice.path!r}")
            return choice, f"{choice.path}\n{choice.excerpt}"

        text, attempts, reason = self._judge(choose, budget)
        if reason:
            return _Tier(reason=reason, rungs=attempts, judgement_reason=reason)
        return _Tier(text, rungs=attempts)

    def _history(
        self,
        request: ModelLaneBriefContextRequest,
        budget: int,
        tenant_id: str | None,
    ) -> _Tier:
        result = self._ledger.read_lane_history(
            ModelLedgerHistoryScope(
                lane=request.lane,
                ticket=request.ticket,
                repository=request.repository,
                pull_request=request.pull_request,
                touched_paths=request.files_to_touch,
                as_of=datetime.now(UTC),
            ),
            tenant_id=tenant_id,
            supplied_rows=request.ledger_rows,
        )
        if result.unavailable_reason:
            return _Tier(
                reason=result.unavailable_reason,
                judgement_reason=result.unavailable_reason,
            )
        if not result.references:
            return _Tier(judgement_reason="no matching ledger references to summarize")

        def priority(ref: ModelLedgerHistoryReference) -> tuple[bool, bool, str]:
            same_pr = bool(
                request.pull_request
                and request.pull_request.rsplit("/", 1)[-1].casefold()
                in {
                    pr.strip().rsplit("/", 1)[-1].casefold() for pr in ref.pr.split(",")
                }
            )
            terminal = ref.kind == "TERMINAL"
            return terminal and same_pr, terminal, ref.time

        summary_budget = min(500, budget // 4)
        reference_budget = max(0, (budget - summary_budget) * 4 - 160)
        lines: list[str] = []
        used = 0
        for ref in sorted(result.references, key=priority, reverse=True):
            entry = _ledger_reference(ref)
            if used + len(entry) + 1 <= reference_budget:
                lines.append(entry)
                used += len(entry) + 1
        facts = "\n".join(lines)
        omitted = len(result.references) - len(lines)

        def summarize(
            rung: ProtocolLaneBriefRung,
        ) -> tuple[ModelLaneBriefJudgement, str]:
            answer = rung.summarize(request, facts)
            self._check_assembler(answer)
            return answer, answer.text

        if facts:
            text, attempts, reason = self._judge(summarize, summary_budget)
        else:
            text, attempts, reason = (
                "",
                (),
                "no whole ledger reference fit the tier budget",
            )
        if reason:
            text = f"summary unassembled ({reason})"
        return _Tier(
            f"{text}\n\n### Ledger references\n{facts}",
            rungs=attempts,
            judgement_reason=reason,
            omitted=omitted,
        )

    @staticmethod
    def _check_assembler(answer: ModelLaneBriefJudgement) -> None:
        if is_claude_assembler(answer.rung, answer.model):
            raise LaneBriefContextRefusedError(
                "Claude cannot assemble a lane-brief judgement",
                EnumLaneBriefRefusal.CLAUDE_ASSEMBLER,
            )

    def _judge(
        self,
        produce: Callable[[ProtocolLaneBriefRung], tuple[ModelLaneBriefJudgement, str]],
        budget: int,
    ) -> tuple[str, tuple[ModelLaneBriefRungAttempt, ...], str | None]:
        attempts: list[ModelLaneBriefRungAttempt] = []
        for rung in self._rungs:
            try:
                answer, content = produce(rung)
                actual = answer.rung or rung.name
                if (
                    actual not in _RUNG_ORDER
                    or not answer.model
                    or not answer.run_id
                    or not content.strip()
                ):
                    raise LaneBriefRungError(
                        "answer lacks allowed assembler provenance or text"
                    )
                text = f"assembler: rung={actual.value} model={answer.model} run_id={answer.run_id}\n{content}"
                if _tokens(text) > budget:
                    raise LaneBriefRungError(
                        f"delegation answer exceeds its {budget}-token budget"
                    )
            except LaneBriefContextRefusedError:
                raise
            except (LaneBriefRungError, ValueError, OSError) as exc:
                attempts.append(
                    ModelLaneBriefRungAttempt(
                        rung=rung.name,
                        outcome="fell_back",
                        reason=f"{type(exc).__name__}: {exc}".rstrip(": "),
                    )
                )
                continue
            attempts.append(
                ModelLaneBriefRungAttempt(
                    rung=actual,
                    outcome="selected",
                    model=answer.model,
                    run_id=answer.run_id,
                )
            )
            return text, tuple(attempts), None
        reason = (
            "every delegation rung fell back"
            if attempts
            else "delegation step is not bound"
        )
        return "", tuple(attempts), reason


__all__ = [
    "HandlerLaneBriefContextPack",
    "LaneBriefContextRefusedError",
    "LaneBriefRungError",
    "ModelLaneBriefRisk",
    "load_lane_brief_risks",
]
