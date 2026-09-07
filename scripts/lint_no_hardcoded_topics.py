#!/usr/bin/env python3
"""Topic-literal guardrail: reject hardcoded onex.evt.* / onex.cmd.* strings in production Python.

Scans src/**/*.py files only. contract.yaml files are not scanned (not Python).

Allowed:
- Test files (test_*, conftest*) and the handler/model/CLI transition prefixes below;
- ``topics.py`` / ``structured_logger.py`` topic-constant registries;
- any line carrying an inline ``# onex-topic-allow`` / ``# onex-topic-sot`` /
  ``# onex-topic-doc-example`` annotation with a reason (the established escape hatch);
- f-string literals containing ``{`` placeholders (dynamic construction, not a literal).

Forbidden:
- topics.py files in node directories (deleted — contract.yaml is now the source of truth)
- Any new ad-hoc topic string outside the established handler pattern

Why this file honours the inline annotation (OMN-18013)
------------------------------------------------------
This script and its stricter sibling ``scripts/ci/check_no_hardcoded_topics.py`` scan the
same corpus in the same CI job (ci.yml "Topic-literal guardrail" and "... (annotation
allowlist)"), but they used to disagree about what is allowed: the sibling honours a
per-line ``# onex-topic-allow: <reason>`` annotation, while this one knew only whole-file
basenames. The pre-commit config papered over the gap with an ``exclude:`` regex, but CI
invokes this script directly over ``src/`` and never consults that regex — so the same
bytes were green locally and red in CI. That divergence is a defect in the gate, not in
the code it scans, and closing it is what this module now does.

Aligning on the annotation is a NARROWING, not a loosening: two whole-file allowances
(``no_baseline_refreeze.py``, ``no_literal_event_type_in_tests.py``) are gone from
``ALLOWED_FILES`` and replaced by the per-line, reason-bearing annotations those files
already carry on every literal, and ``contract_topic_graph.py`` needs no entry at all.

NOT in scope, deliberately: the third-party ``no-hardcoded-topics`` pre-commit hook is an
external repo with no annotation support of any kind, so its ``exclude:`` entries for
those three files must stay — they are the only mechanism that hook offers. What keeps
that whole-file exclusion honest is ``tests/ci/test_lint_no_hardcoded_topics_parity.py``,
which pins that EVERY topic literal inside those files still carries its own reason, so
the exclusion cannot come to hide an unreasoned one.

NOTE: This is a line-grep guardrail, not full semantic enforcement. It detects
"onex.evt." and "onex.cmd." string literals in non-exempt production Python.
The sibling gate does the AST/tokenizer-accurate pass; this one is the fast fence.

Exit codes: 0 = clean; 1 = violations found; 2 = invocation error (run from repo root).
"""

from __future__ import annotations

import pathlib
import sys

# Files allowed to contain onex.evt.* / onex.cmd.* literals.
# NOTE: node-level topics.py is intentionally NOT exempt via this set — those files were
# deleted and contract.yaml is the source of truth. The name is kept for the platform-wide
# logging registry only.
ALLOWED_FILES = {
    # Platform-wide log-entry topic constant (shared utility, not a handler)
    "structured_logger.py",
    # logging/topics.py: platform-wide log-entry topic registry (not a node topics.py).
    # Node-level topics.py files in src/omnimarket/nodes/node_*/ are banned — see CI gate.
    "topics.py",
}

# Handler, config model, and CLI entry point files are allowed to declare inline
# topic string constants as module-level variables. This is the transition state
# after topics.py deletion — handlers own their own topic constants until
# runtime contract auto-wiring is complete.
ALLOWED_PREFIXES = (
    "test_",
    "conftest",
    "handler_",
    "model_",
    "overseer_tick",
    "__main__",
)

# The established per-line escape hatch. Kept byte-identical to the sibling gate's
# markers (scripts/ci/check_no_hardcoded_topics.py::_INLINE_ALLOW_MARKERS) — if the two
# lists ever drift, the parity test fails.
INLINE_ALLOW_MARKERS = (
    "# onex-topic-allow",
    "# onex-topic-sot",
    "# onex-topic-doc-example",
)

# Topic literal patterns to detect (more precise than bare "onex.")
# Built via join to avoid self-triggering the no-hardcoded-topics hook,
# which rejects quoted onex.evt.* / onex.cmd.* literals in non-approved files.
_ONEX_PREFIXES = ["onex", "evt", ""], ["onex", "cmd", ""]
TOPIC_PATTERNS = tuple(
    q + ".".join(parts) for parts in _ONEX_PREFIXES for q in ('"', "'")
)


def _is_allowed_file(path: pathlib.Path) -> bool:
    if path.name in ALLOWED_FILES:
        return True
    return any(path.name.startswith(p) for p in ALLOWED_PREFIXES)


def scan(src_root: pathlib.Path) -> list[str]:
    """Return one ``path:line: text`` string per violating line, in path order."""
    violations: list[str] = []

    for py_file in sorted(src_root.rglob("*.py")):
        if _is_allowed_file(py_file):
            continue

        lines = py_file.read_text(encoding="utf-8").splitlines()

        # Track whether we are inside a multi-line docstring / triple-quoted string.
        in_triple_double = False
        in_triple_single = False

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # A line with an odd number of """ toggles the docstring state.
            dq_count = stripped.count('"""')
            sq_count = stripped.count("'''")

            # Check for violations before updating triple-quote state,
            # but skip if currently inside a multi-line string.
            if (
                not in_triple_double
                and not in_triple_single
                and _line_is_violation(line, stripped)
            ):
                violations.append(f"{py_file}:{i}: {stripped}")

            # Update triple-quote state after processing the line.
            if dq_count % 2 == 1:
                in_triple_double = not in_triple_double
            if sq_count % 2 == 1:
                in_triple_single = not in_triple_single

    return violations


def _line_is_violation(line: str, stripped: str) -> bool:
    if not any(p in line for p in TOPIC_PATTERNS):
        return False
    # The established escape hatch, honoured identically by the sibling gate.
    if any(marker in line for marker in INLINE_ALLOW_MARKERS):
        return False
    # Skip f-strings with format placeholders — dynamic construction,
    # not a literal topic (e.g. f"onex.evt.omnimarket.{keyword}.v1").
    is_fstring_dynamic = (
        "{" in line and "}" in line and any(f in line for f in ('f"onex.', "f'onex."))
    )
    if is_fstring_dynamic:
        return False
    return not stripped.startswith(("#", '"""', "'''"))


def main() -> int:
    src_root = pathlib.Path("src")
    if not src_root.is_dir():
        print("ERROR: Run this script from the omnimarket repo root (src/ not found)")
        return 2

    violations = scan(src_root)
    if violations:
        print(
            f"ERROR: {len(violations)} hardcoded topic literal(s) found in production Python:"
        )
        for v in violations:
            print(f"  {v}")
        print()
        print(
            "Fix: move topic strings into contract.yaml event_bus.subscribe_topics / publish_topics\n"
            "and read them via contract loader at runtime.\n"
            "If a literal is genuinely required, add an inline `# onex-topic-allow: <reason>`\n"
            "annotation — the same escape hatch the sibling gate honours.\n"
            "See: docs/plans/2026-04-08-contract-first-enforcement.md"
        )
        return 1

    print("OK: No hardcoded topic literals found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
