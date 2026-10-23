# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket admission through the installed guard, plus parity with its MCP hook."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_lane_intake import read_lane_intake_policy
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    TicketGuardRefusedError,
)
from omnimarket.nodes.node_create_ticket.handlers.ticket_guard_parity import (
    assert_guard_parity,
    guard_parity,
    hook_guard_path,
)

pytestmark = pytest.mark.unit

_BODY = (
    "Gate: OmniNode-ai/omnimarket ci.yml\n\n"
    "## Acceptance criteria\n\n"
    "- [ ] AC1: the handler refuses an unbound ticket -- falsifier: "
    "uv run pytest tests/nodes/node_create_ticket -k create_ticket_gate_refuses\n"
)
_FILES = (
    "ticket_creation_guard.py",
    "ticket_creation_policy.json",
    "gate_binding_grammar.json",
)


class _RecordingLinearClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
        label_names: Sequence[str] = (),
    ) -> tuple[str, str]:
        self.calls.append(
            {
                "title": title,
                "description": description,
                "team": team,
                "parent": parent,
                "assignee_id": assignee_id,
                "label_names": list(label_names),
            }
        )
        return "OMN-91001", "https://linear.app/omninode/issue/OMN-91001"


def _plugin_tree(root: Path) -> Path:
    """Write a plugin whose registered hook loads its guard beside two configs."""
    hooks = root / "hooks"
    for directory in ("scripts", "lib", "config"):
        (hooks / directory).mkdir(parents=True)
    (hooks / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "^mcp__linear-server__(save_issue|update_issue)$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/pre_tool_use_ticket_creation_gate.sh",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (hooks / "scripts/pre_tool_use_ticket_creation_gate.sh").write_text(
        '#!/bin/bash\nGUARD_PY="${SCRIPT_DIR}/../lib/ticket_creation_guard.py"\n',
        encoding="utf-8",
    )
    guard = hooks / "lib/ticket_creation_guard.py"
    guard.write_text("# guard\n", encoding="utf-8")
    (hooks / "config/ticket_creation_policy.json").write_text(
        json.dumps(
            {
                "lane_intake": {
                    "label": "lane-created",
                    "stamp_prefix": "created-by-lane:",
                }
            }
        ),
        encoding="utf-8",
    )
    (hooks / "config/gate_binding_grammar.json").write_text("{}\n", encoding="utf-8")
    return guard


def _write_guard(tmp_path: Path, exit_code: int, stdout: str) -> Path:
    guard = _plugin_tree(tmp_path)
    guard.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.load(sys.stdin)\n"
        "assert payload['tool_name'] == 'mcp__linear-server__save_issue'\n"
        "Path(__file__).with_suffix('.input.json').write_text(\n"
        "    json.dumps({'payload': payload, 'argv': sys.argv[1:]}))\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return guard


def _handler(
    guard: Path, client: _RecordingLinearClient, monkeypatch: pytest.MonkeyPatch
) -> HandlerCreateTicket:
    monkeypatch.setenv("ONEX_TICKET_GUARD_PATH", str(guard))
    monkeypatch.setenv("ONEX_LANE", "gate-test-lane")
    return HandlerCreateTicket(
        linear_client=client, lane_intake_policy=read_lane_intake_policy(guard)
    )


def test_create_ticket_gate_refuses_stub_preserves_finding_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = (
        "BLOCKED: this Linear issue CREATE is not bound to a commitment\n"
        "  * [missing_gate_line] description: no Gate line\n"
        "  * [project_set] project: new tickets must have no project\n"
    )
    guard = _write_guard(
        tmp_path, 3, json.dumps({"decision": "block", "reason": reason})
    )
    client = _RecordingLinearClient()
    handler = _handler(guard, client, monkeypatch)
    with pytest.raises(TicketGuardRefusedError) as exc:
        handler.handle(
            ModelCreateTicketRequest(
                title="Ticket gate coverage", description="No binding", dry_run=False
            )
        )
    assert exc.value.finding_codes == ["missing_gate_line", "project_set"]
    assert reason in str(exc.value)
    assert client.calls == []


def test_create_ticket_gate_admits_stub_creates_once_in_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _write_guard(tmp_path, 0, "")
    client = _RecordingLinearClient()
    result = _handler(guard, client, monkeypatch).handle(
        ModelCreateTicketRequest(
            title="Ticket gate coverage",
            description=_BODY,
            parent="OMN-21329",
            dry_run=False,
        )
    )
    assert result.status == "created"
    assert result.ticket_id == "OMN-91001"
    assert result.guard_path == str(guard)
    description = "created-by-lane: gate-test-lane\n\n" + _BODY
    assert client.calls == [
        {
            "title": "Ticket gate coverage",
            "description": description,
            "team": "Omninode",
            "parent": "OMN-21329",
            "assignee_id": None,
            "label_names": ["lane-created"],
        }
    ]
    assert "project" not in client.calls[0]
    assert result.description_body == description
    recorded = json.loads(guard.with_suffix(".input.json").read_text(encoding="utf-8"))
    assert recorded["argv"] == ["--node"]
    assert recorded["payload"]["tool_input"] == {
        "team": "Omninode",
        "title": "Ticket gate coverage",
        "description": description,
        "state": "Backlog",
        "labels": ["lane-created"],
        "parentId": "OMN-21329",
    }


def test_create_ticket_gate_parity_identical_trees(tmp_path: Path) -> None:
    node_guard = _plugin_tree(tmp_path / "node")
    hook_guard = _plugin_tree(tmp_path / "hook")
    parity = assert_guard_parity(node_guard, hook_guard.parents[2])
    assert parity.same is True
    assert parity.differing == []
    assert parity.guard_path == str(node_guard.resolve())
    assert parity.hook_guard_path == str(hook_guard.resolve())
    assert parity.files == {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in (
            (_FILES[0], node_guard),
            (_FILES[1], node_guard.parent.parent / "config" / _FILES[1]),
            (_FILES[2], node_guard.parent.parent / "config" / _FILES[2]),
        )
    }
    with pytest.raises(ValidationError, match="frozen"):
        parity.guard_path = "changed"


@pytest.mark.parametrize("name", _FILES)
def test_create_ticket_gate_parity_detects_one_byte_drift(
    tmp_path: Path, name: str
) -> None:
    node_guard = _plugin_tree(tmp_path / "node")
    hook_guard = _plugin_tree(tmp_path / "hook")
    changed = (
        hook_guard if name == _FILES[0] else hook_guard.parent.parent / "config" / name
    )
    changed.write_bytes(changed.read_bytes() + b" ")
    parity = guard_parity(node_guard, hook_guard.parents[2])
    assert parity.same is False
    assert parity.differing == [name]
    with pytest.raises(RuntimeError, match=name):
        assert_guard_parity(node_guard, hook_guard.parents[2])


@pytest.mark.parametrize("name", _FILES)
@pytest.mark.parametrize("side", ["node", "hook", "both"])
def test_create_ticket_gate_parity_detects_missing_files(
    tmp_path: Path, name: str, side: str
) -> None:
    node_guard = _plugin_tree(tmp_path / "node")
    hook_guard = _plugin_tree(tmp_path / "hook")
    for tree, guard in (("node", node_guard), ("hook", hook_guard)):
        if side in (tree, "both"):
            missing = (
                guard if name == _FILES[0] else guard.parent.parent / "config" / name
            )
            missing.unlink()
    parity = guard_parity(node_guard, hook_guard.parents[2])
    assert parity.same is False
    assert parity.differing == [name]
    assert (name in parity.files) is (side == "hook")
    with pytest.raises(RuntimeError, match=name):
        assert_guard_parity(node_guard, hook_guard.parents[2])


def test_create_ticket_gate_parity_no_vendored_guard() -> None:
    source = Path(__file__).resolve().parents[3] / "src/omnimarket"
    assert source.is_dir()
    for name in _FILES:
        assert list(source.rglob(name)) == [], f"vendored guard file: {name}"


@pytest.mark.parametrize(
    ("relative_path", "contents", "missing"),
    [
        ("hooks/hooks.json", None, "hooks.json"),
        ("hooks/hooks.json", "{", "hook registration"),
        ("hooks/hooks.json", "{}", "no PreToolUse hook registered"),
        (
            "hooks/scripts/pre_tool_use_ticket_creation_gate.sh",
            None,
            "pre_tool_use_ticket_creation_gate.sh",
        ),
        (
            "hooks/scripts/pre_tool_use_ticket_creation_gate.sh",
            "#!/bin/bash\n",
            "GUARD_PY",
        ),
    ],
)
def test_create_ticket_gate_parity_reports_missing_hook_configuration(
    tmp_path: Path, relative_path: str, contents: str | None, missing: str
) -> None:
    _plugin_tree(tmp_path)
    path = tmp_path / relative_path
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeError, match=missing):
        hook_guard_path(tmp_path)


