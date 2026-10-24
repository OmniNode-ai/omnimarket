# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextPackOrchestrator: the one callable path to a context package (OMN-21110, TA.8).

``handle(ModelContextPackCommand) -> ModelContextPackBuilt``. It resolves the profile by
name, reads each tier the profile selects from the source bound to that factor through
``ProtocolContextTierSource`` (the knowledge assembler's fan-out shape), builds the pack and
renders it. A required tier whose source answers anything but ``resolved`` fails the build
``REQUIRED_FACTOR_MISSING``; an optional tier's other statuses are listed per tier and the
tier is omitted. Optional tiers that do not fit the budget are dropped lowest precedence
first and listed in ``excluded_by_budget``. When the required tiers alone are still over
budget, the cited entries of every required tier are charged first. Each tier is then
selected inside its share of what is left: the newest, whole entries only and never
truncated (OMN-21111).
``OPERATOR_WORDS_UNRECORDED`` is carried whole (the carry rule). A cited entry that does not
fit, or a tier left with no entry, fails the build ``TOKEN_BUDGET_EXCEEDED``. Each tier is
read at its own revision when the request names one in ``tier_revisions``.
When sources are omitted, handlers are loaded from the contract; explicit mappings are used as supplied.

Identities follow plan section 6: ``candidate_id`` is uuid5 over the build's inputs,
contracts and ``implementation_identity``; ``assembly_hash`` is the pack hash over the
deterministic factors only; ``final_pack_hash`` is the builder's hash over the whole pack.
``generated_at`` is the newest source timestamp, never the wall clock.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_pack_provenance import EnumContextPackProvenance
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from omnibase_core.models.pack.model_context_pack import ModelContextPack
from omnibase_core.utils.util_context_pack import compute_chunk_id

from omnimarket.models.context_pack.model_context_task_kind_rule import (
    TASK_KIND_UNKNOWN,
)
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackMode,
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackCommand,
    ModelContextPackRequest,
    ModelContextPackTierStatus,
)
from omnimarket.models.context_pack_render import (
    EnumContextPackRenderStatus,
    ModelContextPackCarriedPrompt,
    ModelContextPackRenderChunkRef,
    ModelContextPackRenderIdentity,
    ModelContextPackRenderRequest,
    ModelContextPackRenderResult,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.models.context_task_kind_rules import (
    EnumContextTaskKindRulesStatus,
    ModelContextTaskKindRulesRequest,
)
from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_pack_render_compute.handlers.handler_context_pack_render import (
    RENDER_CONTRACT_VERSION,
    HandlerContextPackRender,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.handlers.handler_context_task_kind_rules import (
    HandlerContextTaskKindRules,
)
from omnimarket.pack.model_context_pack_artifact import ModelContextPackArtifact
from omnimarket.pack.model_context_pack_builder_request import (
    ModelContextPackBuilderRequest,
)
from omnimarket.pack.model_context_pack_builder_result import (
    EnumContextPackBuilderStatus,
)
from omnimarket.pack.model_context_profile import ModelContextProfile

REDUCER_VERSION = "context-pack-orchestrator/1"

_NODES_DIR = Path(__file__).resolve().parents[2]
_BUILDER_CONTRACT = _NODES_DIR / "node_context_pack_builder_compute" / "contract.yaml"
_RENDER_CONTRACT = _NODES_DIR / "node_context_pack_render_compute" / "contract.yaml"
# The handler modules whose code shapes the deterministic output (plan section 6).
_IMPLEMENTATION_MODULES = (
    Path(__file__).resolve(),
    _NODES_DIR
    / "node_context_pack_builder_compute"
    / "handlers"
    / "handler_context_pack_builder.py",
    _NODES_DIR
    / "node_context_pack_render_compute"
    / "handlers"
    / "handler_context_pack_render.py",
    _NODES_DIR
    / "node_context_profile_resolver_effect"
    / "handlers"
    / "handler_context_profile_resolver.py",
    _NODES_DIR
    / "node_context_task_kind_rules_compute"
    / "handlers"
    / "handler_context_task_kind_rules.py",
)
_CANDIDATE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "onex:context-pack-candidate/1")
# Model-dependent factors stay out of the assembly hash (plan section 6, v6).
_NON_DETERMINISTIC = frozenset(
    {EnumContextFactor.TRANSCRIPT_EXTRACTS, EnumContextFactor.TAIL_SUMMARY}
)
_PROVENANCE = {
    EnumContextFactor.TAIL_SUMMARY: EnumContextPackProvenance.GENERATED,
}
# A ledger row opens "<timestamp> | <TYPE> | ... lane=<lane> ...".
_ROW_HEAD = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \|")
_ROW_LANE = re.compile(r"\| lane=(?P<lane>[^|\s]+)")


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _token_estimate(text: str) -> int:
    # heuristic_chars, the profile default: about four characters per token.
    return max(1, (len(text) + 3) // 4)


def default_implementation_identity() -> dict[str, str]:
    """Distribution, version, source commit and handler code digest of the pack path.

    A handler change under an unchanged contract changes the code digest, and so the
    candidate (TA.8 criterion); an installed distribution adds its recorded commit.
    """
    identity: dict[str, str] = {}
    try:
        dist = importlib.metadata.distribution("omnimarket")
        identity["omnimarket.version"] = dist.version
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            commit = json.loads(direct_url).get("vcs_info", {}).get("commit_id")
            if commit:
                identity["omnimarket.commit"] = str(commit)
    except importlib.metadata.PackageNotFoundError:
        identity["omnimarket.version"] = "not-installed"
    for module in _IMPLEMENTATION_MODULES:
        identity[f"code.{module.parent.parent.name}"] = _file_sha256(module)
    return identity


def _selected_factors(
    profile: ModelContextProfile, admitted: frozenset[EnumContextFactor]
) -> tuple[EnumContextFactor, ...]:
    excluded = set(profile.excluded_factors)
    seen: list[EnumContextFactor] = []
    for factor in (*profile.required_factors, *profile.optional_factors):
        if factor in admitted and factor not in excluded and factor not in seen:
            seen.append(factor)
    return tuple(seen)


def _precedence(profile: ModelContextProfile) -> dict[EnumContextFactor, int]:
    ordered = profile.factor_precedence or (
        profile.required_factors + profile.optional_factors
    )
    return {factor: index for index, factor in enumerate(ordered)}


def _session_scopes(request: ModelContextPackRequest) -> tuple[str, ...]:
    scopes: list[str] = []
    # The session source reads exactly one mode scope: case: (pinned) or session: (live).
    if request.mode is EnumContextPackMode.PINNED:
        scopes.append(f"case:{request.manifest_case}")
    elif request.session_id is not None:
        scopes.append(f"session:{request.session_id}")
    if request.newest_turn is not None:
        scopes.append(f"newest_turn:{request.newest_turn}")
    if request.session_barrier is not None:
        scopes.append(f"session_barrier:{request.session_barrier}")
    return tuple(scopes)


def _failed(
    request: ModelContextPackRequest,
    failure_class: EnumContextPackFailure,
    *errors: str,
    profile: ModelContextProfile | None = None,
    profile_content_hash: str | None = None,
    tiers: tuple[ModelContextPackTierStatus, ...] = (),
) -> ModelContextPackBuilt:
    return ModelContextPackBuilt(
        status=EnumContextPackOrchestratorStatus.FAILED,
        profile_name=request.profile_name,
        profile_version=profile.profile_version if profile is not None else None,
        profile_content_hash=profile_content_hash,
        tier_statuses=tiers,
        failure_class=failure_class,
        errors=errors,
        terminal_failure_cause=failure_class,
    )


def _ruling_ref(entry: ModelContextSourceEntry) -> tuple[str | None, str | None]:
    head = _ROW_HEAD.match(entry.text)
    lane = _ROW_LANE.search(entry.text)
    return (
        head.group("ts") if head else None,
        lane.group("lane") if lane else None,
    )


def _source_identity(source: ProtocolContextTierSource) -> str:
    contract_hash = getattr(source, "contract_hash", None)
    if isinstance(contract_hash, str) and contract_hash:
        return contract_hash
    kind = type(source)
    return f"{kind.__module__}.{kind.__qualname__}"


class HandlerContextPackOrchestrator:
    """Resolve, read every tier through its source, build and render one context package."""

    def __init__(
        self,
        sources: Mapping[EnumContextFactor, ProtocolContextTierSource] | None = None,
        *,
        profile_resolver: HandlerContextProfileResolver | None = None,
        builder: HandlerContextPackBuilder | None = None,
        renderer: HandlerContextPackRender | None = None,
        task_kind_rules: HandlerContextTaskKindRules | None = None,
        implementation_identity: Callable[[], Mapping[str, str]] | None = None,
        render_contract_version: str = RENDER_CONTRACT_VERSION,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._sources = {} if sources is None else dict(sources)
        self._resolver = profile_resolver or HandlerContextProfileResolver()
        self._builder = builder or HandlerContextPackBuilder()
        self._renderer = renderer or HandlerContextPackRender()
        self._task_kind_rules = task_kind_rules or HandlerContextTaskKindRules()
        self._implementation_identity = (
            implementation_identity or default_implementation_identity
        )
        self._render_contract_version = render_contract_version
        self._clock = clock if clock is not None else time.perf_counter

    def handle(self, command: ModelContextPackCommand) -> ModelContextPackBuilt:
        request = command.request
        resolved = self._resolver.handle(
            ModelContextProfileResolveRequest(profile_name=request.profile_name)
        )
        if (
            resolved.status is not EnumContextProfileResolveStatus.OK
            or resolved.profile is None
            or resolved.profile_content_hash is None
        ):
            return _failed(
                request,
                resolved.failure_class or EnumContextPackFailure.PROFILE_NOT_FOUND,
                *resolved.errors,
            )
        profile = resolved.profile
        content_hash = resolved.profile_content_hash
        if request.token_budget_override is not None:
            if request.token_budget_override > profile.token_budget:
                return _failed(
                    request,
                    EnumContextPackFailure.INVALID_PROFILE,
                    f"token_budget_override {request.token_budget_override} exceeds "
                    f"the profile budget {profile.token_budget}; it may only lower it",
                    profile=profile,
                    profile_content_hash=content_hash,
                )
            profile = profile.model_copy(
                update={"token_budget": request.token_budget_override}
            )

        evaluated = self._task_kind_rules.handle(
            ModelContextTaskKindRulesRequest(
                profile_name=request.profile_name,
                profile=profile,
                caller_scope=request.caller_scope,
            )
        )
        if evaluated.status is not EnumContextTaskKindRulesStatus.OK:
            return _failed(
                request,
                EnumContextPackFailure.INVALID_PROFILE,
                evaluated.error or "task-kind rules could not be evaluated",
                profile=profile,
                profile_content_hash=content_hash,
            )
        task_kind = evaluated.task_kind if profile.task_kind_rules else None
        admitted = frozenset(evaluated.admitted_factors)
        required = set(profile.required_factors)
        session_scopes = _session_scopes(request)
        default_revision = request.session_barrier or request.newest_turn or "head"

        reads: list[
            tuple[EnumContextFactor, ModelContextSourceResult, float | None]
        ] = []
        for factor in _selected_factors(profile, admitted):
            source = self._sources.get(factor)
            if source is None:
                reads.append(
                    (
                        factor,
                        ModelContextSourceResult(
                            status=EnumContextSourceStatus.UNAVAILABLE
                        ),
                        None,
                    )
                )
                continue
            source_request = ModelContextSourceRequest(
                factor=factor,
                scopes=(*profile.scopes, *session_scopes),
                task_kind=task_kind,
                caller_scope=request.caller_scope,
                access=command.access,
                revision=request.tier_revisions.get(factor, default_revision),
            )
            started = self._clock()
            try:
                try:
                    answer = source.handle(source_request)
                finally:
                    elapsed_ms = (self._clock() - started) * 1000.0
            except Exception as exc:  # a source failure is a tier status, never a crash
                answer = ModelContextSourceResult.error(
                    EnumContextSourceErrorCode.INVALID_RESPONSE,
                    f"{type(exc).__name__}: {exc}",
                )
            reads.append((factor, answer, elapsed_ms))

        tiers: list[ModelContextPackTierStatus] = []
        entries_by_factor: dict[
            EnumContextFactor, tuple[ModelContextSourceEntry, ...]
        ] = {}
        missing: list[EnumContextFactor] = []
        for factor, answer, read_ms in reads:
            entries = tuple(e for e in answer.entries if e.factor is factor and e.text)
            usable = answer.status is EnumContextSourceStatus.RESOLVED and bool(entries)
            if usable:
                entries_by_factor[factor] = entries
            elif factor in required:
                missing.append(factor)
            tiers.append(
                ModelContextPackTierStatus(
                    factor=factor,
                    required=factor in required,
                    status=answer.status,
                    error_code=answer.error_code,
                    error_message=answer.error_message,
                    entry_count=len(entries) if usable else 0,
                    token_estimate=(
                        sum(_token_estimate(e.text) for e in entries) if usable else 0
                    ),
                    included=usable,
                    read_ms=read_ms,
                )
            )
        if missing:
            return _failed(
                request,
                EnumContextPackFailure.REQUIRED_FACTOR_MISSING,
                "required tiers not resolved: "
                + ", ".join(
                    f"{t.factor.value}={t.status.value}"
                    for t in tiers
                    if t.factor in missing
                ),
                profile=profile,
                profile_content_hash=content_hash,
                tiers=tuple(tiers),
            )

        # Optional tiers that do not fit are dropped lowest precedence first.
        precedence = _precedence(profile)
        tier_tokens = {t.factor: t.token_estimate for t in tiers if t.included}
        excluded_by_budget: list[EnumContextFactor] = []
        optional_by_rank = sorted(
            (f for f in entries_by_factor if f not in required),
            key=lambda f: precedence.get(f, len(precedence)),
            reverse=True,
        )
        for factor in optional_by_rank:
            if sum(tier_tokens.values()) <= profile.token_budget:
                break
            excluded_by_budget.append(factor)
            del entries_by_factor[factor]
            del tier_tokens[factor]
        if excluded_by_budget:
            dropped = set(excluded_by_budget)
            tiers = [
                t.model_copy(update={"included": False}) if t.factor in dropped else t
                for t in tiers
            ]
        if sum(tier_tokens.values()) > profile.token_budget:
            selected = _select_required_tiers(
                entries_by_factor, profile.token_budget, frozenset(request.cited_refs)
            )
            if isinstance(selected, str):
                return _failed(
                    request,
                    EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED,
                    selected,
                    profile=profile,
                    profile_content_hash=content_hash,
                    tiers=tuple(tiers),
                )
            tiers = [
                t.model_copy(
                    update={
                        "entry_count": len(selected[t.factor]),
                        "token_estimate": _tokens(selected[t.factor]),
                        "trimmed_entry_count": len(entries_by_factor[t.factor])
                        - len(selected[t.factor]),
                    }
                )
                if t.factor in selected
                else t
                for t in tiers
            ]
            entries_by_factor.update(selected)

        all_entries = [e for entries in entries_by_factor.values() for e in entries]
        if not all_entries:
            return _failed(
                request,
                EnumContextPackFailure.REQUIRED_FACTOR_MISSING,
                "no tier resolved any entry",
                profile=profile,
                profile_content_hash=content_hash,
                tiers=tuple(tiers),
            )
        generated_at = max(e.as_of for e in all_entries).isoformat()

        version_vector = [
            {
                "factor": t.factor.value,
                "status": t.status.value,
                "entries": [
                    [e.source_ref, e.text_sha256, e.as_of.isoformat()]
                    for e in entries_by_factor.get(t.factor, ())
                ],
            }
            for t in tiers
        ]
        version_vector_hash = _sha256(version_vector)
        builder_contract_hash = _file_sha256(_BUILDER_CONTRACT)
        implementation_identity = dict(sorted(self._implementation_identity().items()))
        candidate_id = str(
            uuid.uuid5(
                _CANDIDATE_NAMESPACE,
                _canonical(
                    {
                        "session_id": request.session_id,
                        "input_messages_hash": _sha256(
                            [
                                request.session_id,
                                request.newest_turn,
                                request.manifest_case,
                                # Present only when set, so a request without them
                                # keeps its candidate.
                                *(
                                    [
                                        {
                                            f.value: r
                                            for f, r in sorted(
                                                request.tier_revisions.items()
                                            )
                                        }
                                    ]
                                    if request.tier_revisions
                                    else []
                                ),
                                *(
                                    [sorted(request.cited_refs)]
                                    if request.cited_refs
                                    else []
                                ),
                            ]
                        ),
                        "version_vector_hash": version_vector_hash,
                        "caller_scope_hash": _sha256(
                            request.caller_scope.model_dump(mode="json")
                        ),
                        "profile_content_hash": _sha256(
                            [content_hash, request.token_budget_override]
                        ),
                        "sources_contract_hash": _sha256(
                            {
                                f.value: _source_identity(s)
                                for f, s in sorted(
                                    self._sources.items(), key=lambda kv: kv[0].value
                                )
                            }
                        ),
                        "reducer_version": REDUCER_VERSION,
                        "builder_contract_hash": builder_contract_hash,
                        "render_contract_hash": _sha256(
                            [
                                self._render_contract_version,
                                _file_sha256(_RENDER_CONTRACT),
                            ]
                        ),
                        "implementation_identity": implementation_identity,
                    }
                ),
            )
        )

        artifacts = tuple(
            ModelContextPackArtifact(
                factor=entry.factor,
                content=entry.text,
                token_estimate=_token_estimate(entry.text),
                provenance=_PROVENANCE.get(
                    entry.factor, EnumContextPackProvenance.OBSERVED
                ),
                source_artifact_hash=entry.text_sha256,
                source_contract_hash=builder_contract_hash,
                source_run_id=entry.source_ref,
                # The builder orders a factor's chunks by source_priority before its
                # hashes, so the position in the source result keeps source order.
                source_priority=position,
            )
            for entries in entries_by_factor.values()
            for position, entry in enumerate(entries)
        )
        built = self._builder.handle(
            ModelContextPackBuilderRequest(
                contract_hash=builder_contract_hash,
                generated_at=generated_at,
                profile=profile,
                artifacts=artifacts,
            )
        )
        if (
            built.status is not EnumContextPackBuilderStatus.OK
            or built.context_pack is None
            or built.pack_hash is None
        ):
            return _failed(
                request,
                built.failure_class or EnumContextPackFailure.INVALID_PROFILE,
                *built.errors,
                profile=profile,
                profile_content_hash=content_hash,
                tiers=tuple(tiers),
            )
        pack = built.context_pack
        assembly_hash = _assembly_hash(pack)

        refs = self._chunk_refs(entries_by_factor)
        unrecorded = entries_by_factor.get(
            EnumContextFactor.OPERATOR_WORDS_UNRECORDED, ()
        )
        rendered: ModelContextPackRenderResult = self._renderer.handle(
            ModelContextPackRenderRequest(
                pack=pack,
                chunk_refs=refs,
                identity=ModelContextPackRenderIdentity(
                    candidate_id=candidate_id,
                    assembly_hash=assembly_hash,
                    final_pack_hash=built.pack_hash,
                    version_vector_hash=version_vector_hash,
                    profile_name=request.profile_name,
                    unrecorded_prompt_count=len(unrecorded),
                    reviewed_prompt_count=0,
                    scrubbed_span_count=0,
                    transcript_extract_candidate_count=len(
                        entries_by_factor.get(EnumContextFactor.TRANSCRIPT_EXTRACTS, ())
                    ),
                    compact_route_identity=profile.summary_task_class
                    if EnumContextFactor.TAIL_SUMMARY in entries_by_factor
                    else None,
                ),
                carried_prompts=tuple(
                    ModelContextPackCarriedPrompt(
                        prompt_id=entry.source_ref or entry.text_sha256[:16],
                        capture_sequence=index,
                    )
                    for index, entry in enumerate(_unique_by_ref(unrecorded))
                ),
            )
        )
        if rendered.status is not EnumContextPackRenderStatus.OK:
            failed = _failed(
                request,
                EnumContextPackFailure.INVALID_PROFILE,
                *rendered.errors,
                profile=profile,
                profile_content_hash=content_hash,
                tiers=tuple(tiers),
            )
            return failed.model_copy(
                update={"render_failure_class": rendered.failure_class}
            )

        return ModelContextPackBuilt(
            status=EnumContextPackOrchestratorStatus.OK,
            profile_name=request.profile_name,
            profile_version=profile.profile_version,
            profile_content_hash=content_hash,
            task_kind=task_kind,
            candidate_id=candidate_id,
            assembly_hash=assembly_hash,
            final_pack_hash=built.pack_hash,
            body_hash=rendered.body_hash,
            stable_prefix_hash=rendered.stable_prefix_hash,
            version_vector_hash=version_vector_hash,
            generated_at=generated_at,
            messages=rendered.messages,
            factor_token_estimates={
                t.factor: t.token_estimate
                for t in tiers
                if t.factor in entries_by_factor
            },
            tier_statuses=tuple(tiers),
            excluded_by_budget=tuple(excluded_by_budget),
            implementation_identity=implementation_identity,
        )

    @staticmethod
    def _chunk_refs(
        entries_by_factor: Mapping[
            EnumContextFactor, tuple[ModelContextSourceEntry, ...]
        ],
    ) -> tuple[ModelContextPackRenderChunkRef, ...]:
        refs: dict[str, ModelContextPackRenderChunkRef] = {}
        for factor, entries in entries_by_factor.items():
            for entry in entries:
                chunk_id = compute_chunk_id(factor, entry.text)
                timestamp, lane = (
                    _ruling_ref(entry)
                    if factor is EnumContextFactor.OPERATOR_RULINGS
                    else (None, None)
                )
                refs.setdefault(
                    chunk_id,
                    ModelContextPackRenderChunkRef(
                        chunk_id=chunk_id,
                        source_identifier=entry.source_ref,
                        row_timestamp=timestamp,
                        row_lane=lane,
                    ),
                )
        return tuple(refs.values())


# Carried whole and verbatim: only an operator's review row removes a prompt (carry rule).
_NEVER_TRIMMED = frozenset({EnumContextFactor.OPERATOR_WORDS_UNRECORDED})


def _tokens(entries: tuple[ModelContextSourceEntry, ...]) -> int:
    return sum(_token_estimate(e.text) for e in entries)


def _is_cited(entry: ModelContextSourceEntry, cited: frozenset[str]) -> bool:
    if entry.source_ref is not None and entry.source_ref in cited:
        return True
    head = _ROW_HEAD.match(entry.text)
    return head is not None and head.group("ts") in cited


def _select_within_tier(
    factor: EnumContextFactor,
    entries: tuple[ModelContextSourceEntry, ...],
    budget: int,
    cited: frozenset[str],
) -> tuple[ModelContextSourceEntry, ...] | str:
    """Cited entries first, then the newest, whole entries only, to ``budget``.

    Returns the kept entries in source order, or the reason the tier cannot fit.
    """
    indexed = list(enumerate(entries))
    newest_first = sorted(
        indexed, key=lambda pair: (-pair[1].as_of.timestamp(), pair[0])
    )
    kept: set[int] = set()
    used = 0
    for index, entry in newest_first:
        if _is_cited(entry, cited):
            used += _token_estimate(entry.text)
            kept.add(index)
    if used > budget:
        return (
            f"{factor.value}: cited entries need {used} tokens, over the tier "
            f"budget {budget}"
        )
    for index, entry in newest_first:
        if index in kept:
            continue
        cost = _token_estimate(entry.text)
        if used + cost > budget:
            break
        used += cost
        kept.add(index)
    if not kept:
        return (
            f"{factor.value}: no entry fits the tier budget {budget} "
            f"(newest entry {_token_estimate(newest_first[0][1].text)} tokens)"
        )
    return tuple(entry for index, entry in indexed if index in kept)


def _select_required_tiers(
    entries_by_factor: Mapping[EnumContextFactor, tuple[ModelContextSourceEntry, ...]],
    budget: int,
    cited: frozenset[str],
) -> dict[EnumContextFactor, tuple[ModelContextSourceEntry, ...]] | str:
    """Charge cited entries first and share the uncited remainder equally, smallest first.

    Tiers that are never trimmed and cited entries of every trimmable tier are charged
    first. The uncited remainder is shared equally, smallest first. A tier within its
    share is kept whole, and every larger tier is selected inside that share.
    """
    fixed = sum(
        _tokens(entries)
        for factor, entries in entries_by_factor.items()
        if factor in _NEVER_TRIMMED
    )
    remaining = budget - fixed
    if remaining < 0:
        return (
            f"tiers carried whole need {fixed} tokens, over the budget {budget}: "
            + ", ".join(
                f.value for f in sorted(entries_by_factor) if f in _NEVER_TRIMMED
            )
        )
    cost = {
        factor: sum(_token_estimate(e.text) for e in entries if _is_cited(e, cited))
        for factor, entries in entries_by_factor.items()
        if factor not in _NEVER_TRIMMED
    }
    remaining -= sum(cost.values())
    if remaining < 0:
        return (
            f"cited entries need {fixed + sum(cost.values())} tokens, "
            f"over the budget {budget}: "
            + ", ".join(f.value for f in sorted(cost) if cost[f] > 0)
        )
    trimmable = sorted(
        cost,
        key=lambda f: (_tokens(entries_by_factor[f]) - cost[f], f.value),
    )
    selected: dict[EnumContextFactor, tuple[ModelContextSourceEntry, ...]] = {}
    for position, factor in enumerate(trimmable):
        entries = entries_by_factor[factor]
        share = remaining // (len(trimmable) - position)
        uncited = _tokens(entries) - cost[factor]
        if uncited <= share:
            remaining -= uncited
            continue
        chosen = _select_within_tier(factor, entries, cost[factor] + share, cited)
        if isinstance(chosen, str):
            return chosen
        selected[factor] = chosen
        remaining -= _tokens(chosen) - cost[factor]
    return selected


def _unique_by_ref(
    entries: tuple[ModelContextSourceEntry, ...],
) -> tuple[ModelContextSourceEntry, ...]:
    seen: set[str] = set()
    unique: list[ModelContextSourceEntry] = []
    for entry in entries:
        key = entry.source_ref or entry.text_sha256[:16]
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return tuple(unique)


def _assembly_hash(pack: ModelContextPack) -> str:
    """The pack hash over the deterministic factors only (plan section 6, v6)."""
    dumped = pack.model_dump(mode="json")
    dumped["chunks"] = [
        chunk.model_dump(mode="json")
        for chunk in pack.chunks
        if chunk.factor not in _NON_DETERMINISTIC
    ]
    return _sha256(dumped)


__all__ = [
    "REDUCER_VERSION",
    "TASK_KIND_UNKNOWN",
    "HandlerContextPackOrchestrator",
    "default_implementation_identity",
]
