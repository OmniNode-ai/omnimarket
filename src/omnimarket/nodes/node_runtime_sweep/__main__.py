# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_runtime_sweep.

Usage:
    python -m omnimarket.nodes.node_runtime_sweep \
        --scope all-repos \
        --dry-run

Outputs JSON to stdout: RuntimeSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import yaml

from omnimarket.nodes.node_runtime_sweep.collection import collect_contracts
from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    EnumSweepCheck,
    ModelContractInput,
    ModelEntryPointProbe,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)

_log = logging.getLogger(__name__)

# OMN-13589: lifecycle states whose entry-point import probe is skipped.
# Ported verbatim from the (now-deleted) freestanding CI runtime-sweep script.
LIFECYCLE_EXEMPTIONS = {"deprecated", "experimental"}


def _import_dotted_path(dotted_path: str) -> None:
    module_path, _, attr = dotted_path.rpartition(".")
    if not module_path or not attr:
        raise ValueError(
            f"Expected dotted path with module and attribute: {dotted_path}"
        )
    module = import_module(module_path)
    getattr(module, attr)


def _is_dotted_import_path(value: str) -> bool:
    module_path, _, attr = value.rpartition(".")
    return bool(module_path and attr)


def _import_model_ref(raw_ref: object) -> None:
    """Import contract model refs when they include an importable module path."""
    if isinstance(raw_ref, str):
        if _is_dotted_import_path(raw_ref):
            _import_dotted_path(raw_ref)
        return
    if not isinstance(raw_ref, dict):
        return

    module = raw_ref.get("module")
    class_name = raw_ref.get("class") or raw_ref.get("name")
    if isinstance(module, str) and isinstance(class_name, str):
        _import_dotted_path(f"{module}.{class_name}")


def _contract_lifecycle(content: dict[object, object]) -> str:
    candidates: list[object] = [content.get("lifecycle"), content.get("status")]
    for nested_key in ("metadata", "descriptor"):
        nested = content.get(nested_key)
        if isinstance(nested, dict):
            candidates.extend([nested.get("lifecycle"), nested.get("status")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return ""


def _probe_entry_point(
    node_name: str, module_path: str, src_root: Path
) -> ModelEntryPointProbe:
    """Run the structural + import checks for one onex.nodes entry point.

    Ported verbatim from the (now-deleted) freestanding CI runtime-sweep
    script's ``--import-check`` path. Returns a probe with ``ok=False`` and the
    exact reason string on the first failing check, or ``ok=True`` when every
    check passes (or the contract is lifecycle-exempt before the import probes).
    """
    node_dir = src_root / Path(*module_path.split("."))

    if not node_dir.exists():
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason="module directory missing",
        )

    if not (node_dir / "__init__.py").exists():
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason="__init__.py missing",
        )

    contract = node_dir / "contract.yaml"
    if not contract.exists():
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason="contract.yaml missing",
        )

    content = yaml.safe_load(contract.read_text())
    if not isinstance(content, dict):
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason="contract.yaml is not a mapping",
        )

    if not content.get("description"):
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason="contract.yaml missing description field",
        )

    # Lifecycle-exempt contracts skip the import probes and pass structurally.
    if _contract_lifecycle(content) in LIFECYCLE_EXEMPTIONS:
        return ModelEntryPointProbe(
            node_name=node_name, module_path=module_path, ok=True
        )

    try:
        import_module(module_path)
    except Exception as exc:
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason=f"entry point import failed: {exc}",
        )

    handler = content.get("handler")
    if not isinstance(handler, dict):
        return ModelEntryPointProbe(
            node_name=node_name, module_path=module_path, ok=True
        )

    handler_module = handler.get("module")
    handler_class = handler.get("class")
    if not isinstance(handler_module, str) or not isinstance(handler_class, str):
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason="contract.yaml missing handler module/class",
        )

    try:
        _import_dotted_path(f"{handler_module}.{handler_class}")
    except Exception as exc:
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason=f"handler import failed: {exc}",
        )

    input_model = handler.get("input_model")
    try:
        _import_model_ref(input_model)
    except Exception as exc:
        return ModelEntryPointProbe(
            node_name=node_name,
            module_path=module_path,
            ok=False,
            reason=f"input_model import failed: {exc}",
        )

    for model_key in ("input_model", "output_model"):
        try:
            _import_model_ref(content.get(model_key))
        except Exception as exc:
            return ModelEntryPointProbe(
                node_name=node_name,
                module_path=module_path,
                ok=False,
                reason=f"{model_key} import failed: {exc}",
            )

    return ModelEntryPointProbe(node_name=node_name, module_path=module_path, ok=True)


