# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manual compact bindings render and install without binding auto (OMN-21335)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.claude_code_binding import (
    EnumBindingKind,
    EnumClientMaxMode,
    ModelBindingRenderRequest,
    ModelClientArgument,
    ModelClientBinding,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers import (
    handler_binding_install,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_binding_install import (
    HandlerClaudeCodeBindingInstall,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models import (
    ModelBindingInstallRequest,
)
from omnimarket.nodes.node_claude_code_binding_render_compute.handlers.handler_binding_render import (
    ADAPTER_COMMAND,
    HandlerClaudeCodeBindingRender,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.binding_render_error import (
    BindingRenderError,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_bindings_loader import (
    load_client_section,
)

from .conftest import make_binding

pytestmark = pytest.mark.unit

_COMPACT_PROBE_ROWS = ("P0", "P6", "P11a", "P13", "P14")
_STRING = re.compile(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
_COMMENT = re.compile(r"//[^\n]*")
_BRANCH_TOKENS = re.compile(
    r"\b(?:if|else|switch|case|for|while|do|try|throw|eval|Function)\b|\?|&&|\|\||\?\?"
)


@pytest.fixture(autouse=True)
def _work_tree_probe_ends_at_tmp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep work-tree detection independent of stray git entries above tmp_path."""

    def inside_tmp_path_work_tree(path: Path) -> bool:
        return any(
            (p / ".git").exists()
            for p in (path, *path.parents)
            if p == tmp_path or tmp_path in p.parents
        )

    monkeypatch.setattr(
        handler_binding_install, "_inside_a_work_tree", inside_tmp_path_work_tree
    )
    monkeypatch.setattr(
        handler_binding_install, "_resolve_adapter_command", lambda: ADAPTER_COMMAND
    )


def _compact_binding(**overrides: Any) -> ModelClientBinding:
    fields: dict[str, Any] = {
        "name": "compact_manual",
        "kind": EnumBindingKind.REPLACING,
        "hook_event": "session.compact",
        "matcher_tool": "manual",
        "arguments": (ModelClientArgument(name="trigger", source="$.trigger"),),
        "probe_rows": _COMPACT_PROBE_ROWS,
    }
    fields.update(overrides)
    return make_binding(**fields)


def _render(binding: ModelClientBinding):
    return HandlerClaudeCodeBindingRender().handle(
        ModelBindingRenderRequest(
            bindings=(binding,), max_mode=EnumClientMaxMode.REQUIRED
        )
    )


def _code_residue(module_text: str) -> str:
    """The module with comments and every string literal blanked."""
    return _STRING.sub('""', _COMMENT.sub("", module_text))


def _install(tmp_path: Path, binding: ModelClientBinding):
    request = ModelBindingInstallRequest(
        bindings=(binding,),
        max_mode=EnumClientMaxMode.REQUIRED,
        mods_dir=str(tmp_path / "mods"),
        settings_path=str(tmp_path / ".claude" / "settings.json"),
        supported_probe_rows=_COMPACT_PROBE_ROWS,
    )
    return HandlerClaudeCodeBindingInstall().handle(request)


def test_adapter_template_compact_binding_manual_renders_trigger_and_argv() -> None:
    binding = _compact_binding()
    text = _render(binding).module_text
    assert 'on("session.compact", { trigger: "manual" }' in text
    assert f"['onex-claude-code-hook', '--binding', '{binding.name}']" in text
    assert '{ tool: "manual" }' not in text
    assert "never_started: 'deny'" in text
    assert "other: 'deny'" in text
    assert f"deny: (ran, fall) => ({{ deny: {json.dumps(binding.deny_text)} }})" in text
    assert (
        "plan.lines.forEach(line => $.ui.log(line))\n    return ANSWER[plan.answer](plan.ran, () => next(e))"
        in text
    )


@pytest.mark.parametrize("trigger", ["auto", None, "other"])
def test_adapter_template_compact_binding_rejects_non_manual_trigger(
    trigger: str | None,
) -> None:
    binding = _compact_binding(matcher_tool=trigger)
    with pytest.raises(BindingRenderError) as exc:
        _render(binding)
    assert binding.name in str(exc.value)
    assert "manual" in str(exc.value)


def test_adapter_template_compact_binding_rejects_additive_kind() -> None:
    binding = _compact_binding(kind=EnumBindingKind.ADDITIVE)
    with pytest.raises(BindingRenderError) as exc:
        _render(binding)
    assert binding.name in str(exc.value)
    assert "manual" in str(exc.value)


def test_adapter_template_compact_binding_is_deterministic_without_branching() -> None:
    binding = _compact_binding()
    first = _render(binding)
    second = _render(binding)
    assert first.module_text.encode("utf-8") == second.module_text.encode("utf-8")
    assert first.module_sha256 == second.module_sha256
    residue = _code_residue(first.module_text)
    assert _BRANCH_TOKENS.findall(residue) == [], residue


def test_adapter_template_compact_binding_installs_manual_with_record(
    tmp_path: Path,
) -> None:
    binding = _compact_binding()
    rendered = _render(binding)
    result = _install(tmp_path, binding)
    assert result.installed_bindings == (binding.name,)
    assert result.skipped_bindings == ()
    assert result.module_path is not None
    module_path = Path(result.module_path)
    on_disk = module_path.read_bytes()
    assert on_disk == rendered.module_text.encode("utf-8")
    assert (
        hashlib.sha256(on_disk).hexdigest()
        == result.installed_sha256
        == result.render_sha256
        == rendered.module_sha256
    )
    record = json.loads((module_path.parents[1] / "install_record.json").read_text())
    assert {"event": "session.compact", "matcher": "manual"} in record["events"]
    assert record["render_sha256"] == result.installed_sha256


def test_adapter_template_compact_binding_auto_install_writes_no_module(
    tmp_path: Path,
) -> None:
    binding = _compact_binding(matcher_tool="auto")
    with pytest.raises(BindingRenderError) as exc:
        _install(tmp_path, binding)
    assert binding.name in str(exc.value)
    assert "manual" in str(exc.value)
    assert list(tmp_path.rglob("register.ts")) == []


def test_adapter_template_compact_binding_contract_row_is_manual_only_and_installs(
    tmp_path: Path,
) -> None:
    section = load_client_section()
    compact = [b for b in section.bindings if b.hook_event == "session.compact"]
    assert [b.name for b in compact] == ["compaction"]
    assert {b.matcher_tool for b in compact} == {"manual"}
    assert all(b.matcher_tool != "auto" for b in section.bindings)
    result = _install(tmp_path, compact[0])
    assert result.installed_bindings == ("compaction",)
    assert result.module_path is not None
    module = Path(result.module_path).read_text(encoding="utf-8")
    assert 'on("session.compact", { trigger: "manual" }' in module
