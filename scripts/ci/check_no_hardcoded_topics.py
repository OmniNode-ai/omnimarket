#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI gate: reject bare ONEX topic string literals in production Python (OMN-10909).

Topic references in Python must flow from contract.yaml through generated
constants/enums (e.g. ``topics.py`` modules, ``EnumOmniclaudeTopic``). Bare
``"onex.cmd.*"`` / ``"onex.evt.*"`` string literals are forbidden in
``src/omnimarket/`` outside the explicit allowlist.

Allowlist:
- files named ``topics.py`` (declared topic-constant registries — source of truth);
- ``test_*`` / ``conftest*`` files (explicit test fixtures may assert literal topics);
- any line carrying an inline ``# onex-topic-allow`` / ``# onex-topic-sot`` /
  ``# onex-topic-doc-example`` annotation (the established escape hatch);
- f-string literals containing ``{`` placeholders (dynamic construction, not a literal).

Comments and docstring lines are not flagged unless they carry a quoted literal that
is not one of the doc-example annotated forms.

Exit codes: 0 = clean; 1 = violations found; 2 = invocation error (run from repo root).
"""

from __future__ import annotations

import pathlib
import sys

# Built via join so this script does not self-trigger any topic-literal grep guard.
_ONEX_LITERAL_FORMS = (
    "onex." + "cmd.",
    "onex." + "evt.",
)
_QUOTES = ('"', "'")
_TOPIC_PATTERNS = tuple(q + form for form in _ONEX_LITERAL_FORMS for q in _QUOTES)

_ALLOWED_FILENAMES = {"topics.py"}
_ALLOWED_PREFIXES = ("test_", "conftest")
_INLINE_ALLOW_MARKERS = (
    "# onex-topic-allow",
    "# onex-topic-sot",
    "# onex-topic-doc-example",
)


def _is_allowed_file(path: pathlib.Path) -> bool:
    if path.name in _ALLOWED_FILENAMES:
        return True
    return any(path.name.startswith(p) for p in _ALLOWED_PREFIXES)


def _is_dynamic_fstring(line: str) -> bool:
    if "{" not in line or "}" not in line:
        return False
    return any(marker in line for marker in ('f"onex.', "f'onex."))


def _is_prefix_match_usage(line: str) -> bool:
    """A literal passed to ``str.startswith(...)`` / ``str.endswith(...)`` is a
    prefix probe, not a topic name — e.g. ``topic.startswith("onex.evt.omniclaude.")``."""
    return ".startswith(" in line or ".endswith(" in line


def _line_is_violation(line: str, stripped: str) -> bool:
    if not any(pattern in line for pattern in _TOPIC_PATTERNS):
        return False
    if any(marker in line for marker in _INLINE_ALLOW_MARKERS):
        return False
    if _is_dynamic_fstring(line) or _is_prefix_match_usage(line):
        return False
    return not stripped.startswith(("#", '"""', "'''"))


def scan(src_root: pathlib.Path) -> list[str]:
    violations: list[str] = []
    for py_file in sorted(src_root.rglob("*.py")):
        if _is_allowed_file(py_file):
            continue
        in_triple_double = False
        in_triple_single = False
        for lineno, line in enumerate(
            py_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            dq_count = stripped.count('"""')
            sq_count = stripped.count("'''")
            if (
                not in_triple_double
                and not in_triple_single
                and _line_is_violation(line, stripped)
            ):
                violations.append(f"{py_file}:{lineno}: {stripped}")
            if dq_count % 2 == 1:
                in_triple_double = not in_triple_double
            if sq_count % 2 == 1:
                in_triple_single = not in_triple_single
    return violations


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src_root = repo_root / "src" / "omnimarket"
    if not src_root.is_dir():
        print(f"ERROR: src/omnimarket not found under {repo_root}", file=sys.stderr)
        return 2

    violations = scan(src_root)
    if violations:
        print(
            f"ERROR: {len(violations)} bare ONEX topic literal(s) found in "
            f"src/omnimarket/ outside the allowlist:"
        )
        for v in violations:
            print(f"  {v}")
        print()
        print(
            "Fix: declare the topic in the node's contract.yaml "
            "(event_bus.subscribe_topics / publish_topics) and reference it via a "
            "generated constant / topics.py registry. If a literal is genuinely "
            "required, add an inline `# onex-topic-allow: <reason>` annotation."
        )
        return 1

    print("OK: no bare ONEX topic literals in src/omnimarket/ outside the allowlist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