def collect_entry_point_probes(repo_root: Path) -> list[ModelEntryPointProbe]:
    """Probe every ``onex.nodes`` entry point declared in ``repo_root``'s pyproject.

    Reads ``<repo_root>/pyproject.toml`` ``[project.entry-points."onex.nodes"]``
    and runs the structural + import checks for each node (single-repo, no
    ``$OMNI_HOME`` walk). One ModelEntryPointProbe per node. This is the I/O
    boundary — imports happen here, never in the pure handler.
    """
    pyproject_path = repo_root / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        config = tomllib.load(f)

    entry_points: dict[str, str] = (
        config.get("project", {}).get("entry-points", {}).get("onex.nodes", {})
    )
    if not entry_points:
        raise ValueError(
            'No [project.entry-points."onex.nodes"] found in '
            f"{pyproject_path}"  # local-path-ok: dynamic repo_root, not hardcoded
        )

    src_root = repo_root / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

    return [
        _probe_entry_point(node_name, module_path, src_root)
        for node_name, module_path in entry_points.items()
    ]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    omni_home = os.environ.get("OMNI_HOME")

    parser = argparse.ArgumentParser(
        description="Runtime registration and wiring verification."
    )
    parser.add_argument(
        "--scope",
        default="all-repos",
        choices=["all-repos", "omnidash-only"],
        help="Check scope (default: all-repos)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report findings without creating Linear tickets",
    )
    parser.add_argument(
        "--live-consumer-profiles",
        default=None,
        help=(
            "Comma-separated runtime_profiles that currently have a live "
            "consumer group on the broker (e.g. 'main,effects,workers'). When "
            "provided, runs the OMN-12957 dual check: any subscribing node whose "
            "declared profiles are all absent from this census is flagged as a "
            "silent orphan. Omit to skip the live-census check."
        ),
    )
    parser.add_argument(
        "--import-check",
        action="store_true",
        default=False,
        help=(
            "Single-repo entry-point import probe (OMN-13589). Does NOT walk "
            "$OMNI_HOME; instead probes this repo's "
            '[project.entry-points."onex.nodes"] (module dir, __init__.py, '
            "contract.yaml, description, and importability of the entry-point "
            "module, handler, and input/output models), then runs the "
            "REGISTRATION phase only. Exits 1 if any entry point is broken."
        ),
    )

    args = parser.parse_args()

    # OMN-13589: single-repo import-probe path. Mutually exclusive with the
    # cross-repo $OMNI_HOME walk — the harness collects per-entry-point probes
    # and the pure node turns them into BROKEN_ENTRY_POINT findings.
    if args.import_check:
        # __file__ = .../<repo>/src/omnimarket/nodes/node_runtime_sweep/__main__.py
        # parents: [0]=node_runtime_sweep [1]=nodes [2]=omnimarket [3]=src
        #          [4]=<repo root>
        repo_root = Path(__file__).resolve().parents[4]
        probes = collect_entry_point_probes(repo_root)
        request = RuntimeSweepRequest(
            entry_point_probes=probes,
            enabled_checks=[EnumSweepCheck.REGISTRATION],
            dry_run=True,
        )
        result = NodeRuntimeSweep().handle(request)
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        sys.exit(1 if result.findings else 0)

    if not omni_home:
        _log.warning("OMNI_HOME is not set — contract collection skipped")
        contracts: list[ModelContractInput] = []
    else:
        contracts = collect_contracts(omni_home, args.scope)
    if not contracts:
        _log.warning("no contract.yaml files found")

    all_publish: list[str] = []
    all_subscribe: list[str] = []
    for c in contracts:
        all_publish.extend(c.publish_topics)
        all_subscribe.extend(c.subscribe_topics)

    live_consumer_profiles: list[str] | None = None
    if args.live_consumer_profiles is not None:
        live_consumer_profiles = [
            p.strip().lower()
            for p in args.live_consumer_profiles.split(",")
            if p.strip()
        ]

    request = RuntimeSweepRequest(
        contracts=contracts,
        topic_producers=all_publish,
        topic_consumers=all_subscribe,
        live_consumer_profiles=live_consumer_profiles,
        dry_run=args.dry_run,
    )

    handler = NodeRuntimeSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.findings:
        sys.exit(1)


if __name__ == "__main__":
    main()
