# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerGeneratedExecutor — dynamic handler loading from sandbox."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_generation_consumer.handlers.handler_generated_executor import (
    HandlerGeneratedExecutor,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeDeploy,
)

_VALID_HANDLER = """\
def handle(input_data):
    return {"echo": input_data.get("value", "none")}
"""

_NON_DICT_HANDLER = """\
def handle(input_data):
    return "just a string"
"""

_RAISING_HANDLER = """\
def handle(input_data):
    raise ValueError("boom")
"""

_NO_HANDLE_HANDLER = """\
def process(input_data):
    return {}
"""

_SYNTAX_ERROR_HANDLER = """\
def handle(input_data
    return {}
"""

_CONTRACT_PATH = (
    Path(__file__).parents[4]
    / "src/omnimarket/nodes/node_generation_consumer/contract.yaml"
)


def _write_handler(sandbox: Path, node_name: str, source: str) -> None:
    node_dir = sandbox / node_name
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "handler.py").write_text(source)


@pytest.mark.unit
def test_contract_routes_node_deploy_to_generated_executor() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    handlers = contract["handler_routing"]["handlers"]

    deploy_routes = [
        entry
        for entry in handlers
        if entry.get("event_type") == "omnimarket.node-deploy"
    ]

    assert len(deploy_routes) == 1
    route = deploy_routes[0]
    assert route["message_category"] == "command"
    assert route["handler"] == {
        "name": "HandlerGeneratedExecutor",
        "module": (
            "omnimarket.nodes.node_generation_consumer.handlers."
            "handler_generated_executor"
        ),
    }
    assert route["event_model"] == {
        "name": "ModelNodeDeploy",
        "module": "omnimarket.nodes.node_generation_consumer.models.model_generation",
    }


@pytest.mark.unit
def test_contract_routes_generation_request_to_generation_consumer_only() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    handlers = contract["handler_routing"]["handlers"]

    generation_routes = [
        entry
        for entry in handlers
        if entry.get("event_type") == "omnimarket.node-generation-requested"
    ]

    assert len(generation_routes) == 1
    route = generation_routes[0]
    assert route["message_category"] == "command"
    assert route["handler"]["name"] == "HandlerGenerationConsumer"
    assert route["event_model"]["name"] == "ModelNodeGenerationRequest"


@pytest.mark.unit
def test_execute_returns_result_from_valid_handler() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_echo", _VALID_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_echo", {"value": "hello"})

    assert result == {"echo": "hello"}


@pytest.mark.unit
def test_execute_returns_error_when_handler_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.execute("node_nonexistent", {})

    assert "error" in result
    assert "Handler not found" in result["error"]


@pytest.mark.unit
def test_execute_returns_error_when_no_handle_function() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_no_handle", _NO_HANDLE_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_no_handle", {})

    assert "error" in result
    assert "missing handle()" in result["error"]


@pytest.mark.unit
def test_execute_returns_error_when_handle_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_raises", _RAISING_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_raises", {})

    assert "error" in result
    assert "boom" in result["error"]


@pytest.mark.unit
def test_execute_returns_error_when_syntax_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_bad_syntax", _SYNTAX_ERROR_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_bad_syntax", {})

    assert "error" in result
    assert "Failed to load generated handler" in result["error"]


@pytest.mark.unit
def test_execute_wraps_non_dict_result() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_str_result", _NON_DICT_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_str_result", {})

    assert result == {"result": "just a string"}


