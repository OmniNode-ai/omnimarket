# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""replay_worktree rebuilds each turn's worktree from the base tree and the receipt."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
    ModelDelegatedCodeEditRequest,
    ModelTurnReply,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.replay import (
    ReplayMismatchError,
    ReplayRefusedError,
    replay_worktree,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit

BASE_FILES = {
    "src/m.py": "def add(a, b):\n    return 0\n",
    "src/n.py": "def f():\n        a = 1\n        b = 2\n        return a + b\n",
    "src/o.py": "VALUE = 1\n",
    "README.md": "readme\n",
}


class SnapshotPorts(FakePorts):
    """FakePorts that remembers the worktree as each turn starts."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.snapshots: dict[int, dict[str, str]] = {}

    def delegate(
        self,
        request: ModelDelegatedCodeEditRequest,
        prompt: str,
        response_contract: dict[str, object],
        turn: int,
    ) -> ModelTurnReply:
        self.snapshots[turn - 1] = dict(self.files)
        return super().delegate(request, prompt, response_contract, turn)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scripted() -> tuple[SnapshotPorts, dict[str, object]]:
    ports = SnapshotPorts(
        [
            _reply(
                1,
                _a("view", path="src/m.py"),
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="return 0",
                    new_string="return a + b",
                ),
            ),
            _reply(
                2,
                _a("write", file_path="src/new.py", content="NEW = 1\n"),
                # indent-shifted: the file indents by 8, the model sent 4
                _a(
                    "edit",
                    file_path="src/n.py",
                    old_string="    a = 1\n    b = 2",
                    new_string="    a = 10\n    b = 20",
                ),
            ),
            _reply(
                3,
                # already applied in turn 1: reported unchanged, writes nothing
                _a(
                    "edit",
                    file_path="src/m.py",
                    old_string="return 0",
                    new_string="return a + b",
                ),
                _a("write", file_path="README.md", content="refused\n"),
                _a(
                    "replace_in_files",
                    glob="src/*.py",
                    old_string="VALUE",
                    new_string="LIMIT",
                ),
            ),
            _reply(4, _a("finish", summary="done")),
        ],
        files=dict(BASE_FILES),
        check_passes=[True],
    )
    request = _request(workspace_root="/work/tree", max_turns=8)
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert result.status == EnumCodeEditStatus.ACCEPTED
    receipt = ports.receipts[request.correlation_id]
    ports.snapshots[len(cast(list[object], receipt["turns"]))] = dict(ports.files)
    # a stored receipt is JSON
    return ports, cast("dict[str, object]", json.loads(json.dumps(receipt)))


def _base(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    for name, text in BASE_FILES.items():
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(text, encoding="utf-8")
    return base


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): _sha(p.read_text(encoding="utf-8"))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_scripted_loop_exercises_edit_write_shift_applied_and_refused() -> None:
    ports, receipt = _scripted()
    turns = cast(list[dict[str, Any]], receipt["turns"])
    outputs = [a["output"] for t in turns for a in t["actions"]]
    assert any(o == "edited src/n.py" for o in outputs)
    assert any(o.startswith("unchanged src/m.py") for o in outputs)
    assert any(a["refused"] for t in turns for a in t["actions"])
    assert ports.files["src/n.py"] == (
        "def f():\n        a = 10\n        b = 20\n        return a + b\n"
    )


def test_replay_equals_the_live_worktree_after_every_turn(tmp_path: Path) -> None:
    ports, receipt = _scripted()
    base = _base(tmp_path)
    before = _tree(base)
    last = len(cast(list[object], receipt["turns"]))
    assert last == 4
    for turn in range(0, last + 1):
        target = tmp_path / f"turn{turn}"
        replay_worktree(receipt, base, target, through_turn=turn)
        live = {p: _sha(t) for p, t in ports.snapshots[turn].items()}
        assert _tree(target) == live, f"turn {turn}"
    assert _tree(base) == before


def test_replay_reports_the_files_it_wrote(tmp_path: Path) -> None:
    _, receipt = _scripted()
    result = replay_worktree(receipt, _base(tmp_path), tmp_path / "t", through_turn=3)
    assert result.through_turn == 3
    assert set(result.files) == {"src/m.py", "src/n.py", "src/new.py", "src/o.py"}
    assert result.files["src/new.py"] == _sha("NEW = 1\n")


def test_a_v1_receipt_is_refused_with_its_reason(tmp_path: Path) -> None:
    _, receipt = _scripted()
    receipt["schema"] = "delegated-code-edit-loop-receipt.v1"
    with pytest.raises(ReplayRefusedError, match=r"v1.*arguments"):
        replay_worktree(receipt, _base(tmp_path), tmp_path / "t", through_turn=1)
    assert not (tmp_path / "t").exists()


def test_a_turn_without_recorded_arguments_is_refused(tmp_path: Path) -> None:
    _, receipt = _scripted()
    turns = cast(list[dict[str, Any]], receipt["turns"])
    del turns[1]["actions"][0]["arguments"]
    with pytest.raises(ReplayRefusedError, match="turn 2"):
        replay_worktree(receipt, _base(tmp_path), tmp_path / "t", through_turn=3)


@pytest.mark.parametrize("through_turn", [-1, 5])
def test_through_turn_outside_the_receipt_is_refused(
    tmp_path: Path, through_turn: int
) -> None:
    _, receipt = _scripted()
    with pytest.raises(ReplayRefusedError, match="through_turn"):
        replay_worktree(
            receipt, _base(tmp_path), tmp_path / "t", through_turn=through_turn
        )


def test_a_non_empty_target_is_refused(tmp_path: Path) -> None:
    _, receipt = _scripted()
    target = tmp_path / "t"
    target.mkdir()
    (target / "x").write_text("x")
    with pytest.raises(ReplayRefusedError, match="target_root"):
        replay_worktree(receipt, _base(tmp_path), target, through_turn=1)


def test_a_digest_mismatch_raises_and_writes_nothing_beyond_that_turn(
    tmp_path: Path,
) -> None:
    _, receipt = _scripted()
    forged = copy.deepcopy(receipt)
    turns = cast(list[dict[str, Any]], forged["turns"])
    turns[1]["actions"][0]["written_sha256"] = {"src/new.py": "0" * 64}
    base = _base(tmp_path)
    target = tmp_path / "t"
    with pytest.raises(ReplayMismatchError) as caught:
        replay_worktree(forged, base, target, through_turn=4)
    assert caught.value.turn == 2
    assert caught.value.path == "src/new.py"
    assert caught.value.expected == "0" * 64
    assert caught.value.actual == _sha("NEW = 1\n")
    # turn 2's second action (the indent-shift edit) and turns 3-4 never ran
    assert (target / "src/n.py").read_text() == BASE_FILES["src/n.py"]
    assert (target / "src/m.py").read_text().endswith("return a + b\n")
    assert _tree(base) == {p: _sha(t) for p, t in BASE_FILES.items()}


def test_a_changed_base_tree_is_a_mismatch(tmp_path: Path) -> None:
    _, receipt = _scripted()
    base = _base(tmp_path)
    (base / "src/m.py").write_text("def add(a, b):\n    return 1\n")
    with pytest.raises(ReplayMismatchError) as caught:
        replay_worktree(receipt, base, tmp_path / "t", through_turn=1)
    assert caught.value.turn == 1


def test_a_format_action_needs_a_runner(tmp_path: Path) -> None:
    ports = SnapshotPorts(
        [
            _reply(
                1,
                _a("write", file_path="src/m.py", content="x=1\n"),
                _a("format", path="src/m.py"),
            ),
            _reply(2, _a("finish", summary="done")),
        ],
        files=dict(BASE_FILES),
        check_passes=[True],
    )
    request = _request(formatter=(("fmt",),), max_turns=4)
    HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    receipt = json.loads(json.dumps(ports.receipts[request.correlation_id]))
    with pytest.raises(ReplayRefusedError, match="format"):
        replay_worktree(receipt, _base(tmp_path), tmp_path / "t", through_turn=1)
