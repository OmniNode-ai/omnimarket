# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lines`` operation of the factory floor effect: read one published record and print the board.

The only read is the record (the newest one the effect wrote, a record file named by the caller, or the history
snapshot at a time) and this node's own contract; nothing is recomputed and no other source is opened.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_effect.handlers.floor_lines_render import (
    is_stale,
    render_board,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_floor_lines import (
    ModelFloorLinesConfig,
    ModelFloorLinesContract,
    ModelFloorLinesRecord,
    ModelFloorLinesRequest,
    ModelFloorLinesResult,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    load_contract,
    resolve_home,
)

OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_lines_config(path: Path = OWN_CONTRACT) -> ModelFloorLinesConfig:
    """The contract's ``lines`` block; a missing key fails closed naming it."""
    try:
        return ModelFloorLinesContract.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        ).lines
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SourceRefusedError(
            f"{path}: lines block: {exc}; restore the contract's lines block and rerun"
        ) from exc


def _history_pattern(cfg: ModelFloorLinesConfig) -> re.Pattern[str]:
    """Any file name that ends in the timestamp format: ``<record stem>-<stamp>.<ext>``."""
    probe = datetime(2000, 1, 2, 3, 4, 5, 678901, tzinfo=UTC).strftime(
        cfg.history_timestamp_format
    )
    shape = re.escape(probe)
    for token in re.findall(r"\d+", probe):
        shape = shape.replace(re.escape(token), f"\\d{{{len(token)}}}", 1)
    return re.compile(r"-(?P<stamp>" + shape + r")\.[A-Za-z0-9]+$")


def newest_snapshot_at(
    directory: Path, at: datetime, cfg: ModelFloorLinesConfig
) -> Path | None:
    """The history snapshot with the latest time at or before ``at``, or ``None``."""
    pattern = _history_pattern(cfg)
    best: tuple[datetime, Path] | None = None
    if not directory.is_dir():
        return None
    for entry in sorted(directory.iterdir()):
        match = pattern.search(entry.name)
        if match is None:
            continue
        try:
            moment = datetime.strptime(
                match.group("stamp"), cfg.history_timestamp_format
            ).replace(tzinfo=UTC)
        except ValueError:
            continue
        if moment <= at and (best is None or moment > best[0]):
            best = (moment, entry)
    return best[1] if best else None


class HandlerFloorLines:
    """``handle(request) -> result``: the board text of one record, exit status non-zero when it reads unread."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        config: ModelFloorLinesConfig | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._config = config

    def _record_path(
        self, request: ModelFloorLinesRequest, cfg: ModelFloorLinesConfig
    ) -> Path | None:
        if request.record_path:
            return Path(request.record_path)
        if request.history_at is not None:
            root = request.history_dir or self._environ.get(cfg.history_dir_env, "")
            if not root.strip():
                raise SourceRefusedError(
                    f"{cfg.history_dir_env} is not set and no history directory was given (fail fast, no default)"
                )
            return newest_snapshot_at(
                Path(root) / cfg.history_subdirectory, request.history_at, cfg
            )
        runtime = load_contract(OWN_CONTRACT).runtime
        home = resolve_home(self._environ, runtime.home_env, request.home)
        return home / runtime.output.relpath

    def handle(self, request: ModelFloorLinesRequest) -> ModelFloorLinesResult:
        cfg = self._config or load_lines_config()
        now = request.now or datetime.now(UTC)
        path = self._record_path(request, cfg)
        absent = cfg.words.unread_absent
        if path is None or not path.is_file():
            return ModelFloorLinesResult(
                board=absent.format(path=str(path) if path else "no snapshot") + "\n",
                record_time="",
                exit_status=cfg.unread_exit_status,
            )
        try:
            record = ModelFloorLinesRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError):
            return ModelFloorLinesResult(
                board=cfg.words.unread_unparsable.format(path=str(path)) + "\n",
                record_time="",
                exit_status=cfg.unread_exit_status,
            )
        return ModelFloorLinesResult(
            board=render_board(record, now, cfg),
            record_time=record.generated_at.astimezone(UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            exit_status=cfg.unread_exit_status if is_stale(record, now, cfg) else 0,
        )
