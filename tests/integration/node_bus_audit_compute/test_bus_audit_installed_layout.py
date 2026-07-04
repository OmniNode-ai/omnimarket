# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed-layout and fail-loud regression coverage for node_bus_audit_compute.

Defect class under enforcement: the handler previously resolved its default
``topics.yaml`` registry and contract root via
``Path(__file__).resolve().parents[5] / "src/omnimarket/..."`` — a repo-root
reconstruction that only exists in a source checkout. In an installed wheel
(``<site-packages>/omnimarket/...``) it pointed at
``<venv>/lib/pythonX.Y/src/...``, the registry was "not found", and the skill
audited zero topics — a vacuous pass on a verification surface.

``test_bus_audit_defaults_resolve_in_installed_layout`` mechanically recreates
the installed layout (package copied to a site-packages-like directory with no
``src/`` parent and no repo root) and fails if default resolution ever
regresses to repo-relative reconstruction.

The remaining tests pin the fail-loud contract: a missing packaged registry
and vacuous zero-work audits raise instead of returning a clean result, and
the CLI exits non-zero on ERROR status.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_bus_audit_compute.handlers import (
    handler_bus_audit_compute as handler_module,
)
from omnimarket.nodes.node_bus_audit_compute.handlers.handler_bus_audit_compute import (
    HandlerBusAuditCompute,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_request import (
    ModelBusAuditComputeRequest,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_result import (
    EnumBusAuditFindingType,
    EnumBusAuditStatus,
    ModelBusAuditComputeResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_SRC = _REPO_ROOT / "src" / "omnimarket"

# Runs inside the simulated site-packages environment. Asserts the shadowed
# package actually won import resolution, then runs the handler with an
# all-defaults request (the exact invocation `onex skill bus_audit` performs).
_INSTALLED_LAYOUT_SCRIPT = """
import json
import sys

site_dir = sys.argv[1]
sys.path.insert(0, site_dir)
import omnimarket

if not omnimarket.__file__.startswith(site_dir):
    raise SystemExit(
        "simulation invalid: omnimarket resolved to " + omnimarket.__file__
    )

from omnimarket.nodes.node_bus_audit_compute.handlers.handler_bus_audit_compute import (
    HandlerBusAuditCompute,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_request import (
    ModelBusAuditComputeRequest,
)

result = HandlerBusAuditCompute().handle(ModelBusAuditComputeRequest(dry_run=True))
print(result.model_dump_json())
"""


@pytest.mark.integration
def test_bus_audit_defaults_resolve_in_installed_layout(tmp_path: Path) -> None:
    """Default registry/contract resolution must work from an installed wheel.

    Recreates the installed layout: the package is copied to
    ``<tmp>/site-packages/omnimarket`` (no ``src/`` parent, no repo root) and
    the handler runs with an all-defaults request from an unrelated cwd. The
    audit must check a non-zero number of topics and contracts. This test
    FAILS if default resolution regresses to repo-root reconstruction.
    """
    site_dir = tmp_path / "site-packages"
    shutil.copytree(
        _PACKAGE_SRC,
        site_dir / "omnimarket",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )

    completed = subprocess.run(
        [sys.executable, "-c", _INSTALLED_LAYOUT_SCRIPT, str(site_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert completed.returncode == 0, (
        f"installed-layout audit failed:\nstdout={completed.stdout}\n"
        f"stderr={completed.stderr}"
    )

    result = ModelBusAuditComputeResult.model_validate(json.loads(completed.stdout))
    assert result.topics_registered > 0, "audited zero registered topics"
    assert result.topics_declared > 0, "audited zero contract-declared topics"
    assert result.contracts_checked > 0, "scanned zero contracts"
    assert all(
        finding.finding_type != EnumBusAuditFindingType.REGISTRY_NOT_FOUND
        for finding in result.findings
    ), f"packaged registry not found: {result.findings}"


@pytest.mark.integration
def test_bus_audit_defaults_resolve_in_source_checkout() -> None:
    """The same defaults must audit non-zero work from the source checkout."""
    result = HandlerBusAuditCompute().handle(ModelBusAuditComputeRequest(dry_run=True))
    assert result.topics_registered > 0
    assert result.topics_declared > 0
    assert result.contracts_checked > 0


@pytest.mark.integration
def test_bus_audit_missing_default_registry_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing packaged default registry is a broken install: raise, never audit."""
    monkeypatch.setattr(
        handler_module,
        "_DEFAULT_REGISTRY_PATH",
        tmp_path / "does_not_exist" / "topics.yaml",
    )
    with pytest.raises(FileNotFoundError, match="default topic registry is missing"):
        HandlerBusAuditCompute().handle(ModelBusAuditComputeRequest(dry_run=True))


@pytest.mark.integration
def test_bus_audit_zero_contracts_raises(tmp_path: Path) -> None:
    """Zero contract.yaml files under the roots is vacuous work: raise."""
    registry = tmp_path / "topics.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "events": {
                    "sample.completed": {
                        "fan_out": [
                            {"topic": "onex.evt.omnimarket.sample-completed.v1"}
                        ],
                        "partition_key_field": "run_id",
                        "required_fields": ["run_id"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    empty_root = tmp_path / "empty_nodes"
    empty_root.mkdir()
    with pytest.raises(ValueError, match=r"zero contract\.yaml files"):
        HandlerBusAuditCompute().handle(
            ModelBusAuditComputeRequest(
                registry_path=str(registry),
                contract_roots=[str(empty_root)],
                dry_run=True,
            )
        )


@pytest.mark.integration
def test_bus_audit_zero_topics_without_error_finding_raises(tmp_path: Path) -> None:
    """Zero topics audited with nothing flagged must raise, not pass clean."""
    registry = tmp_path / "topics.yaml"
    registry.write_text(yaml.safe_dump({"events": {}}), encoding="utf-8")
    contract = tmp_path / "nodes" / "node_sample" / "contract.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        yaml.safe_dump(
            {
                "name": "node_sample",
                "node_type": "compute",
                "event_bus": {"publish_topics": [], "subscribe_topics": []},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="zero topics audited"):
        HandlerBusAuditCompute().handle(
            ModelBusAuditComputeRequest(
                registry_path=str(registry),
                contract_roots=[str(tmp_path / "nodes")],
                dry_run=True,
            )
        )


@pytest.mark.integration
def test_bus_audit_cli_exits_nonzero_on_error_status(tmp_path: Path) -> None:
    """The CLI must exit 1 (with full JSON on stdout) when the audit ERRORs."""
    registry = tmp_path / "topics.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "events": {
                    "bad.event": {
                        "fan_out": [{"topic": "not-a-valid-topic"}],
                        "partition_key_field": "run_id",
                        "required_fields": ["run_id"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    contract = tmp_path / "nodes" / "node_sample" / "contract.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        yaml.safe_dump(
            {
                "name": "node_sample",
                "node_type": "compute",
                "event_bus": {
                    "publish_topics": ["onex.evt.omnimarket.sample-completed.v1"]
                },
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_bus_audit_compute",
            "--registry-path",
            str(registry),
            "--contract-root",
            str(tmp_path / "nodes"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    result = ModelBusAuditComputeResult.model_validate(json.loads(completed.stdout))
    assert result.status == EnumBusAuditStatus.ERROR
