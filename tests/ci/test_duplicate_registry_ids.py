# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20182: registry coverage and enforcement through the shared validator."""

from __future__ import annotations

import re
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.validation.model_duplicate_id_check_spec import (
    ModelDuplicateIdCheckSpec,
)
from omnibase_core.models.validation.model_duplicate_id_manifest import (
    ModelDuplicateIdManifest,
)
from omnibase_core.validation.validator_duplicate_config_ids import (
    ValidatorDuplicateConfigIds,
    main,
)

from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / ".duplicate-id-registries.yaml"
_SPECS = ModelDuplicateIdManifest.from_yaml(_MANIFEST).registries


@pytest.mark.unit
def test_manifest_covers_delegation_and_model_lists_and_passes() -> None:
    specs = {spec.path: spec for spec in _SPECS}
    for path, list_path, id_field in (
        ("src/omnimarket/configs/bifrost_delegation.yaml", "backends", "backend_id"),
        (
            "src/omnimarket/nodes/node_ab_compare_orchestrator/models_registry.yaml",
            "models",
            "id",
        ),
    ):
        assert specs[path].list_path == list_path
        assert specs[path].id_field == id_field
        assert specs[path].disambiguator_field is None
    for spec in _SPECS:
        assert (_ROOT / spec.path).is_file()
    assert ValidatorDuplicateConfigIds().check_manifest(_MANIFEST, _ROOT) == []
    assert main(["--manifest", str(_MANIFEST), "--repo-root", str(_ROOT)]) == 0


@pytest.mark.unit
@pytest.mark.parametrize("spec", _SPECS, ids=lambda spec: spec.path)
def test_planted_duplicate_fails_manifest_validator(
    tmp_path: Path, spec: ModelDuplicateIdCheckSpec
) -> None:
    manifest = tmp_path / _MANIFEST.name
    shutil.copyfile(_MANIFEST, manifest)
    for registry in _SPECS:
        target = tmp_path / registry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_ROOT / registry.path, target)

    validator = ValidatorDuplicateConfigIds()
    assert validator.check_manifest(manifest, tmp_path) == []
    target = tmp_path / spec.path
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    if spec.list_path == "tiers[].models":
        entries = data["tiers"][0]["models"]
    else:
        entries = data[spec.list_path]
    assert isinstance(entries, list)
    assert entries
    entries.append(deepcopy(entries[0]))
    target.write_text(yaml.safe_dump(data), encoding="utf-8")

    violations = validator.check_manifest(manifest, tmp_path)
    assert violations
    assert all(violation.registry_path == spec.path for violation in violations)
    assert all("duplicate" in violation.detail for violation in violations)
    assert main(["--manifest", str(manifest), "--repo-root", str(tmp_path)]) == 1


@pytest.mark.unit
def test_hook_runs_for_every_manifest_registry() -> None:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "check-duplicate-registry-ids"
    )
    for path in (_MANIFEST.name, *(spec.path for spec in _SPECS)):
        assert re.search(hook["files"], path), path


@pytest.mark.unit
def test_ci_runs_manifest_validator_in_required_lint_job() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
    lint = workflow["jobs"]["lint"]
    assert lint.get("name", "lint") in STRICT_GATE_JOBS
    step = next(
        step
        for step in lint["steps"]
        if step.get("name") == "Duplicate registry IDs gate (enforce)"
    )
    assert "if" not in step
    assert not step.get("continue-on-error", False)
    run = step["run"]
    assert "test -f .duplicate-id-registries.yaml" in run
    assert (
        "uv run python -m omnibase_core.validation.validator_duplicate_config_ids"
        in run
    )
    assert "--manifest .duplicate-id-registries.yaml --repo-root ." in run
    assert "|| true" not in run
