# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reduce contract-walker output into delegation chain obligations."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

WALKER_REPORT = Path(__file__).with_name("walker_report.json")

Step = tuple[str, str, str]
ProjectedKey = tuple[str, tuple[Step, ...]]


@dataclass(frozen=True)
class Obligation:
    """One deduplicated owner-component path from the walker report."""

    path_id: str
    kind: str
    steps: tuple[Step, ...]


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return cast("Mapping[str, object]", value)


def _sequence(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    return cast("Sequence[object]", value)


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _owner_state(value: object, *, label: str) -> str:
    states = _sequence(value, label=label)
    if not states:
        raise ValueError(f"{label} must contain the owner state")
    return _string(states[0], label=f"{label}[0]")


def reduce_walker_workflow(
    full_report: Mapping[str, object],
    *,
    workflow_owner: str,
    core_sha: str,
    omnimarket_sha: str,
    walker_command: str,
    full_report_sha256: str,
) -> dict[str, object]:
    """Project and deduplicate one workflow onto its owner component."""
    workflows = _sequence(full_report.get("workflows"), label="workflows")
    matches = [
        workflow
        for item in workflows
        if (workflow := _mapping(item, label="workflow")).get("workflow_owner")
        == workflow_owner
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one workflow owned by {workflow_owner!r}; "
            f"found {len(matches)}"
        )
    workflow = matches[0]
    raw_paths = _sequence(workflow.get("paths"), label="workflow.paths")

    raw_kind_counts: Counter[str] = Counter()
    projected_counts: Counter[ProjectedKey] = Counter()
    for raw_path in raw_paths:
        path = _mapping(raw_path, label="path")
        kind = _string(path.get("kind"), label="path.kind")
        raw_kind_counts[kind] += 1
        projected_steps: list[Step] = []
        for raw_step in _sequence(path.get("steps"), label="path.steps"):
            step = _mapping(raw_step, label="step")
            from_state = _owner_state(step.get("from_state"), label="from_state")
            to_state = _owner_state(step.get("to_state"), label="to_state")
            if from_state != to_state:
                projected_steps.append(
                    (
                        from_state,
                        _string(step.get("trigger"), label="step.trigger"),
                        to_state,
                    )
                )
        projected_counts[(kind, tuple(projected_steps))] += 1

    paths: list[dict[str, object]] = []
    for (kind, steps), raw_path_count in projected_counts.items():
        triggers = ">".join(trigger for _, trigger, _ in steps)
        paths.append(
            {
                "path_id": f"{kind}:{triggers}",
                "kind": kind,
                "steps": [list(step) for step in steps],
                "raw_path_count": raw_path_count,
            }
        )
    paths.sort(key=lambda path: cast(str, path["path_id"]))

    raw_counts = {
        "paths": len(raw_paths),
        "golden": raw_kind_counts["golden"],
        "error": raw_kind_counts["error"],
        "open": raw_kind_counts["open"],
    }
    components = [
        _string(_mapping(item, label="component").get("node"), label="component.node")
        for item in _sequence(workflow.get("components"), label="workflow.components")
    ]
    sync_triggers = [
        _string(item, label="sync_trigger")
        for item in _sequence(
            workflow.get("sync_triggers"), label="workflow.sync_triggers"
        )
    ]
    error_edges = list(
        _sequence(workflow.get("error_edges"), label="workflow.error_edges")
    )
    return {
        "schema_version": 1,
        "workflow_owner": workflow_owner,
        "core_sha": core_sha,
        "omnimarket_sha": omnimarket_sha,
        "walker_command": walker_command,
        "full_report_sha256": full_report_sha256,
        "components": components,
        "sync_triggers": sync_triggers,
        "raw_counts": raw_counts,
        "error_edges": error_edges,
        "projection": "owner-component state changes, deduplicated",
        "paths": paths,
    }


def load_obligations(path: Path = WALKER_REPORT) -> tuple[Obligation, ...]:
    """Load validated obligations from a reduced walker report."""
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    report = _mapping(loaded, label="walker report")
    core_sha = report.get("core_sha")
    if not isinstance(core_sha, str) or not core_sha:
        raise ValueError("walker report is missing core_sha")

    obligations: list[Obligation] = []
    seen_path_ids: set[str] = set()
    for raw_path in _sequence(report.get("paths"), label="walker report paths"):
        path_data = _mapping(raw_path, label="obligation")
        path_id = _string(path_data.get("path_id"), label="obligation.path_id")
        if path_id in seen_path_ids:
            raise ValueError(f"duplicate path_id: {path_id}")
        seen_path_ids.add(path_id)
        steps = tuple(
            (
                _string(step_values[0], label="step.from"),
                _string(step_values[1], label="step.trigger"),
                _string(step_values[2], label="step.to"),
            )
            for raw_step in _sequence(path_data.get("steps"), label="obligation.steps")
            if len(step_values := _sequence(raw_step, label="obligation.step")) == 3
        )
        if len(steps) != len(
            _sequence(path_data.get("steps"), label="obligation.steps")
        ):
            raise ValueError(f"every step for {path_id} must contain three strings")
        obligations.append(
            Obligation(
                path_id=path_id,
                kind=_string(path_data.get("kind"), label="obligation.kind"),
                steps=steps,
            )
        )
    return tuple(obligations)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-report", type=Path, required=True)
    parser.add_argument("--workflow-owner", required=True)
    parser.add_argument("--core-sha", required=True)
    parser.add_argument("--omnimarket-sha", required=True)
    parser.add_argument("--walker-command", required=True)
    parser.add_argument("--out", type=Path, default=WALKER_REPORT)
    return parser.parse_args()


def main() -> None:
    """Run the walker-report reduction CLI."""
    args = _parse_args()
    full_report_path = cast(Path, args.full_report)
    full_report_bytes = full_report_path.read_bytes()
    loaded: object = json.loads(full_report_bytes)
    full_report = _mapping(loaded, label="full report")
    reduced = reduce_walker_workflow(
        full_report,
        workflow_owner=cast(str, args.workflow_owner),
        core_sha=cast(str, args.core_sha),
        omnimarket_sha=cast(str, args.omnimarket_sha),
        walker_command=cast(str, args.walker_command),
        full_report_sha256=hashlib.sha256(full_report_bytes).hexdigest(),
    )
    out = cast(Path, args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(reduced, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
