# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAskStateReport: read the operator capture store and the ledger, then fold them (OMN-21713).

The file reading is this effect node's; the decision is node_ask_state_compute's, called in
process, with the config block of that node's contract.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

import omnimarket.nodes.node_ask_state_compute as compute_node
from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
    HandlerAskState,
)
from omnimarket.nodes.node_ask_state_compute.models.model_ask_state import (
    ModelAskPrompt,
    ModelAskPromptItem,
    ModelAskStateConfig,
    ModelAskStateReport,
    ModelAskStateRequest,
)
from omnimarket.nodes.node_ask_state_effect.models.model_ask_state_read import (
    ModelAskStateReadRequest,
)

EFFECT_CONTRACT = Path(__file__).parent.parent / "contract.yaml"
COMPUTE_CONTRACT = Path(compute_node.__file__).with_name("contract.yaml")
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ARCHIVE_DAY = re.compile(r"(\d{4}-\d{2}-\d{2})")
_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")


class AskStateSourceError(Exception):
    """A source the report needs is missing or unreadable; the report is not guessed."""


def load_contract(path: Path = EFFECT_CONTRACT) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AskStateSourceError(f"{path} is not a contract mapping")
    return loaded


def load_config(contract: dict[str, Any] | None = None) -> ModelAskStateConfig:
    """The compute node's config block, validated; the contract of the node that owns it."""
    return ModelAskStateConfig.model_validate(
        (contract or load_contract(COMPUTE_CONTRACT))["config"]
    )


def parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def read_prompts(captures: Path, since: datetime) -> tuple[ModelAskPrompt, ...]:
    """Operator prompts of the capture store at or after ``since``; machine text is left out."""
    if not captures.is_file():
        raise AskStateSourceError(f"operator capture store {captures} is not readable")
    prompts: dict[str, ModelAskPrompt] = {}
    for line in captures.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
            said = parse_time(str(record["received_at"]))
            capture_id = str(record["capture_id"])
        except (ValueError, KeyError, TypeError):
            continue  # a torn or foreign line is not an operator prompt
        classification = record.get("classification") or {}
        if (
            record.get("status") == "machine"
            or classification.get("origin") == "machine"
        ):
            continue
        if said < since or capture_id in prompts:
            continue
        items = tuple(
            ModelAskPromptItem(
                item_id=str(item["item_id"]),
                kind=str(item["kind"]),
                quote=str(item["quote"]),
                subject=str(item.get("subject", "")),
                confidence=float(item.get("confidence", 0.0)),
            )
            for item in classification.get("items", [])
            if item.get("quote")
        )
        prompts[capture_id] = ModelAskPrompt(
            capture_id=capture_id,
            session_id=str(record.get("session_id", "")),
            said_at=said,
            text=str(record.get("text", "")),
            items=items,
        )
    return tuple(prompts.values())


def read_ledger_rows(
    ledger: Path,
    since: datetime,
    *,
    archive_glob: str,
    archive_dir: str,
    archives: bool,
) -> tuple[str, ...]:
    """Every ledger row stamped at or after ``since``, archive rolls first, live file last."""
    if not ledger.is_file():
        raise AskStateSourceError(f"ledger {ledger} is not readable")
    floor = since.strftime(_STAMP_FORMAT)
    paths: list[Path] = []
    folder = ledger.parent / archive_dir
    if archives and folder.is_dir():
        for path in sorted(folder.glob(archive_glob)):
            day = _ARCHIVE_DAY.search(path.name)
            if day is None or day.group(1) >= floor[:10]:
                paths.append(path)
    paths.append(ledger)
    rows: list[str] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            rows.extend(
                line.rstrip("\n")
                for line in handle
                if _ROW_START.match(line) and line[:20] >= floor
            )
    return tuple(rows)


class HandlerAskStateReport:
    """Read the sources named by the request and return the compute node's report."""

    def handle(self, request: ModelAskStateReadRequest) -> ModelAskStateReport:
        runtime = load_contract()["runtime"]
        config = load_config()
        # Restatements and dependency edges look back past the window; the compute node reports
        # only the asks stated at or after ``since``.
        floor = request.since - timedelta(days=config.restate_lookback_days)
        return HandlerAskState().handle(
            ModelAskStateRequest(
                prompts=read_prompts(
                    request.captures_dir / runtime["captures_file"], floor
                ),
                rows=read_ledger_rows(
                    request.ledger_path,
                    floor,
                    archive_glob=runtime["archive_glob"],
                    archive_dir=runtime["archive_dir"],
                    archives=request.archives,
                ),
                now=request.now,
                since=request.since,
                config=config,
            )
        )


__all__ = [
    "AskStateSourceError",
    "HandlerAskStateReport",
    "load_config",
    "load_contract",
    "parse_time",
    "read_ledger_rows",
    "read_prompts",
]
