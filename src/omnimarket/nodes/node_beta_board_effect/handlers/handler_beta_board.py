# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The beta board's stages, run as a step of the factory floor publish (OMN-23315).

``build`` refreshes the probe reads, reads the ledger readings, computes each criterion's state and writes the board's JSON under the
registry home. ``publish`` draws the page from that JSON and copies the page and the JSON to every host the overlay lists, over ssh,
each file written to a temporary name and renamed into place. ``run`` does both; a host that cannot be reached is named in the result
and never stops the others.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_beta_board_effect.handlers.adapter_beta_board_sources import (
    CommandRunner,
    CopyRunner,
    read_ledger_readings,
    refresh_probes,
    remote_write_command,
    run_command,
    run_copy,
    ssh_argv,
    write_atomic,
)
from omnimarket.nodes.node_beta_board_effect.handlers.compute_beta_board import (
    build_board,
)
from omnimarket.nodes.node_beta_board_effect.handlers.render_beta_board import (
    render_page,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoard,
    ModelBetaBoardContractRuntime,
    ModelBetaBoardOverlay,
    ModelBetaBoardRequest,
    ModelBetaBoardResult,
    ModelBetaBoardRuntime,
    ModelSourceRead,
    ModelTargetPublish,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    write_tally_receipt,
)

OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
BETA_BOARD_STEP = "beta_board"


def load_runtime(
    contract_path: Path = OWN_CONTRACT,
    environ: Mapping[str, str] | None = None,
    overlay: ModelBetaBoardOverlay | None = None,
) -> ModelBetaBoardRuntime:
    """The contract's runtime block with the deployment overlay: the file the contract's variable names, else the overlay root's.

    A node with no overlay refuses by name (there is no packaged fallback), so the board never follows criteria nobody declared.
    """
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract = ModelBetaBoardContractRuntime.model_validate(raw["runtime"])
    declared = load_structured_overlay(
        contract.overlay_id,
        ModelBetaBoardOverlay,
        environ,
        explicit=overlay,
        file_env=contract.overlay_file_env,
    )
    return ModelBetaBoardRuntime(**contract.model_dump(), overlay=declared)


class HandlerBetaBoard:
    """Runs one stage of the beta board."""

    def __init__(
        self,
        runtime: ModelBetaBoardRuntime | None = None,
        run: CommandRunner = run_command,
        copy: CopyRunner = run_copy,
        clock: Callable[[], datetime] | None = None,
        ledger: ProtocolLedgerRowSource | None = None,
        cache: CachedSourceReader | None = None,
    ) -> None:
        self._rt = runtime or load_runtime()
        self._ledger = ledger
        self._cache = cache
        self._run = run
        self._copy = copy
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelBetaBoardRequest) -> ModelBetaBoardResult:
        rt = self._rt
        home_text = request.home or os.environ.get(rt.home_env)
        if not home_text:
            return ModelBetaBoardResult(
                ok=False,
                stage=request.stage,
                detail=f"no registry home: pass --home or set {rt.home_env}",
            )
        home = Path(home_text)
        if request.stage == "publish":
            return self._publish(rt, home, "publish")
        built = self._build(rt, home)
        if request.stage == "build" or not built.ok:
            return built
        published = self._publish(rt, home, "run")
        return ModelBetaBoardResult(
            ok=published.ok,
            stage="run",
            detail=f"{built.detail}; {published.detail}",
            wrote=built.wrote,
            met=built.met,
            counts=built.counts,
            targets=published.targets,
        )

    def _ledger_reads(
        self, rt: ModelBetaBoardRuntime, now: datetime
    ) -> dict[str, ModelSourceRead]:
        readings = rt.overlay.ledger_readings
        if not readings:
            return {}
        try:
            source = self._ledger or build_row_source(rt.overlay.ledger_read)
        except LedgerRouteError as exc:
            return {
                r.id: ModelSourceRead(
                    source_id=r.id,
                    kind="ledger_row",
                    read="unread",
                    read_at=now,
                    reason=str(exc)[:240],
                )
                for r in readings
            }
        return read_ledger_readings(source, readings, rt, now)

    def _build(self, rt: ModelBetaBoardRuntime, home: Path) -> ModelBetaBoardResult:
        now = self._clock()
        probe_reads = refresh_probes(rt, home, now, self._run, self._clock, self._cache)
        if self._cache is not None:
            write_tally_receipt(self._cache, home, BETA_BOARD_STEP)
        write_atomic(
            home / rt.reads_relpath, probe_reads.model_dump_json(indent=1).encode()
        )
        reads = {r.source_id: r for r in probe_reads.reads}
        reads.update(self._ledger_reads(rt, now))
        board = build_board(rt.overlay, reads, self._clock())
        target = home / rt.output_relpath
        write_atomic(target, board.model_dump_json(indent=1).encode())
        line = ", ".join(f"{k} {v}" for k, v in board.counts.items() if v)
        return ModelBetaBoardResult(
            ok=True,
            stage="build",
            detail=f"{board.milestone_id} {'met' if board.met else 'not met'}: {line}",
            wrote=str(target),
            met=board.met,
            counts=board.counts,
        )

    def _publish(
        self, rt: ModelBetaBoardRuntime, home: Path, stage: str
    ) -> ModelBetaBoardResult:
        source = home / rt.output_relpath
        try:
            data = source.read_bytes()
            board = ModelBetaBoard.model_validate_json(data)
        except (OSError, ValueError) as exc:
            return ModelBetaBoardResult(
                ok=False,
                stage=stage,
                detail=f"no board to publish at {rt.output_relpath}: {type(exc).__name__}",
            )
        page = render_page(board, rt.page_refresh_seconds).encode()
        files = ((rt.data_file, data), (rt.page_file, page))
        results: list[ModelTargetPublish] = []
        for target in rt.overlay.serve_targets:
            failure = ""
            for name, payload in files:
                argv = ssh_argv(
                    rt.transport.connect_timeout_seconds,
                    target.ssh_target,
                    remote_write_command(target.remote_dir, name),
                )
                code, tail = self._copy(
                    argv, payload, rt.transport.transfer_timeout_seconds
                )
                if code != 0:
                    failure = f"{name}: exit {code}: {tail}"
                    break
            results.append(
                ModelTargetPublish(
                    name=target.name,
                    serve_url=target.serve_url,
                    ok=not failure,
                    detail=failure,
                )
            )
        ok = all(r.ok for r in results)
        return ModelBetaBoardResult(
            ok=ok,
            stage=stage,
            detail="published to "
            + ", ".join(f"{r.name} ({'ok' if r.ok else 'failed'})" for r in results),
            wrote=str(source),
            met=board.met,
            counts=board.counts,
            targets=tuple(results),
        )
