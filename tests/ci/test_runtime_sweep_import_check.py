# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the canonical single-repo entry-point import probe (OMN-13589).

The freestanding CI runtime-sweep script was deleted and its single-repo "do
the entry points load?" probe was absorbed into the canonical
``node_runtime_sweep``: the harness (``__main__.collect_entry_point_probes``)
walks ``pyproject.toml`` and imports each node, and the pure
``NodeRuntimeSweep`` handler turns failed probes into ``BROKEN_ENTRY_POINT``
findings via the REGISTRATION phase. These tests cover both halves.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from omnimarket.nodes.node_runtime_sweep import __main__ as harness
from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    EnumFindingType,
    EnumSweepCheck,
    ModelContractInput,
    ModelEntryPointProbe,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)

# ---------------------------------------------------------------------------
# Harness import-ref helpers (ported verbatim from the deleted script)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_import_model_ref_skips_symbolic_model_names() -> None:
    """A bare symbolic model name (no dotted module path) is not imported."""
    harness._import_model_ref("ModelCanaryCommandPayload")


@pytest.mark.unit
def test_import_model_ref_imports_structured_module_name() -> None:
    """A structured {module, name} ref importing an existing symbol succeeds."""
    harness._import_model_ref(
        {
            "module": "omnimarket.nodes.node_runtime_sweep.__main__",
            "name": "LIFECYCLE_EXEMPTIONS",
        }
    )


@pytest.mark.unit
def test_import_model_ref_rejects_missing_structured_ref() -> None:
    """A structured ref pointing at a missing symbol raises."""
    with pytest.raises(AttributeError):
        harness._import_model_ref(
            {
                "module": "omnimarket.nodes.node_runtime_sweep.__main__",
                "name": "MissingModel",
            }
        )


# ---------------------------------------------------------------------------
# collect_entry_point_probes (the harness I/O boundary)
# ---------------------------------------------------------------------------


def _write_temp_repo(tmp_path: Path, entry_points: str) -> Path:
    """Write a minimal repo_root with a pyproject + src tree for probing."""
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        dedent(
            f"""\
            [project]
            name = "temp-probe-repo"
            version = "0.0.0"

            [project.entry-points."onex.nodes"]
            {entry_points}
            """
        )
    )
    return tmp_path


@pytest.mark.unit
def test_collect_probes_flags_missing_module_directory(tmp_path: Path) -> None:
    """An entry point whose module dir is absent yields ok=False + exact reason."""
    repo_root = _write_temp_repo(tmp_path, 'node_ghost = "pkg.nodes.node_ghost"')
    probes = harness.collect_entry_point_probes(repo_root)

    assert len(probes) == 1
    assert probes[0].node_name == "node_ghost"
    assert probes[0].module_path == "pkg.nodes.node_ghost"
    assert probes[0].ok is False
    assert probes[0].reason == "module directory missing"


@pytest.mark.unit
def test_collect_probes_flags_missing_init(tmp_path: Path) -> None:
    """A module dir without __init__.py yields the '__init__.py missing' reason."""
    repo_root = _write_temp_repo(tmp_path, 'node_noinit = "pkg.nodes.node_noinit"')
    (repo_root / "src" / "pkg" / "nodes" / "node_noinit").mkdir(parents=True)
    probes = harness.collect_entry_point_probes(repo_root)

    assert probes[0].ok is False
    assert probes[0].reason == "__init__.py missing"


@pytest.mark.unit
def test_collect_probes_flags_missing_contract(tmp_path: Path) -> None:
    """A package without contract.yaml yields the 'contract.yaml missing' reason."""
    repo_root = _write_temp_repo(
        tmp_path, 'node_nocontract = "pkg.nodes.node_nocontract"'
    )
    node_dir = repo_root / "src" / "pkg" / "nodes" / "node_nocontract"
    node_dir.mkdir(parents=True)
    (node_dir / "__init__.py").write_text("")
    probes = harness.collect_entry_point_probes(repo_root)

    assert probes[0].ok is False
    assert probes[0].reason == "contract.yaml missing"


@pytest.mark.unit
def test_collect_probes_flags_missing_description(tmp_path: Path) -> None:
    """A contract.yaml without a description yields the exact reason string."""
    repo_root = _write_temp_repo(tmp_path, 'node_nodesc = "pkg.nodes.node_nodesc"')
    node_dir = repo_root / "src" / "pkg" / "nodes" / "node_nodesc"
    node_dir.mkdir(parents=True)
    (node_dir / "__init__.py").write_text("")
    (node_dir / "contract.yaml").write_text("name: node_nodesc\n")
    probes = harness.collect_entry_point_probes(repo_root)

    assert probes[0].ok is False
    assert probes[0].reason == "contract.yaml missing description field"


