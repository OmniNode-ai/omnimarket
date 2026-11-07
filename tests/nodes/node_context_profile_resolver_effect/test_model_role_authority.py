# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model roles cannot become required authority (OMN-21145 AC2)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure

from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
    ModelContextProfileResolveResult,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.pack.model_context_profile import ModelContextProfile
from omnimarket.routing.task_class_contract_path import (
    TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE = "node_context_profile_resolver_effect"


async def _resolve(binding: str, tmp_path: Path) -> ModelContextProfileResolveResult:
    request = ModelContextProfileResolveRequest(profile_name="authority")
    if binding == "typed":
        return HandlerContextProfileResolver().handle(request)
    input_path = tmp_path / "request.json"
    input_path.write_text(request.model_dump_json(), encoding="utf-8")
    if binding == "inmemory":
        runtime = RuntimeLocal(
            workflow_path=Path(__file__).resolve().parents[3]
            / "src"
            / "omnimarket"
            / "nodes"
            / NODE
            / "contract.yaml",
            input_path=input_path,
            backend_overrides={"event_bus": "inmemory"},
            state_root=tmp_path / "runtime",
            timeout=10,
        )
        await runtime.run_async()
        assert runtime.handler_result is not None
        return ModelContextProfileResolveResult.model_validate(runtime.handler_result)
    state = tmp_path / "cli"
    completed = subprocess.run(
        [
            str(Path(sys.executable).parent / "onex"),
            "node",
            NODE,
            "--input",
            str(input_path),
            "--state-root",
            str(state),
            "--backend",
            "event_bus=inmemory",
            "--timeout",
            "10",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    saved_path = state / "workflow_result.json"
    assert saved_path.is_file(), completed.stdout + completed.stderr
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    assert saved["handler_locus"] == "in_process"
    return ModelContextProfileResolveResult.model_validate(saved["handler_result"])


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["typed", "inmemory", "cli"])
@pytest.mark.parametrize(
    ("field", "factor"),
    [
        ("extraction_task_class", "transcript_extracts"),
        ("summary_task_class", "tail_summary"),
    ],
)
@pytest.mark.parametrize("required", [True, False])
async def test_role_factor_is_optional_on_every_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    promoted_context_profile_routing: None,
    binding: str,
    field: str,
    factor: str,
    required: bool,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setenv("CONTEXT_PROFILES_DIR", str(profiles))
    data = {
        "model_id": "fixture-model",
        field: "extraction_proposer"
        if field == "extraction_task_class"
        else "context_compaction",
        "required_factors" if required else "optional_factors": [factor],
    }
    (profiles / "authority.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    result = await _resolve(binding, tmp_path)
    if required:
        assert result.status is EnumContextProfileResolveStatus.FAILED
        assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
        assert result.profile is None
        assert result.profile_content_hash is None
        assert any(field in error and factor in error for error in result.errors)
    else:
        assert result.status is EnumContextProfileResolveStatus.OK, result.errors
        assert result.profile is not None
        assert result.profile_content_hash == result.profile.content_hash()


def test_explicit_directory_takes_precedence_over_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTEXT_PROFILES_DIR", str(tmp_path / "absent"))
    (tmp_path / "authority.yaml").write_text(
        "model_id: fixture-model\n", encoding="utf-8"
    )
    result = HandlerContextProfileResolver(tmp_path).handle(
        ModelContextProfileResolveRequest(profile_name="authority")
    )
    assert result.status is EnumContextProfileResolveStatus.OK


def test_missing_overlay_directory_never_falls_back_to_shipped_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTEXT_PROFILES_DIR", str(tmp_path / "absent"))
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name="lane_brief")
    )
    assert result.failure_class is EnumContextPackFailure.PROFILE_NOT_FOUND
    assert result.profile is None


@pytest.mark.parametrize("factor", ["transcript_extracts", "tail_summary"])
def test_deterministic_source_without_model_role_can_still_be_required(
    factor: str,
) -> None:
    profile = ModelContextProfile.model_validate(
        {"model_id": "fixture-model", "required_factors": [factor]}
    )
    assert profile.required_factors[0].value == factor


def test_contract_and_overlay_declare_the_same_profile_and_routing_inputs() -> None:
    node = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes" / NODE
    contract = yaml.safe_load((node / "contract.yaml").read_text(encoding="utf-8"))
    overlay = yaml.safe_load((node / "overlay.yaml").read_text(encoding="utf-8"))
    assert overlay["configuration"] == contract["configuration"]
    config = contract["configuration"]
    assert (
        config["profiles_directory"]["environment_variable"] == "CONTEXT_PROFILES_DIR"
    )
    assert (
        config["task_class_contract"]["environment_variable"]
        == "TASK_CLASS_CONTRACT_PATH"
    )
    package = node.parent.parent.parent
    assert (package / config["profiles_directory"]["packaged_default"]).is_dir()
    assert (
        package / config["task_class_contract"]["packaged_default"]
    ) == TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH
