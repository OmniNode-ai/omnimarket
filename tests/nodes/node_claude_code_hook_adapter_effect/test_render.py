# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_claude_code_binding_render_compute: the rendered mod module (OMN-18531, T0.3 AC1-AC5, AC11)."""

from __future__ import annotations

import json
import re

import pytest

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    ModelBindingRenderRequest,
    ModelClientArgument,
)
from omnimarket.nodes.node_claude_code_binding_render_compute.handlers.handler_binding_render import (
    HandlerClaudeCodeBindingRender,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.binding_render_error import (
    BindingRenderError,
)

from .conftest import make_binding

pytestmark = pytest.mark.unit

_STRING = re.compile(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
_COMMENT = re.compile(r"//[^\n]*")
_BRANCH_TOKENS = re.compile(
    r"\b(?:if|else|switch|case|for|while|do|try|throw|eval|Function)\b|\?|&&|\|\||\?\?"
)


def _render(*bindings, max_mode=EnumClientMaxMode.REQUIRED):
    return HandlerClaudeCodeBindingRender().handle(
        ModelBindingRenderRequest(bindings=tuple(bindings), max_mode=max_mode)
    )


def _code_residue(module_text: str) -> str:
    """The module with comments and every string literal blanked."""
    return _STRING.sub('""', _COMMENT.sub("", module_text))


def test_ac1_command_is_exactly_the_adapter_entry_with_the_binding_name() -> None:
    out = _render(make_binding())
    assert "['onex-claude-code-hook', '--binding', 'delegation']" in out.module_text, (
        out.module_text
    )
    # OMN-21099: the event plus the classic base fields, read from the engine.
    assert (
        "stdin: JSON.stringify({ ...e, session_id: await $.session.id(), "
        "cwd: await $.session.cwd() })"
    ) in out.module_text


def test_ac2_required_host_maps_never_started_to_deny() -> None:
    binding = make_binding()
    text = _render(binding, max_mode=EnumClientMaxMode.REQUIRED).module_text
    assert "never_started: 'deny'" in text
    assert "other: 'deny'" in text
    assert f"deny: (ran, fall) => ({{ deny: {json.dumps(binding.deny_text)} }})" in text
    assert "never_started: 'native'" not in text


@pytest.mark.parametrize(
    "mode", [EnumClientMaxMode.OBSERVE, EnumClientMaxMode.SELECTIVE]
)
def test_ac2_observe_and_selective_map_never_started_to_native(mode) -> None:
    text = _render(make_binding(), max_mode=mode).module_text
    assert "never_started: 'native'" in text
    assert "never_started: 'deny'" not in text
    if mode is EnumClientMaxMode.OBSERVE:
        assert "other: 'native'" in text
        assert "deny: (ran, fall)" not in text
    else:
        assert "other: 'deny'" in text
        assert "deny: (ran, fall)" in text


def test_ac2_changing_the_maximum_changes_only_fallbacks_and_deny_answer() -> None:
    req = _render(make_binding(), max_mode=EnumClientMaxMode.REQUIRED).module_text
    obs = _render(make_binding(), max_mode=EnumClientMaxMode.OBSERVE).module_text
    req_lines = req.splitlines()
    deny_lines = [line for line in req_lines if line.startswith("  deny:")]
    assert len(deny_lines) == 1
    assert "  deny:" not in obs
    changed = [
        (a, b)
        for a, b in zip(
            [line for line in req_lines if line not in deny_lines],
            obs.splitlines(),
            strict=True,
        )
        if a != b
    ]
    assert changed, "the maximum must change the table"
    assert all(
        "never_started" in a or "other:" in a or "maximum mode" in a for a, _ in changed
    )


def test_ac3_render_is_byte_identical() -> None:
    first = _render(make_binding())
    second = _render(make_binding())
    assert first.module_text == second.module_text
    assert first.module_sha256 == second.module_sha256


def test_ac3_changing_one_deadline_changes_only_that_literal() -> None:
    a = _render(make_binding(deadline_ms=30000)).module_text.splitlines()
    b = _render(make_binding(deadline_ms=45000)).module_text.splitlines()
    diff = [(x, y) for x, y in zip(a, b, strict=True) if x != y]
    assert len(a) == len(b)
    assert len(diff) == 1
    assert "30000" in diff[0][0]
    assert "45000" in diff[0][1]
    assert diff[0][0].replace("30000", "45000") == diff[0][1]


def test_ac4_no_branching_beyond_table_lookups() -> None:
    residue = _code_residue(_render(make_binding()).module_text)
    assert _BRANCH_TOKENS.findall(residue) == [], residue


def test_ac4_a_data_field_carrying_code_is_a_string_literal() -> None:
    hostile = "x'); process.exit(1); if (e) { throw e } ('"
    benign = "x"
    h = _render(
        make_binding(deny_text=hostile, never_started_pattern="a|b'); if(1){")
    ).module_text
    b = _render(make_binding(deny_text=benign, never_started_pattern="a|b")).module_text
    assert _code_residue(h) == _code_residue(b)
    assert _BRANCH_TOKENS.findall(_code_residue(h)) == []


def test_ac5_an_argument_not_drawn_from_the_hook_input_fails_the_render() -> None:
    bad = make_binding(
        arguments=(ModelClientArgument(name="mode", source="observe"),),
    )
    with pytest.raises(BindingRenderError, match="mode"):
        _render(bad)


def test_ac5_the_argv_carries_nothing_but_the_binding_name() -> None:
    text = _render(make_binding()).module_text
    argvs = re.findall(r"ARGV_\w+ = (\[.*\])", text)
    assert argvs == ["['onex-claude-code-hook', '--binding', 'delegation']"]


def test_a_binding_name_with_shell_or_code_characters_fails_the_render() -> None:
    with pytest.raises(BindingRenderError):
        _render(make_binding(name="x'); evil('"))


def test_ac11_additive_row_renders_one_registration_with_the_exact_command(
    additive_binding,
) -> None:
    out = _render(additive_binding)
    assert out.registrations == _render(additive_binding).registrations
    (reg,) = out.registrations
    assert reg.event == "SessionStart"
    assert reg.command == "onex-claude-code-hook --binding session_start"
    assert "session_start" not in out.module_text


def test_catch_handler_never_throws_for_a_replacing_binding() -> None:
    text = _render(make_binding(), max_mode=EnumClientMaxMode.REQUIRED).module_text
    assert (
        ".catch(($, e, next) => ($.ui.log(FAILED + 'hook threw'), "
        "ANSWER[FALLBACK.other](undefined, () => next(e))))" in text
    )
    assert "other: 'deny'" in text
    assert "deny: (ran, fall) => ({ deny:" in text


@pytest.mark.parametrize("mode", list(EnumClientMaxMode))
def test_every_failure_path_logs_one_line_before_resolving_its_answer(mode) -> None:
    """OMN-21341 AC3 (OMN-21116 AC2): a failed /compact is never silent."""
    text = _render(make_binding(), max_mode=mode).module_text
    assert (
        "false: ran => ({ ran, lines: [FAILED + 'exit ' + ran.exitCode], answer: FALLBACK.other })"
        in text
    )
    assert (
        "err => ({ ran: undefined, lines: [FAILED + String(err)], answer: FALLBACK[KIND[String(PATTERN_delegation.test(String(err)))]] })"
        in text
    )
    log = "plan.lines.forEach(line => $.ui.log(line))"
    answer = "return ANSWER[plan.answer](plan.ran, () => next(e))"
    assert text.index(log) < text.index(answer)
    assert text.count("$.ui.log(") == 2  # One planned line or the catch handler.
    assert "native: (ran, fall) => fall()" in text
    assert "output: (ran, fall) => JSON.parse(ran.stdout)" in text
    assert (
        "true: ran => ({ ran, lines: [], answer: OUTPUT[Math.min(ran.stdout.length, 1)] })"
        in text
    )


@pytest.mark.parametrize("mode", list(EnumClientMaxMode))
@pytest.mark.parametrize("with_additive", [False, True])
def test_no_replacing_binding_has_only_native_fallbacks(
    mode, with_additive, additive_binding
) -> None:
    bindings = (additive_binding,) if with_additive else ()
    text = _render(*bindings, max_mode=mode).module_text
    assert "never_started: 'native'" in text
    assert "other: 'native'" in text
    assert "deny: (ran, fall)" not in text


def test_first_replacing_binding_decides_the_fixed_deny_literal(
    additive_binding,
) -> None:
    first = make_binding(deny_text="first strictest binding")
    second = make_binding(name="second", deny_text="second binding")
    text = _render(additive_binding, first, second).module_text
    assert f"deny: {json.dumps(first.deny_text)}" in text
    assert second.deny_text not in text
    assert text.count("  deny: (ran, fall)") == 1
