# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The no-file census and its ratchet (plan section 4.3, reading 2).

``handle(ModelCensusRequest) -> ModelCensusResult``. It counts, per registered repository, the files
holding an executable reader or writer of the ledger file's name or its path variable, and compares
each count with the shrink-only ratchet. A count above its ratchet row is red (``ratchet-grew``); a
count below it passes and is returned lowered in ``lowered_ratchet`` for the caller to write. An
absent repository, an unreadable one, a repository with no ratchet row and an unreadable ratchet
file are typed failures, never a pass. Each run is one chain under the emission contract.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    executable_hits,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    EnumCensusKind,
    ModelCensusHit,
    ModelCensusOverlay,
    ModelCensusRequest,
    ModelCensusResult,
    ModelRepositoryCount,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.protocols import (
    ProtocolRatchetStore,
    ProtocolRepositorySource,
)

NODE_NAME = "node_work_ledger_file_census_effect"
PIECE = 6
_ORDER = (
    "repository-not-registered",
    "repo-absent",
    "repo-unreadable",
    "ratchet-missing-row",
    "ratchet-grew",
)


class HandlerFileCensus:
    def __init__(
        self,
        overlay: ModelCensusOverlay,
        source: ProtocolRepositorySource,
        ratchet: ProtocolRatchetStore,
        sink: ProtocolEmissionSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        backend: str = "real",
    ) -> None:
        self._overlay = overlay
        self._source = source
        self._ratchet = ratchet
        self._sink = sink
        self._clock = clock
        self._backend = backend

    def handle(self, request: ModelCensusRequest) -> ModelCensusResult:
        chain = ChainEmitter(
            self._sink,
            node=NODE_NAME,
            piece=PIECE,
            clock=self._clock,
            correlation_id=request.correlation_id,
            backend="in_memory" if self._backend == "in_memory" else "real",
        )
        chain.start()
        registered = {r.name: r for r in self._overlay.repositories}
        wanted = request.repositories or tuple(registered)
        unknown = [name for name in wanted if name not in registered]
        if unknown:
            return self._fail(
                chain, request, (), "repository-not-registered", ",".join(unknown)
            )
        try:
            ratchet = self._ratchet.read()
        except (OSError, ValueError) as exc:
            return self._fail(
                chain, request, (), "ratchet-unreadable", type(exc).__name__
            )
        counts = tuple(
            self._count(registered[name].name, registered[name].own_line, ratchet)
            for name in wanted
        )
        total = sum(c.files for c in counts if not c.own_line)
        lowered = dict(ratchet)
        for count in counts:
            if count.state == "lowerable":
                lowered[count.repository] = count.files
        codes = {
            "absent": "repo-absent",
            "unreadable": "repo-unreadable",
            "unratcheted": "ratchet-missing-row",
            "grew": "ratchet-grew",
        }
        failing = [c for c in counts if c.state in codes]
        if not failing:
            chain.success({"files": total, "repositories": len(counts)})
            return ModelCensusResult(
                correlation_id=request.correlation_id,
                passed=True,
                total_files=total,
                counts=counts,
                lowered_ratchet=lowered,
            )
        code = min((codes[c.state] for c in failing), key=_ORDER.index)
        detail = ",".join(
            f"{c.repository}={c.files}>{c.ratchet}"
            if c.state == "grew"
            else c.repository
            for c in failing
            if codes[c.state] == code
        )
        return self._fail(chain, request, counts, code, detail, total, lowered)

    def _fail(
        self,
        chain: ChainEmitter,
        request: ModelCensusRequest,
        counts: tuple[ModelRepositoryCount, ...],
        code: str,
        detail: str,
        total: int = 0,
        lowered: dict[str, int] | None = None,
    ) -> ModelCensusResult:
        chain.failure(code, detail)
        return ModelCensusResult(
            correlation_id=request.correlation_id,
            passed=False,
            total_files=total,
            counts=counts,
            failure_code=code,
            failure_detail=detail[:300],
            lowered_ratchet=lowered or {},
        )

    def _count(
        self, name: str, own_line: bool, ratchet: dict[str, int]
    ) -> ModelRepositoryCount:
        def result(
            state: str, hits: tuple[ModelCensusHit, ...] = ()
        ) -> ModelRepositoryCount:
            files = len({h.path for h in hits})
            return ModelRepositoryCount(
                repository=name,
                own_line=own_line,
                present=state != "absent",
                files=files,
                readers=sum(h.kind is EnumCensusKind.READER for h in hits),
                writers=sum(h.kind is EnumCensusKind.WRITER for h in hits),
                hits=hits,
                ratchet=ratchet.get(name),
                state=state,
            )

        try:
            if not self._source.present(name):
                return result("absent")
            tokens = (*self._overlay.names, *self._overlay.path_variables)
            hits: list[ModelCensusHit] = []
            for path in sorted(self._source.candidates(name, tokens)):
                text = self._source.read(name, path)
                if text is not None:
                    hits.extend(executable_hits(path, text, self._overlay))
        except OSError:
            return result("unreadable")
        found = tuple(hits)
        files = len({h.path for h in found})
        bound = ratchet.get(name)
        if bound is None:
            return result("unratcheted", found)
        if files > bound:
            return result("grew", found)
        return result("lowerable" if files < bound else "ok", found)
