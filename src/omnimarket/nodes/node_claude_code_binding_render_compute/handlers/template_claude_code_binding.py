# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The mod-module template (OMN-18531, T0.3).

The template holds NO branching of its own: the only decisions in the rendered text
are table lookups (the plan/answer tables and the fixed fallback table).
Every value from a contract is substituted as a JSON string/number literal by the
handler, never as code. The template lives with the render node, not in the mods
location: the rendered module is written at install and tracked nowhere.
"""

from __future__ import annotations

MODULE_HEADER = """\
// Rendered by node_claude_code_binding_render_compute from client_bindings.claude_code.
// Host maximum mode: @@max_mode@@. Generated; never hand-edited; not tracked in any repository.
import type { Register } from 'claude-code'

const KIND = { true: 'never_started', false: 'other' }
const FAILED = 'onex hook adapter did not answer: '
const FALLBACK = {
  @@never_started_line@@,
  @@other_line@@,
}
const ANSWER = {
  native: (ran, fall) => fall(),
  output: (ran, fall) => JSON.parse(ran.stdout),
@@deny_line@@}
const OUTPUT = ['native', 'output']
const PLAN = {
  true: ran => ({ ran, lines: [], answer: OUTPUT[Math.min(ran.stdout.length, 1)] }),
  false: ran => ({ ran, lines: [FAILED + 'exit ' + ran.exitCode], answer: FALLBACK.other }),
}
"""

BINDING_CONSTANTS = """\
const ARGV_@@name@@ = @@argv@@
const PATTERN_@@name@@ = new RegExp(@@pattern@@)
const DEADLINE_@@name@@ = @@deadline_ms@@
"""

REGISTER_OPEN = """\

export const register: Register = on => {
"""

BINDING_HOOK = """\
  on(@@event@@, @@matcher@@, async ($, e, next) => {
    const plan = await $.process.run(ARGV_@@name@@, { stdin: JSON.stringify({ ...e, session_id: await $.session.id(), cwd: await $.session.cwd() }), timeoutMs: DEADLINE_@@name@@ }).then(
      ran => PLAN[String(ran.exitCode === 0)](ran),
      err => ({ ran: undefined, lines: [FAILED + String(err)], answer: FALLBACK[KIND[String(PATTERN_@@name@@.test(String(err)))]] }),
    )
    plan.lines.forEach(line => $.ui.log(line))
    return ANSWER[plan.answer](plan.ran, () => next(e))
  }).catch(($, e, next) => ($.ui.log(FAILED + 'hook threw'), ANSWER[FALLBACK.other](undefined, () => next(e))))
"""

REGISTER_CLOSE = """\
}
"""

# OMN-21099: tables never receive the loader's $ interface. The hook logs each
# planned failure line before resolving the answer through its native closure.
REQUIRED_NEVER_STARTED = "never_started: 'deny'"
REQUIRED_OTHER = "other: 'deny'"
NATIVE_NEVER_STARTED = "never_started: 'native'"
NATIVE_OTHER = "other: 'native'"
DENY_ANSWER = "  deny: (ran, fall) => ({ deny: @@deny_text@@ }),\n"