@pytest.mark.unit
def test_collect_probes_raises_without_entry_points(tmp_path: Path) -> None:
    """A pyproject with no onex.nodes entry points raises (mirrors script error)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        dedent(
            """\
            [project]
            name = "empty-repo"
            version = "0.0.0"
            """
        )
    )
    with pytest.raises(ValueError, match=r"onex\.nodes"):
        harness.collect_entry_point_probes(tmp_path)


@pytest.mark.unit
def test_collect_probes_real_repo_all_ok() -> None:
    """Probing this repo's own pyproject yields findings-free probes.

    repo_root resolves the same way the --import-check CLI path does:
    __main__.py is at <repo>/src/omnimarket/nodes/node_runtime_sweep/, so
    parents[4] is the repo root.
    """
    repo_root = Path(harness.__file__).resolve().parents[4]
    probes = harness.collect_entry_point_probes(repo_root)

    assert probes  # repo declares onex.nodes entry points
    broken = [p for p in probes if not p.ok]
    assert broken == [], f"unexpected broken entry points: {broken}"


# ---------------------------------------------------------------------------
# Pure node: probe -> BROKEN_ENTRY_POINT finding (REGISTRATION phase)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_broken_probe_yields_broken_entry_point_finding() -> None:
    """A failed probe becomes one CRITICAL BROKEN_ENTRY_POINT finding."""
    request = RuntimeSweepRequest(
        entry_point_probes=[
            ModelEntryPointProbe(
                node_name="node_broken",
                module_path="pkg.nodes.node_broken",
                ok=False,
                reason="entry point import failed: boom",
            )
        ],
        enabled_checks=[EnumSweepCheck.REGISTRATION],
    )
    result = NodeRuntimeSweep().handle(request)

    assert result.status == "findings"
    assert result.entry_points_checked == 1
    findings = [
        f
        for f in result.findings
        if f.finding_type == EnumFindingType.BROKEN_ENTRY_POINT
    ]
    assert len(findings) == 1
    assert findings[0].subject == "node_broken"
    assert findings[0].severity == "CRITICAL"
    assert findings[0].message == (
        "pkg.nodes.node_broken: entry point import failed: boom"
    )


@pytest.mark.unit
def test_clean_probes_yield_no_findings() -> None:
    """All-ok probes produce no findings and a clean status (exit-0 case)."""
    request = RuntimeSweepRequest(
        entry_point_probes=[
            ModelEntryPointProbe(
                node_name="node_a", module_path="pkg.nodes.node_a", ok=True
            ),
            ModelEntryPointProbe(
                node_name="node_b", module_path="pkg.nodes.node_b", ok=True
            ),
        ],
        enabled_checks=[EnumSweepCheck.REGISTRATION],
    )
    result = NodeRuntimeSweep().handle(request)

    assert result.status == "clean"
    assert result.total_findings == 0
    assert result.entry_points_checked == 2


@pytest.mark.unit
def test_registration_scoped_request_ignores_contracts() -> None:
    """enabled_checks=[REGISTRATION] does not run symmetry/description phases.

    Single-repo scoping is the whole point: a request that also carries
    asymmetric contracts must NOT emit SYMMETRY/PRODUCER_ONLY/CONSUMER_ONLY or
    description findings when only REGISTRATION is enabled.
    """
    request = RuntimeSweepRequest(
        contracts=[
            ModelContractInput(
                node_name="node_asym",
                description="",  # would trip MISSING_DESCRIPTION if DESCRIPTION ran
                publish_topics=["onex.evt.orphan.topic.v1"],  # producer-only
            )
        ],
        entry_point_probes=[
            ModelEntryPointProbe(
                node_name="node_broken",
                module_path="pkg.nodes.node_broken",
                ok=False,
                reason="contract.yaml missing",
            )
        ],
        enabled_checks=[EnumSweepCheck.REGISTRATION],
    )
    result = NodeRuntimeSweep().handle(request)

    types = {f.finding_type for f in result.findings}
    assert types == {EnumFindingType.BROKEN_ENTRY_POINT}
    assert EnumFindingType.PRODUCER_ONLY not in types
    assert EnumFindingType.CONSUMER_ONLY not in types
    assert EnumFindingType.MISSING_DESCRIPTION not in types
