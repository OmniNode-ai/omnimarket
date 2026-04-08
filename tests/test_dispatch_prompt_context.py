# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for AdapterLlmDispatch source context loading and prompt construction.

Covers: _build_coder_prompt, _extract_code_from_response, and _load_source_context.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
    AdapterLlmDispatch,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MOCK_ENDPOINT = ModelEndpointConfig(
    tier=EnumModelTier.LOCAL_CODER,
    base_url="http://localhost:8000",
    model_id="test-model",
)

_MOCK_CONFIGS: dict[EnumModelTier, ModelEndpointConfig] = {
    EnumModelTier.LOCAL_CODER: _MOCK_ENDPOINT,
}


def _make_adapter() -> AdapterLlmDispatch:
    return AdapterLlmDispatch(endpoint_configs=_MOCK_CONFIGS)


def _make_target(
    ticket_id: str = "OMN-TEST", title: str = "Fix handle()"
) -> BuildTarget:
    return BuildTarget(ticket_id=ticket_id, title=title, buildability="auto_buildable")


# ---------------------------------------------------------------------------
# _build_coder_prompt tests
# ---------------------------------------------------------------------------


def test_generate_prompt_includes_source_files() -> None:
    """Prompt must include actual source code with section boundaries."""
    adapter = _make_adapter()
    template_src = "class NodeTemplate:\n    def handle(self, req): ..."
    target_src = "class HandlerBroken:\n    def run_pipeline(self): ..."

    prompt = adapter._build_coder_prompt(
        target=_make_target(),
        template_source=template_src,
        target_source=target_src,
    )

    # Source code present verbatim
    assert template_src in prompt
    assert target_src in prompt
    # Section boundaries preserved
    assert "## TEMPLATE" in prompt
    assert "## TARGET" in prompt
    # Template comes before target
    assert prompt.index("## TEMPLATE") < prompt.index("## TARGET")


def test_prompt_includes_ticket_header() -> None:
    """Prompt must include ticket ID and title."""
    adapter = _make_adapter()
    target = _make_target(ticket_id="OMN-9999", title="Add canonical handle()")
    prompt = adapter._build_coder_prompt(
        target=target,
        template_source="template code",
        target_source="target code",
    )
    assert "OMN-9999" in prompt
    assert "Add canonical handle()" in prompt


def test_prompt_includes_model_sources() -> None:
    """Prompt must include model sources under ## RELEVANT MODELS section."""
    adapter = _make_adapter()
    model_src = "class ModelFoo(BaseModel):\n    x: int"

    prompt = adapter._build_coder_prompt(
        target=_make_target(),
        template_source="template",
        target_source="target",
        model_sources=[model_src],
    )

    assert "## RELEVANT MODELS" in prompt
    assert model_src in prompt


def test_prompt_respects_context_limit() -> None:
    """Prompt drops model files to stay within budget. Handlers are never truncated."""
    adapter = _make_adapter()
    big_model = "x" * 60000

    prompt = adapter._build_coder_prompt(
        target=_make_target(),
        template_source="template",
        target_source="target",
        model_sources=[big_model],
        max_context_chars=48000,
    )

    # Handlers preserved
    assert "template" in prompt
    assert "target" in prompt
    # Budget respected
    assert len(prompt) <= 48000
    # Big model file was dropped
    assert big_model not in prompt


def test_prompt_includes_small_model_when_fits() -> None:
    """Small model files should be included when they fit within the budget."""
    adapter = _make_adapter()
    small_model = "class ModelBar(BaseModel):\n    y: str"

    prompt = adapter._build_coder_prompt(
        target=_make_target(),
        template_source="template",
        target_source="target",
        model_sources=[small_model],
        max_context_chars=48000,
    )

    assert small_model in prompt


def test_prompt_no_models_returns_base_prompt() -> None:
    """When no models provided, prompt has no RELEVANT MODELS section."""
    adapter = _make_adapter()
    prompt = adapter._build_coder_prompt(
        target=_make_target(),
        template_source="template",
        target_source="target",
    )
    assert "## RELEVANT MODELS" not in prompt


