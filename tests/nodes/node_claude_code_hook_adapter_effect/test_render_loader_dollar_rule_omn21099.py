# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the module accepted by Claude Code 2.1.309 and its $ usage rule (OMN-21099)."""

from __future__ import annotations

import re

import pytest

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    ModelBindingRenderRequest,
)
from omnimarket.nodes.node_claude_code_binding_render_compute.handlers.handler_binding_render import (
    HandlerClaudeCodeBindingRender,
)

from .conftest import make_binding

pytestmark = pytest.mark.unit

TARGET_OBSERVE = """\
// Rendered by node_claude_code_binding_render_compute from client_bindings.claude_code.
// Host maximum mode: observe. Generated; never hand-edited; not tracked in any repository.
import type { Register } from 'claude-code'

const KIND = { true: 'never_started', false: 'other' }
const FAILED = 'onex hook adapter did not answer: '
const FALLBACK = {
  never_started: 'native',
  other: 'native',
}
const ANSWER = {
  native: (ran, fall) => fall(),
  output: (ran, fall) => JSON.parse(ran.stdout),
}
const OUTPUT = ['native', 'output']
const PLAN = {
  true: ran => ({ ran, lines: [], answer: OUTPUT[Math.min(ran.stdout.length, 1)] }),
  false: ran => ({ ran, lines: [FAILED + 'exit ' + ran.exitCode], answer: FALLBACK.other }),
}
const ARGV_compaction = ['onex-claude-code-hook', '--binding', 'compaction']
const PATTERN_compaction = new RegExp("ENOENT|not found|cannot start|spawn")
const DEADLINE_compaction = 30000

export const register: Register = on => {
  on("session.compact", { trigger: "manual" }, async ($, e, next) => {
    const plan = await $.process.run(ARGV_compaction, { stdin: JSON.stringify({ ...e, session_id: await $.session.id(), cwd: await $.session.cwd() }), timeoutMs: DEADLINE_compaction }).then(
      ran => PLAN[String(ran.exitCode === 0)](ran),
      err => ({ ran: undefined, lines: [FAILED + String(err)], answer: FALLBACK[KIND[String(PATTERN_compaction.test(String(err)))]] }),
    )
    plan.lines.forEach(line => $.ui.log(line))
    return ANSWER[plan.answer](plan.ran, () => next(e))
  }).catch(($, e, next) => ($.ui.log(FAILED + 'hook threw'), ANSWER[FALLBACK.other](undefined, () => next(e))))
}
"""

# Match literals and comments together so comment markers inside contract strings
# cannot hide code. Preserve newlines to keep the parameter check exact.
_NON_CODE = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|//[^\n]*|/\*[\s\S]*?\*/"""
)
_PARAMETERS = re.compile(r"async \(\$, e, next\) =>|\.catch\(\(\$, e, next\) =>")
_INVALID_DOLLAR = re.compile(r"\$(?!\.[A-Za-z_][A-Za-z0-9_]*)")


def _assert_loader_dollar_rule(text: str) -> None:
    residue = _NON_CODE.sub("", text)
    residue = _PARAMETERS.sub("", residue)
    assert _INVALID_DOLLAR.search(residue) is None, residue


def test_observe_compaction_matches_the_loader_proven_module_exactly() -> None:
    binding = make_binding(
        name="compaction", hook_event="session.compact", matcher_tool="manual"
    )
    result = HandlerClaudeCodeBindingRender().handle(
        ModelBindingRenderRequest(
            bindings=(binding,), max_mode=EnumClientMaxMode.OBSERVE
        )
    )
    assert result.module_text.encode("utf-8") == TARGET_OBSERVE.encode("utf-8")


@pytest.mark.parametrize("mode", list(EnumClientMaxMode))
@pytest.mark.parametrize("binding_count", [0, 1, 2])
def test_render_uses_dollar_only_for_hook_parameters_or_direct_members(
    mode: EnumClientMaxMode, binding_count: int
) -> None:
    bindings = tuple(
        make_binding(
            name=f"binding_{i}",
            deny_text="// $, evil($); 'quoted' \"more\" \\",
            never_started_pattern="ENOENT|//|$|/*",
        )
        for i in range(binding_count)
    )
    result = HandlerClaudeCodeBindingRender().handle(
        ModelBindingRenderRequest(bindings=bindings, max_mode=mode)
    )
    _assert_loader_dollar_rule(result.module_text)


@pytest.mark.parametrize(
    "code",
    [
        "table($, ran)",
        "table(ran, $)",
        "table(...$)",
        "const table = ($, ran) => ran",
        "return $",
        "const alias = $",
        "$.ui.log($)",
        "$.ui.log('/*') // ignored\n table($, ran)",
    ],
)
def test_dollar_guard_rejects_passing_binding_spreading_returning_or_reading(
    code: str,
) -> None:
    with pytest.raises(AssertionError):
        _assert_loader_dollar_rule(code)
