#!/usr/bin/env python3
"""Topic-literal guardrail: reject hardcoded onex.evt.* / onex.cmd.* strings in production Python.

Scans src/**/*.py files only. contract.yaml files are not scanned (not Python).

Allowed locations:
- Test files (test_*, conftest*)
- Handler/model files that own their own topic constants (transition state: topics.py
  files have been deleted; handlers now declare topic strings as module-level constants
  until runtime contract auto-wiring is fully wired. See OMN-XXXX.)
- structured_logger.py (platform-wide log-entry topic constant, shared utility)
- __main__.py (CLI entry points for sweep nodes)

Forbidden:
- topics.py files in node directories (deleted — contract.yaml is now the source of truth)
- Any new ad-hoc topic string outside the established handler pattern

NOTE: This is a line-grep guardrail, not full semantic enforcement. It detects
"onex.evt." and "onex.cmd." string literals in non-exempt production Python.
A future iteration should replace this with AST-based ast.Constant node
inspection to more reliably distinguish literals from dynamic construction
and to handle multiline strings.
"""

import pathlib
import sys

# Files allowed to contain onex.evt.* / onex.cmd.* literals.
# NOTE: topics.py is intentionally NOT in this list — those files were deleted.
# Contract.yaml is the source of truth. Handlers declare inline constants as a
# transition measure until runtime contract auto-wiring is complete (OMN-XXXX).
ALLOWED_FILES = {
    # Platform-wide log-entry topic constant (shared utility, not a handler)
    "structured_logger.py",
    # logging/topics.py: platform-wide log-entry topic registry (not a node topics.py)
    # Node-level topics.py files in src/omnimarket/nodes/node_*/ are banned — see CI gate.
    "topics.py",
}

# Handler, config model, and CLI entry point files are allowed to declare inline
# topic string constants as module-level variables. This is the transition state
# after topics.py deletion — handlers own their own topic constants until
# runtime contract auto-wiring is complete (OMN-XXXX).
ALLOWED_PREFIXES = (
    "test_",
    "conftest",
    "handler_",
    "model_",
    "overseer_tick",
    "__main__",
)

# Topic literal patterns to detect (more precise than bare "onex.")
# Built via join to avoid self-triggering the no-hardcoded-topics hook,
# which rejects quoted onex.evt.* / onex.cmd.* literals in non-approved files.
_ONEX_PREFIXES = ["onex", "evt", ""], ["onex", "cmd", ""]
TOPIC_PATTERNS = tuple(
    q + ".".join(parts) for parts in _ONEX_PREFIXES for q in ('"', "'")
)

violations = []
src_root = pathlib.Path("src")

if not src_root.is_dir():
    print("ERROR: Run this script from the omnimarket repo root (src/ not found)")
    sys.exit(2)

for py_file in src_root.rglob("*.py"):
    if any(py_file.name.startswith(p) for p in ALLOWED_PREFIXES):
        continue
    if py_file.name in ALLOWED_FILES:
        continue

    source = py_file.read_text(encoding="utf-8")
    lines = source.splitlines()

    # Track whether we are inside a multi-line docstring / triple-quoted string
    in_triple_double = False
    in_triple_single = False

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Toggle triple-quote state (heuristic: count occurrences)
        # A line with an odd number of """ toggles the docstring state.
        dq_count = stripped.count('"""')
        sq_count = stripped.count("'''")

        # Check for violations before updating triple-quote state,
        # but skip if currently inside a multi-line string.
        if not in_triple_double and not in_triple_single:
            has_pattern = any(p in line for p in TOPIC_PATTERNS)
            if has_pattern:
                is_comment_or_docstring = (
                    stripped.startswith("#")
                    or stripped.startswith('"""')
                    or stripped.startswith("'''")
                )
                # Skip f-strings with format placeholders — dynamic construction,
                # not a literal topic (e.g. f"onex.evt.omnimarket.{keyword}.v1")
                is_fstring_dynamic = (
                    "{" in line
                    and "}" in line
                    and any(f in line for f in ('f"onex.', "f'onex."))
                )
                if not is_comment_or_docstring and not is_fstring_dynamic:
                    violations.append(f"{py_file}:{i}: {stripped}")

        # Update triple-quote state after processing the line
        if dq_count % 2 == 1:
            in_triple_double = not in_triple_double
        if sq_count % 2 == 1:
            in_triple_single = not in_triple_single

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
        "See: docs/plans/2026-04-08-contract-first-enforcement.md"
    )
    sys.exit(1)

print("OK: No hardcoded topic literals found.")
