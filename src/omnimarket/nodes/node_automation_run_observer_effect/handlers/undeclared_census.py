# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Units of ours that run with no overlay entry.

Ours is whatever the caller's census scope says: a launchd label with one of
its prefixes or named in its known labels, a unit whose program runs under one
of its path roots, a cron line that runs such a path. The scope is empty until
a deployment supplies it, and an empty scope finds nothing.
"""

import plistlib
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_automation_run_observer_effect.models import (
    ModelUndeclaredCensusScope,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.host_command_runner import (
    ProtocolHostCommandRunner,
)

_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INVALID_ID_CHARS = re.compile(r"[^a-z0-9._/-]+")


class ModelUndeclaredUnit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: str
    native_id: str
    detail: str


class ModelCensusRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    units: tuple[ModelUndeclaredUnit, ...] = ()
    unreadable: str | None = None


def undeclared_process_id(host: str, native_id: str) -> str:
    slug = _INVALID_ID_CHARS.sub("-", native_id.lower()).strip("-./")
    return f"{host}/undeclared/{slug or 'unit'}"


def run_census(
    host: str,
    scope: ModelUndeclaredCensusScope,
    declared: frozenset[str],
    runner: ProtocolHostCommandRunner,
) -> ModelCensusRead:
    if scope.is_empty:
        return ModelCensusRead()
    units: list[ModelUndeclaredUnit] = []
    unreadable: str | None = None
    if scope.label_prefixes or scope.known_labels or scope.path_roots:
        loaded = _loaded_labels(runner)
        if isinstance(loaded, str):
            unreadable = loaded
        else:
            programs = _plist_programs(scope.launchd_agent_dirs)
            for label in loaded:
                if label in declared:
                    continue
                why = _ours(label, programs.get(label, ""), scope)
                if why is not None:
                    units.append(
                        ModelUndeclaredUnit(
                            process_id=undeclared_process_id(host, label),
                            native_id=label,
                            detail=f"launchd label {label} is running ({why}) with no entry",
                        )
                    )
    for cron_file in scope.cron_files:
        units.extend(_cron_units(host, cron_file, scope, declared))
    return ModelCensusRead(units=tuple(units), unreadable=unreadable)


def _loaded_labels(runner: ProtocolHostCommandRunner) -> list[str] | str:
    try:
        outcome = runner.run(["launchctl", "list"])
    except OSError as exc:
        return f"launchctl list: {exc}"
    if outcome.returncode != 0:
        return f"launchctl list exited {outcome.returncode}: {outcome.stderr.strip()}"
    labels = []
    for line in outcome.stdout.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) == 3 and fields[2].strip():
            labels.append(fields[2].strip())
    return labels


def _plist_programs(agent_dirs: tuple[str, ...]) -> dict[str, str]:
    programs: dict[str, str] = {}
    for directory in agent_dirs:
        for plist in sorted(Path(directory).glob("*.plist")):
            try:
                data = plistlib.loads(plist.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            label = data.get("Label")
            if not isinstance(label, str):
                continue
            argv = data.get("ProgramArguments") or []
            program = data.get("Program")
            parts = [program] if isinstance(program, str) else []
            parts.extend(arg for arg in argv if isinstance(arg, str))
            programs[label] = " ".join(parts)
    return programs


def _ours(label: str, program: str, scope: ModelUndeclaredCensusScope) -> str | None:
    if label in scope.known_labels:
        return "named in a unit template or installer contract"
    if any(label.startswith(prefix) for prefix in scope.label_prefixes):
        return "carries one of our prefixes"
    if any(root in program for root in scope.path_roots):
        return "runs a path inside one of our clones or state directories"
    return None


def _cron_units(
    host: str,
    cron_file: str,
    scope: ModelUndeclaredCensusScope,
    declared: frozenset[str],
) -> list[ModelUndeclaredUnit]:
    path = Path(cron_file)
    if not path.exists():
        return []
    units = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#") or _ENV_LINE.match(text):
            continue
        native_id = f"{cron_file}:{number}"
        if native_id in declared or not any(root in text for root in scope.path_roots):
            continue
        units.append(
            ModelUndeclaredUnit(
                process_id=undeclared_process_id(host, native_id),
                native_id=native_id,
                detail=f"cron line {native_id} runs a path of ours with no entry",
            )
        )
    return units
