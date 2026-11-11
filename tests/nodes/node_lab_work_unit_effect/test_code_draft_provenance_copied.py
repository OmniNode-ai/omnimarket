# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 rule 10 -- every delegate turn a code draft causes carries the
synthetic traffic class and the source surface the caller named.

The chain under test: the draft's fields -> the loop request file the runner
writes -> the argv of each ``onex delegate`` turn the loop's ports run. The
break "drop provenance" (any link not copying the two fields) turns it red.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from pydantic import ValidationError

from omnimarket.delegated_code_edit.loop_ports import (
    DelegatedCodeEditPorts,
    deployed_lane_delegate_flags,
)
from omnimarket.models.model_delegated_code_edit import (
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import RESPONSE_CONTRACT
from omnimarket.nodes.node_lab_work_unit_effect.models import ModelLabCodeDraft
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    LocalShellLabWorkExecutor,
)

pytestmark = pytest.mark.unit

SURFACE = "code_eval_trial_orchestrator"


def _draft(**overrides: object) -> ModelLabCodeDraft:
    fields: dict[str, object] = {
        "task": "write answer.py",
        "writable_globs": ("answer.py",),
        "checks": (ModelDeclaredCheck(name="syntax", argv=("python3", "-V")),),
        "max_turns": 3,
        "backend_id": "local-coder",
        "max_diff_bytes": 1000,
        "traffic_class": EnumDelegationTrafficClass.SYNTHETIC,
        "source_surface": SURFACE,
    }
    fields.update(overrides)
    return ModelLabCodeDraft.model_validate(fields)


def _turn_argv(tmp_path: Path, request: ModelDelegatedCodeEditRequest) -> list[str]:
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        run_id = str(uuid.uuid4())
        run_dir = tmp_path / "state" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.txt").write_text('```json\n{"actions": []}\n```')
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    ports = DelegatedCodeEditPorts(
        onex=Path("/bin/onex"),
        state_root=tmp_path / "state",
        delegate_flags=deployed_lane_delegate_flags("dev"),
        run_delegate=runner,  # type: ignore[arg-type]
    )
    ports.delegate(request, "PROMPT", RESPONSE_CONTRACT, 1)
    return seen[0]


def test_provenance_reaches_every_delegate_turn_argv(tmp_path: Path) -> None:
    runner = LocalShellLabWorkExecutor(tmp_path / "work")
    from omnimarket.nodes.node_lab_work_unit_effect.models import (
        ModelLabWorkUnitRequest,
    )

    unit = ModelLabWorkUnitRequest.model_validate(
        {
            "work_unit_id": "lw-prov-0001",
            "target_host": "h202",
            "repo": "OmniNode-ai/demo",
            "commit_sha": "0" * 40,
            "argv": ["onex", "code-edit", "run"],
            "kind": "code_draft",
            "lane": "test-lane",
            "code_draft": _draft(),
        }
    )
    argv = runner._draft_argv(unit, tmp_path / "tree", tmp_path / "state")
    request_file = Path(argv[argv.index("--request") + 1])
    loop_request = ModelDelegatedCodeEditRequest.model_validate_json(
        request_file.read_text(encoding="utf-8")
    )
    assert loop_request.traffic_class is EnumDelegationTrafficClass.SYNTHETIC
    assert loop_request.source_surface == SURFACE
    turn = _turn_argv(tmp_path, loop_request)
    assert turn[turn.index("--traffic-class") + 1] == "synthetic"
    assert turn[turn.index("--source-surface") + 1] == SURFACE


def test_a_request_without_provenance_adds_no_flags(tmp_path: Path) -> None:
    request = ModelDelegatedCodeEditRequest(
        correlation_id=str(uuid.uuid4()),
        task="t",
        workspace_root=str(tmp_path),
        writable_globs=("a.py",),
        checks=(ModelDeclaredCheck(name="c", argv=("true",)),),
    )
    turn = _turn_argv(tmp_path, request)
    assert "--traffic-class" not in turn
    assert "--source-surface" not in turn


def test_synthetic_traffic_must_name_its_surface() -> None:
    with pytest.raises(ValidationError, match="source_surface"):
        ModelDelegatedCodeEditRequest(
            correlation_id=str(uuid.uuid4()),
            task="t",
            workspace_root="/tmp",
            writable_globs=("a.py",),
            checks=(ModelDeclaredCheck(name="c", argv=("true",)),),
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
        )


def test_a_draft_without_provenance_writes_no_null_fields_for_an_older_loop(
    tmp_path: Path,
) -> None:
    """A loop already installed on a pool host rejects unknown keys (extra forbid),
    so a draft that names no provenance must not put the two keys in the file."""
    from omnimarket.nodes.node_lab_work_unit_effect.models import (
        ModelLabWorkUnitRequest,
    )

    unit = ModelLabWorkUnitRequest.model_validate(
        {
            "work_unit_id": "lw-prov-0002",
            "target_host": "h202",
            "repo": "OmniNode-ai/demo",
            "commit_sha": "0" * 40,
            "argv": ["onex", "code-edit", "run"],
            "kind": "code_draft",
            "lane": "test-lane",
            "code_draft": _draft(traffic_class=None, source_surface=None),
        }
    )
    argv = LocalShellLabWorkExecutor(tmp_path / "work")._draft_argv(
        unit, tmp_path / "tree", tmp_path / "state"
    )
    written = json.loads(Path(argv[argv.index("--request") + 1]).read_text())
    assert "traffic_class" not in written
    assert "source_surface" not in written
    assert "ticket" not in written
