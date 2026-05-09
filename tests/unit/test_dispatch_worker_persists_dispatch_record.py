"""node_dispatch_worker writes omnimarket-local dispatch records.

The handler persists `ModelDispatchRecord` to
`$ONEX_STATE_DIR/dispatches/<agent_id>.yaml` immediately before returning the
typed result. The persistence step is the audit trail downstream verification
depends on; it must not import omniclaude or silently skip when persistence is
requested.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dispatch_worker.handlers.dispatch_record_writer import (
    write_dispatch_record,
)
from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_record import (
    ModelDispatchRecord,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)


@pytest.mark.unit
def test_handler_persists_dispatch_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Handler writes a dispatch record YAML without importing omniclaude."""
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ONEX_PARENT_SESSION_ID", "test-session-abc")
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    for mod_name in list(sys.modules):
        if mod_name == "omniclaude" or mod_name.startswith("omniclaude."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "omniclaude" or name.startswith("omniclaude."):
            raise AssertionError(f"omniclaude import is forbidden: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    handler = HandlerDispatchWorker()
    cmd = ModelDispatchWorkerCommand(
        name="test-worker-omn-10208",
        team="test-team",
        role=EnumWorkerRole.fixer,
        scope="dummy scope for test",
        targets=["OMN-9999", "omnibase_core#100"],
    )
    result = handler.handle(cmd, existing_task_subjects=[])

    assert result.rejected_reason == ""
    record_path = tmp_path / "dispatches" / f"{cmd.name}.yaml"
    assert record_path.is_file(), f"dispatch record not written at {record_path}"

    record = yaml.safe_load(record_path.read_text())
    assert record["agent_id"] == cmd.name
    assert record["dispatcher"] == "node_dispatch_worker"
    assert record["ticket"] == "OMN-9999"
    assert record["parent_session_id"] == "test-session-abc"
    assert len(record.get("prompt_digest", "")) >= 8


@pytest.mark.unit
def test_handler_requires_omni_home_for_prompt_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt compilation fails before emitting empty or root worktree paths."""
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv("OMNI_WORKTREES", raising=False)

    handler = HandlerDispatchWorker()
    cmd = ModelDispatchWorkerCommand(
        name="test-worker-no-omni-home",
        team="test-team",
        role=EnumWorkerRole.fixer,
        scope="dummy scope for test",
        targets=["OMN-9999", "omnibase_core#100"],
    )

    with pytest.raises(ValueError, match="OMNI_HOME must be set"):
        handler.handle(cmd, existing_task_subjects=[])


@pytest.mark.unit
def test_writer_requires_explicit_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct writer use fails loudly instead of choosing a fallback directory."""
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    record = ModelDispatchRecord(
        agent_id="test-worker-fail-loud",
        dispatched_at="2026-04-29T00:00:00Z",
        dispatcher="node_dispatch_worker",
        ticket="OMN-10273",
        prompt_digest="abc123",
        parent_session_id="parent",
    )
    with pytest.raises(RuntimeError, match="ONEX_STATE_DIR is not set"):
        write_dispatch_record(record)


@pytest.mark.unit
@pytest.mark.parametrize("bad_id", ["..", "a/b", "agent.1", "a" * 65, "has space"])
def test_dispatch_record_rejects_non_slug_agent_id(bad_id: str) -> None:
    """Record filenames are constrained by the agent_id model contract."""
    with pytest.raises(ValidationError):
        ModelDispatchRecord(
            agent_id=bad_id,
            dispatched_at="2026-04-29T00:00:00Z",
            dispatcher="node_dispatch_worker",
            ticket="OMN-10273",
            prompt_digest="abc123",
            parent_session_id="parent",
        )