def test_create_ticket_gate_parity_follows_matching_registration(
    tmp_path: Path,
) -> None:
    _plugin_tree(tmp_path)
    hooks_path = tmp_path / "hooks/hooks.json"
    document = json.loads(hooks_path.read_text(encoding="utf-8"))
    entries = document["hooks"]["PreToolUse"]
    # re.search must accept an unanchored matcher; unrelated registrations must not win.
    entries[0]["matcher"] = "linear-server__save_issue"
    entries.insert(
        0,
        {
            "matcher": "^unrelated_tool$",
            "hooks": entries[0]["hooks"],
        },
    )
    hooks_path.write_text(json.dumps(document), encoding="utf-8")
    script = tmp_path / "hooks/scripts/pre_tool_use_ticket_creation_gate.sh"
    script.write_text(
        'GUARD_PY="${SCRIPT_DIR}/../lib/alternate_guard.py"\n', encoding="utf-8"
    )
    assert (
        hook_guard_path(tmp_path)
        == (tmp_path / "hooks/lib/alternate_guard.py").resolve()
    )


def test_create_ticket_gate_refuses_cli_prints_finding_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The headless entry point prints the guard's codes in the result and exits 1."""
    from omnimarket.nodes.node_create_ticket.__main__ import main

    reason = (
        "BLOCKED:\n"
        "  * [missing_gate_line] description: no Gate line\n"
        "  * [unfalsified_criterion] description: AC1 names no falsifier\n"
    )
    guard = _write_guard(
        tmp_path, 3, json.dumps({"decision": "block", "reason": reason})
    )
    monkeypatch.setenv("ONEX_TICKET_GUARD_PATH", str(guard))
    monkeypatch.setenv("ONEX_LANE", "gate-test-lane")
    monkeypatch.setattr(
        "sys.argv",
        ["node_create_ticket", "--title", "Ticket gate coverage", "--description", "x"],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert result["ticket_id"] == ""
    assert result["finding_codes"] == ["missing_gate_line", "unfalsified_criterion"]