# ---------------------------------------------------------------------------
# _extract_code_from_response tests
# ---------------------------------------------------------------------------


def test_extract_code_strips_python_fences() -> None:
    """Code extraction handles ```python ... ``` fences."""
    adapter = _make_adapter()
    raw = "Here is the code:\n```python\ndef handle(): pass\n```\nDone."
    code = adapter._extract_code_from_response(raw)
    assert code.strip() == "def handle(): pass"
    assert "```" not in code


def test_extract_code_strips_generic_fences() -> None:
    """Code extraction handles ``` ... ``` fences without language tag."""
    adapter = _make_adapter()
    raw = "Result:\n```\nclass Foo: pass\n```"
    code = adapter._extract_code_from_response(raw)
    assert code.strip() == "class Foo: pass"
    assert "```" not in code


def test_extract_code_returns_raw_when_no_fences() -> None:
    """When no fences present, raw response is returned as-is."""
    adapter = _make_adapter()
    raw = "def handle(self): return None"
    code = adapter._extract_code_from_response(raw)
    assert code == raw


def test_extract_code_prefers_python_fence_over_generic() -> None:
    """```python fence takes priority over generic ``` fence."""
    adapter = _make_adapter()
    raw = textwrap.dedent("""\
        ```
        generic block
        ```
        ```python
        def real_code(): pass
        ```
    """)
    code = adapter._extract_code_from_response(raw)
    assert "def real_code(): pass" in code


# ---------------------------------------------------------------------------
# _load_source_context tests
# ---------------------------------------------------------------------------


def test_load_source_context_reads_handler_file(tmp_path: Path) -> None:
    """_load_source_context reads the target handler file."""
    adapter = _make_adapter()

    # Set up a minimal node directory
    node_dir = tmp_path / "node_test"
    handlers_dir = node_dir / "handlers"
    handlers_dir.mkdir(parents=True)
    handler_file = handlers_dir / "handler_test.py"
    handler_file.write_text("def handle(): pass\n")

    template_source, target_source, _model_sources = adapter._load_source_context(
        target_node_dir=node_dir,
        template_node_dir=node_dir,  # use self as template for simplicity
    )

    assert "def handle(): pass" in target_source
    assert "def handle(): pass" in template_source


def test_load_source_context_reads_model_files(tmp_path: Path) -> None:
    """_load_source_context includes model_*.py files."""
    adapter = _make_adapter()

    node_dir = tmp_path / "node_test"
    handlers_dir = node_dir / "handlers"
    handlers_dir.mkdir(parents=True)
    (handlers_dir / "handler_test.py").write_text("def handle(): pass\n")

    models_dir = node_dir / "models"
    models_dir.mkdir()
    (models_dir / "model_foo.py").write_text("class ModelFoo(BaseModel): pass\n")

    _, _, model_sources = adapter._load_source_context(
        target_node_dir=node_dir,
        template_node_dir=node_dir,
    )

    assert any("ModelFoo" in src for src in model_sources)


def test_load_source_context_missing_dir_returns_empty(tmp_path: Path) -> None:
    """_load_source_context returns empty strings gracefully for missing dirs."""
    adapter = _make_adapter()
    missing_dir = tmp_path / "node_nonexistent"

    template_source, target_source, model_sources = adapter._load_source_context(
        target_node_dir=missing_dir,
        template_node_dir=missing_dir,
    )

    assert template_source == ""
    assert target_source == ""
    assert model_sources == []


def test_load_source_context_includes_contract_yaml(tmp_path: Path) -> None:
    """_load_source_context appends contract.yaml to model_sources."""
    adapter = _make_adapter()

    node_dir = tmp_path / "node_test"
    handlers_dir = node_dir / "handlers"
    handlers_dir.mkdir(parents=True)
    (handlers_dir / "handler_test.py").write_text("def handle(): pass\n")

    contract = node_dir / "contract.yaml"
    contract.write_text("event_bus:\n  publish_topics: []\n")

    _, _, model_sources = adapter._load_source_context(
        target_node_dir=node_dir,
        template_node_dir=node_dir,
    )

    assert any("publish_topics" in src for src in model_sources)
