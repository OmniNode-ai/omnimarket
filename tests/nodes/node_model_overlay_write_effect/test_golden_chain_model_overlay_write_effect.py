# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the developer model overlay written for each mode (OMN-20330).

AC1 native overlay at the path the contract names, read by the native loader.
AC2 local-stack lane overlay accepted by ``render_bifrost_delegation_contract``.
AC3 no key reference in any written overlay.

Each test names the failure it exists to catch.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.render_bifrost_delegation_contract import (
    render_bifrost_delegation_contract,
)

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.nodes.node_model_overlay_write_effect.handlers.handler_model_overlay_write import (
    HandlerModelOverlayWrite,
    node_config,
)
from omnimarket.nodes.node_model_overlay_write_effect.models.model_model_overlay_write_request import (
    ModelModelOverlayWriteRequest,
    OverlayMode,
)
from omnimarket.nodes.node_model_overlay_write_effect.protocols.protocol_overlay_file_store_backends import (
    FilesystemOverlayFileStore,
    InMemoryOverlayFileStore,
)

pytestmark = pytest.mark.unit

_BASE = Path(str(files("omnimarket").joinpath("configs/bifrost_delegation.yaml")))


def _ollama(
    mode: OverlayMode, target: Path, memory_gb: int = 16
) -> ModelModelOverlayWriteRequest:
    return ModelModelOverlayWriteRequest(
        mode=mode,
        source="ollama",
        memory_gb=memory_gb,
        target_path=target,
    )


def test_native_default_path_is_the_one_the_native_loader_reads() -> None:
    """The overlay is useless if it is written where the loader never looks."""
    path = Path(str(node_config()["modes"]["native"]["target_path"])).expanduser()
    assert path == Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"


def test_native_overlay_is_written_and_the_native_loader_binds_the_local_rungs(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bifrost_overrides.yaml"
    result = HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(
        _ollama("native", target)
    )
    assert result.outcome == "written"
    assert result.path == str(target)
    config = load_bifrost_delegation_config(overlay_path=target)
    by_id = {b.backend_id: b for b in config.backends}
    for backend_id in ("local-coder", "local-heavy-reasoning"):
        assert (
            by_id[backend_id].endpoint_url
            == "http://localhost:11434/v1/chat/completions"
        )
        assert by_id[backend_id].model_name == "qwen2.5-coder:7b"
        assert by_id[backend_id].max_tokens == 8192


def test_native_url_source_binds_the_named_server(tmp_path: Path) -> None:
    target = tmp_path / "bifrost_overrides.yaml"
    request = ModelModelOverlayWriteRequest(
        mode="native",
        source="url",
        url="http://127.0.0.1:8000",
        served_model_id="Qwen3.8-27B",
        parameter_count="27B",
        context_window=32768,
        target_path=target,
    )
    HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(request)
    rows = yaml.safe_load(target.read_text())["backends"]
    assert {r["endpoint_url"] for r in rows} == {
        "http://127.0.0.1:8000/v1/chat/completions"
    }
    assert {r["model_name"] for r in rows} == {"Qwen3.8-27B"}


def test_local_stack_overlay_passes_the_renderer(tmp_path: Path) -> None:
    """AC2: the renderer, run over the written file, validates it and binds the rungs."""
    overlay = tmp_path / "local.bifrost.yaml"
    result = HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(
        _ollama("local_stack", overlay, memory_gb=0)
    )
    assert result.outcome == "written"
    rendered = tmp_path / "rendered.yaml"
    render_bifrost_delegation_contract(
        source_path=_BASE,
        overlay_path=overlay,
        target_path=rendered,
        verify_endpoints=False,
    )
    backends = {
        b["backend_id"]: b for b in yaml.safe_load(rendered.read_text())["backends"]
    }
    for backend_id in ("local-coder", "local-heavy-reasoning"):
        assert backends[backend_id]["endpoint_url"] == (
            "http://host.docker.internal:11434/v1/chat/completions"
        )
        assert backends[backend_id]["model_name"] == "qwen2.5-coder:1.5b"


def test_local_stack_url_on_loopback_is_rewritten_for_the_container(
    tmp_path: Path,
) -> None:
    """A container cannot reach the developer's machine at localhost."""
    overlay = tmp_path / "local.bifrost.yaml"
    HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(
        ModelModelOverlayWriteRequest(
            mode="local_stack",
            source="url",
            url="http://localhost:8000/v1/chat/completions",
            served_model_id="Qwen3.8-27B",
            parameter_count="27B",
            context_window=32768,
            target_path=overlay,
        )
    )
    render_bifrost_delegation_contract(
        source_path=_BASE,
        overlay_path=overlay,
        target_path=tmp_path / "rendered.yaml",
        verify_endpoints=False,
    )
    rows = yaml.safe_load(overlay.read_text())["backends"]
    assert {r["endpoint_url"] for r in rows} == {
        "http://host.docker.internal:8000/v1/chat/completions"
    }


@pytest.mark.parametrize("mode", ["native", "local_stack"])
def test_no_key_reference_in_any_written_overlay(
    mode: OverlayMode, tmp_path: Path
) -> None:
    """AC3: the falsifier is a grep for secret_ref in the written file."""
    target = tmp_path / "overlay.yaml"
    HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(_ollama(mode, target))
    text = target.read_text()
    assert "secret_ref" not in text
    assert "credential" not in text


def test_a_second_identical_write_is_unchanged(tmp_path: Path) -> None:
    store = InMemoryOverlayFileStore()
    handler = HandlerModelOverlayWrite(store)
    request = _ollama("native", tmp_path / "o.yaml")
    assert handler.handle(request).outcome == "written"
    assert handler.handle(request).outcome == "unchanged"


def test_an_earlier_onboarding_file_is_replaced_not_refused(tmp_path: Path) -> None:
    """The lab-model file the earlier onboarding wrote must give way to the new overlay."""
    store = InMemoryOverlayFileStore()
    target = tmp_path / "o.yaml"
    store.write_text(
        target, "# Written by lab-onboarding: the lab model server.\nbackends: []\n"
    )
    assert (
        HandlerModelOverlayWrite(store).handle(_ollama("native", target)).outcome
        == "written"
    )


def test_a_named_ollama_row_is_used_whatever_the_memory(tmp_path: Path) -> None:
    """The developer's chosen Ollama model wins over the memory default."""
    target = tmp_path / "o.yaml"
    request = ModelModelOverlayWriteRequest(
        mode="native",
        source="ollama",
        memory_gb=64,
        served_model_id="qwen2.5-coder:1.5b",
        target_path=target,
    )
    HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(request)
    rows = yaml.safe_load(target.read_text())["backends"]
    assert {r["model_name"] for r in rows} == {"qwen2.5-coder:1.5b"}


def test_the_callers_marker_is_the_first_line_and_marks_the_file_as_replaceable(
    tmp_path: Path,
) -> None:
    store = InMemoryOverlayFileStore()
    target = tmp_path / "o.yaml"
    mark = "# Written by a caller: Ollama on this Mac."
    handler = HandlerModelOverlayWrite(store)
    request = ModelModelOverlayWriteRequest(
        mode="native",
        source="ollama",
        memory_gb=16,
        target_path=target,
        marker_line=mark,
    )
    assert handler.handle(request).outcome == "written"
    assert (store.read_text(target) or "").splitlines()[0] == mark
    other = request.model_copy(update={"memory_gb": 0})
    assert handler.handle(other).outcome == "written"
