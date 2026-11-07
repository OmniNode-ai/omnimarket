# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seven modules of the literal_path slice read their locations from the runtime-path authority."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.handlers.runtime_paths import runtime_path, runtime_path_list
from omnimarket.nodes.node_contractor_integration_note_effect.services.note_composer import (
    contains_internal_reference,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify import (
    ModelRuntimeShaVerifyRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    local_host_reader,
    local_shell_work_runner,
)
from omnimarket.nodes.node_push_validation_effect.protocols import (
    dtl_container_invocation,
)

SLICE = (
    "src/omnimarket/nodes/node_codex_hook_registration_effect/handlers/handler_codex_hook_registration.py",
    "src/omnimarket/nodes/node_contractor_integration_note_effect/services/note_composer.py",
    "src/omnimarket/nodes/node_dod_verify/handlers/handler_runtime_sha_verify.py",
    "src/omnimarket/nodes/node_filesystem_crawler_effect/handlers/handler_filesystem_crawler.py",
    "src/omnimarket/nodes/node_lab_work_unit_effect/protocols/local_host_reader.py",
    "src/omnimarket/nodes/node_lab_work_unit_effect/protocols/local_shell_work_runner.py",
    "src/omnimarket/nodes/node_push_validation_effect/protocols/dtl_container_invocation.py",
)


def test_slice_keeps_the_previous_values() -> None:
    assert runtime_path("claude.home_dir") == "~/.claude"
    assert runtime_path("runtime_host.deployed_repo_path") == "/opt/omninode/runtime"
    assert runtime_path("push_validation.container_venv_python") == (
        "/opt/dtl-venv/bin/python"
    )
    assert runtime_path_list("lab_work_unit.extra_path_dirs") == (
        "~/.local/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    )
    assert runtime_path_list("contractor_note.internal_path_prefixes") == (
        "/Users/",
        "/Volumes/",
    )


def test_consumers_take_the_authority_values() -> None:
    assert (
        runtime_path_list("lab_work_unit.extra_path_dirs")
        == local_host_reader._EXTRA_PATH
    )
    assert local_shell_work_runner._EXTRA_PATH == local_host_reader._EXTRA_PATH
    assert dtl_container_invocation.VENV_PYTHON == "/opt/dtl-venv/bin/python"
    request = ModelRuntimeShaVerifyRequest(
        ticket_id="T-1",
        evidence_item_id="e-1",
        merge_sha="abcdef0",
        runtime_host="host",
    )
    assert request.runtime_repo_path == "/opt/omninode/runtime"


@pytest.mark.parametrize(
    "prefix", runtime_path_list("contractor_note.internal_path_prefixes")
)
def test_internal_path_prefixes_still_refuse(prefix: str) -> None:
    assert contains_internal_reference(f"see {prefix}name/file")
    assert not contains_internal_reference("a plain sentence")


def test_slice_has_no_literal_path_findings() -> None:
    root = Path(__file__).resolve().parents[3]
    paths = [root / name for name in SLICE]
    assert all(path.is_file() for path in paths), "a target module is missing"
    validator = ValidatorContractsAndDi()
    assert any(
        rule.rule_id == "literal_path" and rule.enabled
        for rule in validator.contract.rules
    ), "literal_path rule is not enabled"
    result = validator.validate(paths)
    assert result.metadata is not None
    assert result.metadata.files_processed == len(paths)
    findings = [
        issue
        for issue in result.issues
        if issue.rule_name in {"literal_path", "unparseable_file"}
    ]
    assert not findings, findings
