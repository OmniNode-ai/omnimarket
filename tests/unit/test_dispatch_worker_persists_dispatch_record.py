"""Phase 1 Task 4 (OMN-10208): node_dispatch_worker writes a dispatch record.

The handler persists `ModelDispatchRecord` to
`$ONEX_STATE_DIR/dispatches/<agent_id>.yaml` immediately before returning the
typed result. The persistence step is the audit trail downstream verification
depends on; the handler must FAIL LOUD if the writer import chain breaks.

Phase 1 imports `write_dispatch_record` and `ModelDispatchRecord` from
omniclaude (master plan known boundary violation note); Phase 2 will relocate
them to omnibase_core. To keep these tests runnable in omnimarket CI without
adding omniclaude as a real dependency (which would create a logical
package-graph cycle), the tests inject a lightweight stub `omniclaude.hooks.*`
module tree into `sys.modules` when omniclaude is not actually installed.
"""

from __future__ import annotations

import builtins
import os
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)


def _install_omniclaude_stub() -> dict[str, object]:
    """Install a minimal stub omniclaude.hooks tree into sys.modules.

    Returns a handle dict with the patched modules so tests can swap the
    writer or model behavior. The stub writer reads ``ONEX_STATE_DIR`` at
    call time (matching the real omniclaude writer's behavior).
    """
    handle: dict[str, object] = {}

    written: list[dict[str, object]] = []

    def _stub_write_dispatch_record(record: object) -> Path:
        payload = record.model_dump(mode="json")  # type: ignore[attr-defined]
        agent_id = payload["agent_id"]
        state_dir = Path(os.environ["ONEX_STATE_DIR"])
        out_path = state_dir / "dispatches" / f"{agent_id}.yaml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            yaml.safe_dump(payload, sort_keys=True),
            encoding="utf-8",
        )
        written.append(payload)
        return out_path

    handle["written"] = written

    # Build a minimal ModelDispatchRecord BaseModel mirroring the real model's
    # field shape and providing model_dump(mode="json").
    from datetime import datetime

    from pydantic import BaseModel, ConfigDict, Field

    class _StubModelDispatchRecord(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        agent_id: str
        dispatched_at: datetime
        dispatcher: str
        ticket: str
        allowed_tools: list[str] = Field(default_factory=list)
        prompt_digest: str
        parent_session_id: str

    handle["model"] = _StubModelDispatchRecord
    handle["writer"] = _stub_write_dispatch_record

    omniclaude_pkg = types.ModuleType("omniclaude")
    omniclaude_pkg.__path__ = []
    hooks_pkg = types.ModuleType("omniclaude.hooks")
    hooks_pkg.__path__ = []
    lib_pkg = types.ModuleType("omniclaude.hooks.lib")
    lib_pkg.__path__ = []

    record_mod = types.ModuleType("omniclaude.hooks.model_dispatch_record")
    record_mod.ModelDispatchRecord = _StubModelDispatchRecord  # type: ignore[attr-defined]

    writer_mod = types.ModuleType("omniclaude.hooks.lib.dispatch_record_writer")
    writer_mod.write_dispatch_record = (  # type: ignore[attr-defined]
        _stub_write_dispatch_record
    )

    sys.modules["omniclaude"] = omniclaude_pkg
    sys.modules["omniclaude.hooks"] = hooks_pkg
    sys.modules["omniclaude.hooks.lib"] = lib_pkg
    sys.modules["omniclaude.hooks.model_dispatch_record"] = record_mod
    sys.modules["omniclaude.hooks.lib.dispatch_record_writer"] = writer_mod
    handle["writer_mod"] = writer_mod
    return handle


@pytest.fixture
def omniclaude_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, object]]:
    """Inject a stub omniclaude.hooks tree for the duration of a test."""
    for mod in (
        "omniclaude",
        "omniclaude.hooks",
        "omniclaude.hooks.lib",
        "omniclaude.hooks.model_dispatch_record",
        "omniclaude.hooks.lib.dispatch_record_writer",
    ):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    handle = _install_omniclaude_stub()
    yield handle
    for mod in (
        "omniclaude.hooks.lib.dispatch_record_writer",
        "omniclaude.hooks.model_dispatch_record",
        "omniclaude.hooks.lib",
        "omniclaude.hooks",
        "omniclaude",
    ):
        sys.modules.pop(mod, None)


@pytest.mark.unit
def test_handler_persists_dispatch_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    omniclaude_stub: dict[str, object],
) -> None:
    """Handler writes a dispatch record YAML before returning the result."""
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ONEX_PARENT_SESSION_ID", "test-session-abc")

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
def test_handler_fails_loud_on_broken_writer_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If the writer import chain breaks, handler raises — never silently skips."""
    # Persistence is gated on ONEX_STATE_DIR; set it so the import path is reached.
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))

    # Force the lazy import inside _persist_dispatch_record to fail by removing
    # any cached omniclaude.* modules and blocking re-import.
    for mod_name in list(sys.modules):
        if mod_name == "omniclaude" or mod_name.startswith("omniclaude."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "omniclaude" or name.startswith("omniclaude."):
            raise ModuleNotFoundError(f"simulated broken chain for {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    handler = HandlerDispatchWorker()
    cmd = ModelDispatchWorkerCommand(
        name="test-worker-fail-loud",
        team="test-team",
        role=EnumWorkerRole.fixer,
        scope="x",
        targets=["OMN-9999", "omnimarket#1"],
    )
    with pytest.raises((ImportError, RuntimeError, ModuleNotFoundError)):
        handler.handle(cmd, existing_task_subjects=[])
