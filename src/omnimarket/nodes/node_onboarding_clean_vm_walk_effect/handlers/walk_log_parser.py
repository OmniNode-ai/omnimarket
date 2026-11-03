# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure parsers for participant onboarding stdout; no host pseudo-phases."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import cast

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumPhaseResult,
    ModelDelegatedRun,
    ModelInstalledPin,
    ModelPhaseRecord,
)

_TIMESTAMP = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) ")
_START = re.compile(r"▶ Phase (\d+)/6: (.*?)(?:\s{2,}\(usually.*)?$")
_END = re.compile(
    r"[✔✘] Phase (\d+)/6 (PASSED|FAILED): (.*?) "
    r"\((?:(\d+)m )?(\d+)s\)"
)


def parse_phases(log: str) -> tuple[ModelPhaseRecord, ...]:
    """Keep observed phases in numeric order and attach failed-step diagnostics."""
    phases: dict[int, ModelPhaseRecord] = {}
    failed: int | None = None
    for line in log.splitlines():
        start = _START.search(line)
        end = _END.search(line)
        if start:
            failed = None
            number = int(start[1])
            phases[number] = ModelPhaseRecord(
                phase_number=number,
                name=start[2].strip(),
                result=EnumPhaseResult.SKIPPED,
                seconds=None,
            )
        elif end:
            number = int(end[1])
            is_failed = end[2] == "FAILED"
            failed = number if is_failed else None
            phases[number] = ModelPhaseRecord(
                phase_number=number,
                name=end[3],
                result=EnumPhaseResult.FAILED if is_failed else EnumPhaseResult.PASSED,
                seconds=float(int(end[4] or 0) * 60 + int(end[5])),
                failure_text=line.strip() if is_failed else None,
            )
        elif failed is not None and re.search(r"\s+(?:Step:|Last error:)", line):
            phase = phases[failed]
            phases[failed] = phase.model_copy(
                update={"failure_text": f"{phase.failure_text}\n{line.strip()}"}
            )
    return tuple(phases[n] for n in sorted(phases))


def parse_pins(log: str) -> tuple[ModelInstalledPin, ...]:
    """The last identity is authoritative; host/locus tokens are not pins."""
    identity = next(
        (line for line in reversed(log.splitlines()) if line.startswith("identity:")),
        "",
    )
    return tuple(
        ModelInstalledPin(name=m[1], version=m[2], source=m[3])
        for token in identity.split()[1:]
        if (m := re.fullmatch(r"([^=\s]+)=([^@\s]+)@([^\s]+)", token))
    )


def _delegate_lines(log: str) -> Iterator[tuple[int, dict[str, object]]]:
    for index, line in enumerate(log.splitlines()):
        if not line.startswith('{"skill_name":"node_delegate_skill_orchestrator"'):
            continue
        try:
            loaded: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            yield index, cast("dict[str, object]", loaded)


def _mapping(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _backend(payload: dict[str, object], lines: list[str], index: int) -> str | None:
    terminal = _mapping(_mapping(payload.get("result")).get("terminal_payload"))
    for key in ("backend", "provider", "backend_id", "route"):
        value = _string(terminal.get(key))
        if value and value != "UNATTRIBUTED":
            return value
    for line in lines[index + 1 :]:
        if _TIMESTAMP.match(line) or line.startswith('{"skill_name":'):
            break
        match = re.search(r"delegate artifacts \(route ([^\s)]+)", line)
        if match:
            return None if match[1] == "UNATTRIBUTED" else match[1]
    return None


def parse_delegated_run(log: str) -> ModelDelegatedRun:
    """First successful delegation wins; otherwise retain the final run id."""
    last_run: str | None = None
    any_failed = False
    lines = log.splitlines()
    for index, payload in _delegate_lines(log):
        last_run = _string(payload.get("run_id"))
        if payload.get("status") == "success":
            return ModelDelegatedRun(
                backend=_backend(payload, lines, index),
                status="success",
                run_id=last_run,
            )
        any_failed |= payload.get("status") == "failed"
    return ModelDelegatedRun(
        status="failed" if any_failed else "not_run", run_id=last_run
    )


def seconds_to_first_delegated_run(log: str) -> float | None:
    """Use the nearest preceding timestamp, including a walk across midnight."""
    success = next(
        (i for i, p in _delegate_lines(log) if p.get("status") == "success"), None
    )
    if success is None:
        return None
    start: int | None = None
    preceding: int | None = None
    for index, line in enumerate(log.splitlines()):
        if index >= success:
            break
        timestamp = _TIMESTAMP.match(line)
        if timestamp:
            preceding = (
                int(timestamp[1]) * 3600 + int(timestamp[2]) * 60 + int(timestamp[3])
            )
            if "▶ Phase 0/6" in line and start is None:
                start = preceding
    if start is None or preceding is None:
        return None
    return float((preceding - start) % 86400)


def parse_macos_version(log: str) -> str | None:
    match = re.search(r"^\s*macOS\s+(\d+(?:\.\d+)+)\s", log, re.MULTILINE)
    return match[1] if match else None