@pytest.mark.unit
def test_execute_picks_up_updated_handler_without_reinit() -> None:
    """Re-importing on each call means an updated handler.py is picked up immediately."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_hot", _VALID_HANDLER)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        first = executor.execute("node_hot", {"value": "v1"})
        assert first == {"echo": "v1"}

        # Overwrite handler in place — simulates hot-reload write
        updated = "def handle(input_data):\n    return {'updated': True}\n"
        _write_handler(sandbox, "node_hot", updated)

        second = executor.execute("node_hot", {})
        assert second == {"updated": True}


@pytest.mark.unit
def test_default_sandbox_resolves_from_onex_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMN-12854: the sandbox resolves under ONEX_STATE_DIR (preferred).

    OMN-13605 (Phase 0.1): the hackathon-tagged ("hackathon","generated") subdir
    is removed — the node is now a permanent canonical artifact, so the sandbox
    lives under a node-scoped, hackathon-free subdir.
    """
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state-dir"))
    executor = HandlerGeneratedExecutor()
    assert (
        executor.sandbox_dir
        == tmp_path / "state-dir" / "node_generation_consumer" / "sandbox"
    )
    assert executor.sandbox_dir.is_absolute()
    # The hackathon hack is gone — assert it never appears in the resolved path.
    assert "hackathon" not in str(executor.sandbox_dir)


@pytest.mark.unit
def test_default_sandbox_falls_back_to_onex_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMN-12854: ONEX_STATE_ROOT is used when ONEX_STATE_DIR is unset."""
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.setenv("ONEX_STATE_ROOT", str(tmp_path / "state-root"))
    executor = HandlerGeneratedExecutor()
    assert (
        executor.sandbox_dir
        == tmp_path / "state-root" / "node_generation_consumer" / "sandbox"
    )


@pytest.mark.unit
def test_default_sandbox_fails_fast_when_state_root_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-12854: no hardcoded relative fallback — fail fast when env is unset."""
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="ONEX_STATE"):
        HandlerGeneratedExecutor()


# ---------------------------------------------------------------------------
# deploy() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_writes_handler_and_contract_to_sandbox() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        result = executor.deploy(
            {
                "node_name": "node_sentiment",
                "contract_yaml": "name: node_sentiment\n",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-deploy-1",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        )

        assert result["status"] == "ok"
        assert result["node_name"] == "node_sentiment"
        assert (sandbox / "node_sentiment" / "handler.py").read_text() == _VALID_HANDLER
        assert (sandbox / "node_sentiment" / "contract.yaml").exists()


@pytest.mark.unit
def test_deploy_registers_node_for_execution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        executor.deploy(
            {
                "node_name": "node_echo2",
                "contract_yaml": "",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-deploy-2",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        )

        assert "node_echo2" in executor._registry
        assert executor._registry["node_echo2"] == sandbox / "node_echo2" / "handler.py"


@pytest.mark.unit
def test_deploy_then_execute_runs_generated_handler() -> None:
    """Full deploy→execute round-trip without any pre-written files."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        executor.deploy(
            {
                "node_name": "node_round_trip",
                "contract_yaml": "",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-rt-1",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        )

        result = executor.execute("node_round_trip", {"value": "deployed"})

    assert result == {"echo": "deployed"}


@pytest.mark.unit
def test_handle_is_runtime_entrypoint_for_node_deploy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        result = executor.handle(
            {
                "node_name": "node_runtime_entry",
                "contract_yaml": "name: node_runtime_entry\n",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-handle-1",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        )

    assert result["status"] == "completed"
    assert result["node_name"] == "node_runtime_entry"
    assert result["_runtime_backend"] == "sandbox"
    assert result["hot_load"] is False
    assert result["output"] == {"echo": "none"}


@pytest.mark.unit
def test_deploy_accepts_json_bytes_payload() -> None:
    import json

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        raw = json.dumps(
            {
                "node_name": "node_bytes",
                "contract_yaml": "",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-bytes-1",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        ).encode()

        result = executor.deploy(raw)

    assert result["status"] == "ok"
    assert result["node_name"] == "node_bytes"


@pytest.mark.unit
def test_deploy_returns_error_on_missing_node_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(
            {"handler_source": _VALID_HANDLER, "contract_yaml": ""}
        )

    assert "error" in result
    assert "missing node_name" in result["error"]


@pytest.mark.unit
def test_deploy_returns_error_on_missing_handler_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy({"node_name": "node_no_src", "contract_yaml": ""})

    assert "error" in result
    assert "missing handler_source" in result["error"]


@pytest.mark.unit
def test_deploy_returns_error_on_invalid_json_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(b"not valid json {")

    assert "error" in result
    assert "Invalid deploy payload JSON" in result["error"]


@pytest.mark.unit
def test_deploy_rejects_path_traversal_in_node_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(
            {
                "node_name": "../../etc/passwd",
                "handler_source": _VALID_HANDLER,
                "contract_yaml": "",
            }
        )

    assert "error" in result
    assert "unsafe" in result["error"]


@pytest.mark.unit
def test_deploy_rejects_absolute_node_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(
            {
                "node_name": "/etc/pwned",
                "handler_source": _VALID_HANDLER,
                "contract_yaml": "",
            }
        )

    assert "error" in result
    assert "unsafe" in result["error"]


# ---------------------------------------------------------------------------
# on_deploy_event() consumer entrypoint — B1 (OMN-12826)
#   Consumes onex.cmd.omnimarket.node-deploy.v1, sandbox-loads + invokes the
#   generated node, and returns/emits the SAME terminal result shape future
#   runtime dispatch (NodeInvocationAdapter.dispatch) produces.
# ---------------------------------------------------------------------------

_TERMINAL_TOPIC = "onex.evt.omnimarket.generated-node-invoked.v1"
_DEPLOY_TOPIC = "onex.cmd.omnimarket.node-deploy.v1"


def _deploy_payload(node_name: str, source: str, correlation_id: str) -> dict[str, str]:
    import hashlib

    return {
        "node_name": node_name,
        "contract_yaml": f"name: {node_name}\n",
        "handler_source": source,
        "correlation_id": correlation_id,
        "generated_contract_hash": "sha256:"
        + hashlib.sha256(f"name: {node_name}\n".encode()).hexdigest(),
        "generated_handler_hash": "sha256:"
        + hashlib.sha256(source.encode()).hexdigest(),
    }


def _typed_deploy_payload(
    node_name: str, source: str, correlation_id: str
) -> ModelNodeDeploy:
    return ModelNodeDeploy.model_validate(
        _deploy_payload(node_name, source, correlation_id)
    )


@pytest.mark.unit
def test_on_deploy_event_returns_future_runtime_terminal_shape() -> None:
    """The terminal result MUST carry the same evidence keys future runtime
    dispatch (NodeInvocationAdapter.dispatch) returns — no demo-only shape."""
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.on_deploy_event(
            _deploy_payload("node_echo", _VALID_HANDLER, "corr-1")
        )

    # The five evidence keys future runtime dispatch always returns.
    for key in (
        "status",
        "_runtime_backend",
        "_event_bus_backend",
        "_state_store_backend",
        "_node_contract",
        "_command_topic",
    ):
        assert key in result, f"missing future-dispatch terminal key: {key}"

    assert result["status"] == "completed"
    assert result["_command_topic"] == _DEPLOY_TOPIC
    assert result["_node_contract"] == _TERMINAL_TOPIC
    # Sandbox path must be explicitly labelled non-hot-load (plan acceptance).
    assert result["_runtime_backend"] == "sandbox"
    assert result["hot_load"] is False
    assert result["correlation_id"] == "corr-1"
    assert result["node_name"] == "node_echo"


@pytest.mark.unit
def test_on_deploy_event_carries_invocation_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.on_deploy_event(
            _deploy_payload("node_echo", _VALID_HANDLER, "corr-2"),
            input_data={"value": "wired"},
        )

    assert result["status"] == "completed"
    assert result["output"] == {"echo": "wired"}


@pytest.mark.unit
def test_on_deploy_event_emits_terminal_event_to_contract_topic() -> None:
    captured: list[tuple[str, bytes]] = []

    def _publisher(topic: str, payload: bytes) -> None:
        captured.append((topic, payload))

    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(
            sandbox_dir=Path(tmp), event_publisher=_publisher
        )
        executor.on_deploy_event(
            _deploy_payload("node_echo", _VALID_HANDLER, "corr-3"),
            input_data={"value": "emitted"},
        )

    assert len(captured) == 1
    topic, payload = captured[0]
    assert topic == _TERMINAL_TOPIC
    decoded = __import__("json").loads(payload)
    assert decoded["status"] == "completed"
    assert decoded["output"] == {"echo": "emitted"}
    assert decoded["correlation_id"] == "corr-3"


@pytest.mark.unit
def test_on_deploy_event_replay_returns_stored_terminal_without_republishing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    captured: list[tuple[str, bytes]] = []

    def _publisher(topic: str, payload: bytes) -> None:
        captured.append((topic, payload))

    executor = HandlerGeneratedExecutor(
        sandbox_dir=tmp_path, event_publisher=_publisher
    )
    payload = _deploy_payload("node_echo", _VALID_HANDLER, "corr-replay-exec-1")

    first = executor.on_deploy_event(payload, input_data={"value": "first"})
    node_handler = tmp_path / "node_echo" / "handler.py"
    mtime_first = node_handler.stat().st_mtime_ns
    second = executor.on_deploy_event(
        {**payload, "handler_source": _RAISING_HANDLER},
        input_data={"value": "second"},
    )

    assert second == first
    assert len(captured) == 1
    assert node_handler.stat().st_mtime_ns == mtime_first


@pytest.mark.unit
def test_on_deploy_event_failed_status_when_handler_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.on_deploy_event(
            _deploy_payload("node_raises", _RAISING_HANDLER, "corr-4")
        )

    assert result["status"] == "failed"
    assert "boom" in result["error"]
    # Terminal evidence shape is preserved even on failure.
    assert result["_command_topic"] == _DEPLOY_TOPIC
    assert result["_runtime_backend"] == "sandbox"
    assert result["hot_load"] is False


@pytest.mark.unit
def test_on_deploy_event_failed_status_on_bad_deploy_payload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.on_deploy_event({"handler_source": _VALID_HANDLER})

    assert result["status"] == "failed"
    assert "node_name" in result["error"]
    assert result["_command_topic"] == _DEPLOY_TOPIC


@pytest.mark.unit
def test_on_deploy_event_accepts_json_bytes() -> None:
    import json

    raw = json.dumps(_deploy_payload("node_echo", _VALID_HANDLER, "corr-5")).encode()
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.on_deploy_event(raw, input_data={"value": "bytes"})

    assert result["status"] == "completed"
    assert result["output"] == {"echo": "bytes"}


@pytest.mark.unit
def test_handle_accepts_typed_model_node_deploy_payload() -> None:
    """Auto-wiring validates node-deploy JSON into ModelNodeDeploy before handle()."""
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.handle(
            _typed_deploy_payload("node_typed", _VALID_HANDLER, "corr-typed-1")
        )

    assert result["status"] == "completed"
    assert result["node_name"] == "node_typed"
    assert result["output"] == {"echo": "none"}
    assert result["_command_topic"] == _DEPLOY_TOPIC


@pytest.mark.unit
def test_terminal_topic_resolved_from_contract_not_hardcoded(
    tmp_path: Path,
) -> None:
    """The terminal-result topic must come from the node contract, not a literal."""
    executor = HandlerGeneratedExecutor(sandbox_dir=tmp_path)
    assert executor.terminal_topic == _TERMINAL_TOPIC
    assert executor.deploy_topic == _DEPLOY_TOPIC


# ---------------------------------------------------------------------------
# OMN-13605 (Phase 0.1): hot-load + full-package scaffold spine.
#   A node-deploy must (a) hot-load + invoke the generated node in-session AND
#   (b) scaffold the full 10-file canonical package into the worktree staging
#   dir. The hackathon-tagged sandbox subdir hack is removed; the staging path is
#   resolved fail-fast from env (no hardcoded paths).
# ---------------------------------------------------------------------------

# The 10 files the node_generate_node_effect scaffolder writes for a canonical
# node package (see HandlerGenerateNode._render_files). The full-package half of
# the spine must materialize exactly these.
_CANONICAL_PACKAGE_FILES = {
    "contract.yaml",
    "metadata.yaml",
    "__init__.py",
    "handlers/__init__.py",
    "handlers/handler_sentiment.py",
    "models/__init__.py",
    "models/model_sentiment_request.py",
    "models/model_sentiment_result.py",
    "tests/__init__.py",
    "tests/test_golden_chain.py",
}


def _generated_contract_yaml(node_name: str, node_type: str = "compute") -> str:
    return f"name: {node_name}\nnode_type: {node_type}\n"


def _scaffoldable_deploy_payload(
    node_name: str, source: str, correlation_id: str, node_type: str = "compute"
) -> dict[str, str]:
    payload = _deploy_payload(node_name, source, correlation_id)
    payload["contract_yaml"] = _generated_contract_yaml(node_name, node_type)
    return payload


@pytest.mark.unit
def test_staging_dir_resolves_from_dedicated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMN-13605: the staging root prefers ONEX_GENERATED_STAGING_DIR."""
    monkeypatch.setenv("ONEX_GENERATED_STAGING_DIR", str(tmp_path / "staging"))
    executor = HandlerGeneratedExecutor(sandbox_dir=tmp_path)
    assert executor.staging_dir == tmp_path / "staging"
    assert "hackathon" not in str(executor.staging_dir)


@pytest.mark.unit
def test_staging_dir_falls_back_to_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMN-13605: without a dedicated staging dir, fall back to the state root."""
    monkeypatch.delenv("ONEX_GENERATED_STAGING_DIR", raising=False)
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    executor = HandlerGeneratedExecutor(sandbox_dir=tmp_path)
    assert (
        executor.staging_dir
        == tmp_path / "state" / "node_generation_consumer" / "staging"
    )


@pytest.mark.unit
def test_staging_dir_fails_fast_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMN-13605: no hardcoded relative staging fallback — fail fast."""
    monkeypatch.delenv("ONEX_GENERATED_STAGING_DIR", raising=False)
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    executor = HandlerGeneratedExecutor(sandbox_dir=tmp_path)
    with pytest.raises(RuntimeError, match="staging root"):
        _ = executor.staging_dir


@pytest.mark.unit
def test_scaffold_package_writes_full_canonical_package(tmp_path: Path) -> None:
    """OMN-13605: the scaffolder materializes the full 10-file canonical package."""
    staging = tmp_path / "staging"
    executor = HandlerGeneratedExecutor(
        sandbox_dir=tmp_path / "sandbox", staging_dir=staging
    )

    result = executor.scaffold_package(
        "node_sentiment", _generated_contract_yaml("node_sentiment"), "corr-scaffold-1"
    )

    assert result["status"] == "ok"
    package_dir = staging / "node_sentiment"
    written = {
        str(p.relative_to(package_dir)) for p in package_dir.rglob("*") if p.is_file()
    }
    assert written == _CANONICAL_PACKAGE_FILES
    assert len(result["created_files"]) == len(_CANONICAL_PACKAGE_FILES)
    # The scaffolded contract is real (parses, names the node).
    assert "node_sentiment" in (package_dir / "contract.yaml").read_text()


@pytest.mark.unit
def test_scaffold_package_skips_unscaffoldable_node_name(tmp_path: Path) -> None:
    """A malformed/unknown node name is skipped, not raised mid-invoke."""
    executor = HandlerGeneratedExecutor(
        sandbox_dir=tmp_path / "sandbox", staging_dir=tmp_path / "staging"
    )
    result = executor.scaffold_package("unknown", "name: unknown\n", "corr-skip-1")
    assert "error" in result
    assert "scaffolder pattern" in result["error"]


@pytest.mark.unit
def test_on_deploy_event_hot_loads_and_writes_full_package(tmp_path: Path) -> None:
    """OMN-13605 golden chain: a single node-deploy proves BOTH halves.

    (a) the generated node is hot-loaded + invoked in-session (terminal carries
        the invocation output), AND
    (b) the full 10-file canonical package is written to the worktree staging
        dir (terminal.scaffold carries the staging_dir + created_files).
    """
    sandbox = tmp_path / "sandbox"
    staging = tmp_path / "staging"
    executor = HandlerGeneratedExecutor(sandbox_dir=sandbox, staging_dir=staging)

    result = executor.on_deploy_event(
        _scaffoldable_deploy_payload("node_sentiment", _VALID_HANDLER, "corr-spine-1"),
        input_data={"value": "spine"},
    )

    # (a) hot-load + invoke in-session.
    assert result["status"] == "completed"
    assert result["node_name"] == "node_sentiment"
    assert result["output"] == {"echo": "spine"}
    assert result["_runtime_backend"] == "sandbox"
    assert result["hot_load"] is False
    # The sandbox handler was actually written + is importable on disk.
    assert (sandbox / "node_sentiment" / "handler.py").exists()

    # (b) full 10-file canonical package on disk in the staging dir.
    scaffold = result["scaffold"]
    assert scaffold["status"] == "ok"
    package_dir = staging / "node_sentiment"
    written = {
        str(p.relative_to(package_dir)) for p in package_dir.rglob("*") if p.is_file()
    }
    assert written == _CANONICAL_PACKAGE_FILES
    assert Path(scaffold["staging_dir"]) == package_dir


@pytest.mark.unit
def test_on_deploy_event_scaffold_failure_does_not_fail_invocation(
    tmp_path: Path,
) -> None:
    """The hot-load and full-package halves are independent.

    An unscaffoldable node name (no canonical package) must still hot-load +
    invoke in-session; the scaffold outcome is surfaced as an error in the
    scaffold block, not as an invocation failure.
    """
    executor = HandlerGeneratedExecutor(
        sandbox_dir=tmp_path / "sandbox", staging_dir=tmp_path / "staging"
    )
    # _deploy_payload writes contract_yaml "name: node_echo\n" — node_echo IS a
    # valid name, so force an unscaffoldable name via a separate payload.
    payload = _deploy_payload("node_echo", _VALID_HANDLER, "corr-indep-1")
    payload["contract_yaml"] = "name: node_echo\nnode_type: not_a_real_type\n"

    result = executor.on_deploy_event(payload, input_data={"value": "indep"})

    # Invocation still completes even though the contract node_type is bogus.
    assert result["status"] == "completed"
    assert result["output"] == {"echo": "indep"}
    # node_type fell back to COMPUTE, so the scaffold still succeeds here.
    assert result["scaffold"]["status"] == "ok"


@pytest.mark.unit
def test_on_deploy_event_node_type_drives_archetype_from_contract(
    tmp_path: Path,
) -> None:
    """The scaffolded contract's archetype is taken from the generated contract."""
    executor = HandlerGeneratedExecutor(
        sandbox_dir=tmp_path / "sandbox", staging_dir=tmp_path / "staging"
    )
    result = executor.on_deploy_event(
        _scaffoldable_deploy_payload(
            "node_router", _VALID_HANDLER, "corr-reducer-1", node_type="reducer"
        ),
        input_data={},
    )
    assert result["status"] == "completed"
    assert result["scaffold"]["status"] == "ok"
    contract_text = (tmp_path / "staging" / "node_router" / "contract.yaml").read_text()
    assert "node_type: reducer" in contract_text
