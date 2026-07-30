#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-repo criterion-1 falsifier: exactly one hold vocabulary (OMN-15484).

What this is for
----------------
OMN-15483 shipped the merge-hold gate in ``omnimarket`` only. OMN-15484 fans it
out to the repos where every incident in that ticket's table actually happened
(``onex_change_control``, ``omnibase_infra``). The obvious way to fan out — copy
``hold_marker.py`` into each repo — rebuilds, at fleet scale, the exact bug
OMN-15483 round 1 found *inside* omnimarket: two divergent ``_DO_NOT_MERGE_RE``
definitions (``occ_companion_emitter.py:163`` matched
``DO NOT MERGE``/``WORK IN PROGRESS``/``[WIP]``; ``handler_occ_companion_compute.py:110``
matched ``do not merge``/``DNM``/``WIP``/``[draft``), neither a superset of the
other, so the same PR was suppressed by one consumer and authored by the other.

OMN-15484 AC1 therefore rejects "vendored copy plus a sync test" outright and
asks for a scan that FAILS when a second hold vocabulary is declared in an
adopting repo, **run as a check on each adopting repo**. This script is that
scan. The adopting repo does not host it: the reusable workflow
``.github/workflows/merge-hold-gate-reusable.yml`` checks the adopting repo out
and runs *this* file against it, so there is one authored copy with N callers.

How "a second vocabulary" is detected without declaring one
-----------------------------------------------------------
A detector carrying its own token list would itself be a second vocabulary — the
thing it exists to forbid. So every token this script matches on is **derived at
runtime from the canonical module**:

1. **Literal skeletons.** ``HOLD_MARKER_RE.pattern`` is split on its top-level
   ``|`` alternation and each alternative is reduced to its letter/digit
   skeleton (``do[\\s_-]?not[\\s_-]?merge`` -> ``donotmerge``, ``\\bWIP\\b`` ->
   ``wip``, ``\\[\\s*draft`` -> ``draft``). Every ``re.compile(...)`` literal in
   the scanned tree is reduced the same way; a candidate whose skeleton contains
   a canonical skeleton is a re-declaration however differently it is spelled.
   This survives separator/metacharacter noise, which a plain string compare
   does not: a vendored copy that swaps ``[\\s_-]?`` for ``[ _-]*`` still
   reduces to ``donotmerge``.
2. **Identifier names.** The canonical module's own exported regex names
   (``HOLD_MARKER_RE``, ``DO_NOT_MERGE_RE``) are reduced to name fragments, and
   any assignment target in the scanned tree that carries one of those fragments
   and binds a ``re.compile`` is an offender even if its pattern is empty today.
   This is the rule the in-repo omnimarket falsifier
   (``tests/test_merge_hold_marker_omn15483.py::test_only_one_module_declares_a_hold_regex``)
   already applies; keeping both rules means neither an empty-but-named copy nor
   an anonymous-but-equivalent copy slips through.

Both rules read the canonical module. Delete or break it and this script FAILS
(``CanonicalVocabularyUnavailableError``) rather than reporting "no offenders" —
an unloadable vocabulary must never look like a clean scan.

Stdlib only, by the same constraint as the gate itself: the reusable workflow
runs a bare ``python3`` with no ``uv sync``, so a dependency failure can never
skip this check.

Exit codes: ``0`` exactly one vocabulary, ``1`` a second one was found or the
canonical vocabulary could not be loaded.

Related: OMN-15484 (this fan-out), OMN-15483 (the gate), OMN-14741 F-17.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.ci.check_pr_hold_marker import (
    CANONICAL_HOLD_MODULE,
    CanonicalVocabularyUnavailableError,
    load_canonical_hold_module,
)

EXIT_OK = 0
EXIT_OFFENDER = 1

# Directory names never worth scanning: build/VCS noise, and vendored trees that
# are not the adopting repo's own source.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        ".tox",
        "build",
        "dist",
    }
)

_NON_WORD = re.compile(r"[^0-9a-z]+")

# A single SHORT canonical token is not evidence of a re-declaration. ``wip``,
# ``dnm`` and ``draft`` are ordinary words that appear in perfectly innocent
# regexes (a draft-PR parser, a WIP-status enum), and flagging those would wedge
# an adopting repo on a false positive — the one outcome worse than a missing
# gate, because it teaches people to disable the check. A genuine vendored
# vocabulary is recognisable instead by reproducing the DISTINCTIVE multi-word
# tokens (``donotmerge``, ``workinprogress``, ``verificationhold``), or by
# carrying several tokens at once. So a candidate is an offender when it
# contains a long canonical token, OR at least two distinct canonical tokens.
_DISTINCTIVE_TOKEN_LENGTH = 8
_MULTI_TOKEN_THRESHOLD = 2


def literal_skeleton(text: str) -> str:
    """Reduce a regex fragment to its lowercase letter/digit skeleton.

    Regex metacharacters, escapes, separators and character classes all vanish,
    so two spellings of the same token collapse to the same string:

    >>> literal_skeleton(r"do[\\s_-]?not[\\s_-]?merge")
    'donotmerge'
    >>> literal_skeleton(r"do[ _-]*NOT[ _-]*merge")
    'donotmerge'
    >>> literal_skeleton(r"\\bWIP\\b")
    'wip'

    Args:
        text: A regex fragment (or any string).

    Returns:
        The skeleton: lowercase, alphanumerics only.
    """
    # Drop escape backslashes first so ``\b`` does not leave a stray ``b``.
    without_escapes = re.sub(r"\\[A-Za-z]", " ", text)
    without_escapes = without_escapes.replace("\\", " ")
    return _NON_WORD.sub("", without_escapes.lower())


def split_alternation(pattern: str) -> list[str]:
    """Split a regex on its TOP-LEVEL ``|`` alternation.

    ``|`` inside a character class or a group belongs to that construct, not to
    the outer alternation, so a naive ``pattern.split("|")`` would shred a
    grouped vocabulary into meaningless fragments.

    Args:
        pattern: The regex source.

    Returns:
        The top-level alternatives, in order.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_class = False
    escaped = False
    for char in pattern:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if in_class:
            current.append(char)
            if char == "]":
                in_class = False
            continue
        if char == "[":
            in_class = True
            current.append(char)
            continue
        if char == "(":
            depth += 1
            current.append(char)
            continue
        if char == ")":
            depth = max(0, depth - 1)
            current.append(char)
            continue
        if char == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def canonical_skeletons(pattern: str) -> tuple[str, ...]:
    """The canonical vocabulary's alternatives, as skeletons.

    Args:
        pattern: ``HOLD_MARKER_RE.pattern`` from the canonical module.

    Returns:
        Non-empty skeletons, deduplicated, longest first so an offender is
        reported against the most specific token it re-declares.

    Raises:
        CanonicalVocabularyUnavailableError: If the canonical pattern reduces to
            nothing at all — a vocabulary with no literal content cannot be used
            to detect a copy of itself, and silently scanning for nothing would
            be a vacuous green.
    """
    seen: dict[str, None] = {}
    for alternative in split_alternation(pattern):
        skeleton = literal_skeleton(alternative)
        if skeleton:
            seen[skeleton] = None
    if not seen:
        raise CanonicalVocabularyUnavailableError(
            "the canonical hold vocabulary has no literal content to scan for "
            f"(pattern={pattern!r}) — refusing to report a clean scan"
        )
    return tuple(sorted(seen, key=len, reverse=True))


def canonical_name_fragments(module_names: Sequence[str]) -> tuple[str, ...]:
    """Identifier fragments derived from the canonical module's regex exports.

    ``HOLD_MARKER_RE`` -> ``HOLD_MARKER``; ``DO_NOT_MERGE_RE`` -> ``DO_NOT_MERGE``.

    Args:
        module_names: ``dir(canonical_module)`` or its ``__all__``.

    Returns:
        Uppercase fragments, longest first.
    """
    fragments: dict[str, None] = {}
    for name in module_names:
        if name.endswith("_RE") and name.isupper():
            fragments[name[: -len("_RE")]] = None
    return tuple(sorted(fragments, key=len, reverse=True))


def redeclared_tokens(
    candidate_skeleton: str,
    skeletons: Sequence[str],
) -> tuple[str, ...]:
    """Which canonical tokens a candidate pattern re-declares, if any.

    See :data:`_DISTINCTIVE_TOKEN_LENGTH` for why a single short token is not
    enough. Returns an empty tuple for the innocent cases so the caller can
    treat "any hits" as the offender predicate.

    Args:
        candidate_skeleton: :func:`literal_skeleton` of a found ``re.compile``.
        skeletons: The canonical token skeletons.

    Returns:
        The matched canonical tokens when the candidate qualifies as a
        re-declaration, else ``()``.
    """
    if not candidate_skeleton:
        return ()
    matched = tuple(s for s in skeletons if s and s in candidate_skeleton)
    if not matched:
        return ()
    if any(len(s) >= _DISTINCTIVE_TOKEN_LENGTH for s in matched):
        return matched
    if len(set(matched)) >= _MULTI_TOKEN_THRESHOLD:
        return matched
    return ()


def _iter_python_files(root: Path) -> list[Path]:
    """Every ``*.py`` under ``root``, skipping build/VCS/vendor noise."""
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


def scan_tree(
    *,
    tree_root: Path,
    scan_roots: Sequence[str],
    skeletons: Sequence[str],
    name_fragments: Sequence[str],
    canonical_module_path: Path,
) -> tuple[list[str], list[str]]:
    """Find every second declaration of the hold vocabulary under ``tree_root``.

    Args:
        tree_root: Root of the repository being scanned (the *adopting* repo
            when called from the reusable workflow).
        scan_roots: Relative directories to walk. Missing ones are skipped —
            not every repo has ``src/``.
        skeletons: Canonical literal skeletons (see :func:`canonical_skeletons`).
        name_fragments: Canonical identifier fragments.
        canonical_module_path: The one file allowed to declare the vocabulary.
            Exempted so scanning omnimarket itself does not flag the original.

    Returns:
        ``(offenders, unscannable)`` — re-declarations found, and files that
        could not be cleared. Both empty means the tree is provably clean.
    """
    offenders: list[str] = []
    unscannable: list[str] = []
    canonical_resolved = canonical_module_path.resolve()
    # The exemption must ALSO be relative, not just absolute. When omnimarket
    # calls this gate on itself, the caller checkout and the vocabulary checkout
    # are two different directories holding the same file, so an absolute-path
    # exemption alone would flag omnimarket's own canonical module as a second
    # vocabulary — the gate refusing the very repo that defines it.
    canonical_relative = _canonical_relative_path(canonical_resolved)

    for relative in scan_roots:
        root = tree_root / relative
        if not root.is_dir():
            continue
        for path in _iter_python_files(root):
            resolved = path.resolve()
            if resolved == canonical_resolved:
                continue
            if (
                canonical_relative is not None
                and _relative_or_none(resolved, tree_root) == canonical_relative
            ):
                continue
            display = _relative_or_none(resolved, tree_root) or path
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # Unreadable source is not evidence of cleanliness. Recorded as
                # UNSCANNABLE, not as an offender: the two have different causes
                # and different fixes, and conflating them makes the failure
                # message accuse the repo of a vocabulary violation it does not
                # have. Both still fail the check.
                unscannable.append(f"{display}: unreadable ({exc})")
                continue
            try:
                parsed = ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                # Same posture. In practice the overwhelmingly likely cause is
                # an interpreter older than the source (PEP 695 generics parse
                # only on 3.12+), which is why the report prints the running
                # interpreter alongside — a version mismatch must be diagnosable
                # at a glance instead of looking like a fleet-wide violation.
                unscannable.append(f"{display}: unparseable ({exc})")
                continue
            offenders.extend(
                _offenders_in_module(
                    parsed=parsed,
                    path=path,
                    tree_root=tree_root,
                    skeletons=skeletons,
                    name_fragments=name_fragments,
                )
            )
    return offenders, unscannable


def _canonical_relative_path(canonical_resolved: Path) -> Path | None:
    """The canonical module's path relative to ITS OWN repo root.

    ``.../<root>/src/omnimarket/merge_control/hold_marker.py`` ->
    ``src/omnimarket/merge_control/hold_marker.py``. Derived from the file's own
    location (``scripts/ci/<this>.py`` puts the root at ``parents[2]``) rather
    than hardcoded, so moving the module inside omnimarket cannot leave a stale
    exemption behind.

    Args:
        canonical_resolved: Absolute path to the canonical module.

    Returns:
        The relative path, or ``None`` when it does not sit under the expected
        root (in which case only the absolute exemption applies).
    """
    own_root = Path(__file__).resolve().parents[2]
    return _relative_or_none(canonical_resolved, own_root)


def _relative_or_none(path: Path, root: Path) -> Path | None:
    """``path`` relative to ``root``, or ``None`` when it is not underneath."""
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _offenders_in_module(
    *,
    parsed: ast.Module,
    path: Path,
    tree_root: Path,
    skeletons: Sequence[str],
    name_fragments: Sequence[str],
) -> list[str]:
    """Offenders inside a single parsed module (see :func:`scan_tree`)."""
    offenders: list[str] = []
    try:
        display = path.relative_to(tree_root)
    except ValueError:  # pragma: no cover - defensive
        display = path

    for node in ast.walk(parsed):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_re_compile = (
            isinstance(func, ast.Attribute)
            and func.attr == "compile"
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
        )
        if not is_re_compile:
            continue

        pattern_text = _joined_string_arg(node)
        candidate = literal_skeleton(pattern_text) if pattern_text else ""
        hits = redeclared_tokens(candidate, skeletons)
        if hits:
            listed = ", ".join(repr(h) for h in hits)
            offenders.append(
                f"{display}:{node.lineno}: re.compile(...) re-declares the "
                f"canonical hold vocabulary ({listed})"
            )
            continue

        # Rule 2: named like the canonical export, bound to a re.compile.
        for target_name in _assignment_targets(parsed, node):
            normalized = target_name.lstrip("_").upper()
            named = next((f for f in name_fragments if f and f in normalized), None)
            if named is not None:
                offenders.append(
                    f"{display}:{node.lineno}: {target_name} binds a re.compile "
                    f"named after the canonical vocabulary ({named!r})"
                )
                break
    return offenders


def _joined_string_arg(call: ast.Call) -> str:
    """Concatenate the string parts of a ``re.compile`` first argument.

    Handles the common shapes: a plain literal, implicit adjacent-literal
    concatenation (already folded by the parser), and explicit ``a + b``.
    Anything dynamic yields ``""``, which falls through to the name rule.
    """
    if not call.args:
        return ""
    return _string_of(call.args[0])


def _string_of(node: ast.expr) -> str:
    """Best-effort static string value of ``node`` (empty when not static)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_of(node.left) + _string_of(node.right)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            _string_of(value)
            for value in node.values
            if isinstance(value, ast.Constant | ast.BinOp | ast.JoinedStr)
        )
    return ""


def _assignment_targets(parsed: ast.Module, call: ast.Call) -> list[str]:
    """Names assigned from ``call``, if it is the RHS of an assignment."""
    names: list[str] = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.Assign) and node.value is call:
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is call
            and isinstance(node.target, ast.Name)
        ):
            names.append(node.target.id)
    return names


def run(
    *,
    tree_root: Path,
    scan_roots: Sequence[str],
    canonical_module_path: Path = CANONICAL_HOLD_MODULE,
) -> tuple[int, str]:
    """Scan ``tree_root`` and render the verdict.

    Args:
        tree_root: Repository root to scan.
        scan_roots: Relative directories inside it to walk.
        canonical_module_path: The canonical vocabulary module.

    Returns:
        ``(exit_code, report)``.
    """
    try:
        canonical = load_canonical_hold_module(canonical_module_path)
        skeletons = canonical_skeletons(canonical.HOLD_MARKER_RE.pattern)
    except CanonicalVocabularyUnavailableError as exc:
        return EXIT_OFFENDER, f"FAIL (fail-closed): {exc}"

    fragments = canonical_name_fragments(
        getattr(canonical, "__all__", None) or dir(canonical)
    )

    offenders, unscannable = scan_tree(
        tree_root=tree_root,
        scan_roots=scan_roots,
        skeletons=skeletons,
        name_fragments=fragments,
        canonical_module_path=canonical_module_path,
    )

    scanned = ", ".join(scan_roots) or "(nothing)"
    interpreter = f"python {sys.version.split()[0]} at {sys.executable}"

    if offenders:
        listed = "\n".join(f"  - {o}" for o in offenders)
        return EXIT_OFFENDER, (
            "FAIL — a SECOND merge-hold vocabulary is declared in this "
            f"repository (OMN-15484 AC1 falsifier):\n{listed}\n"
            "\n"
            "The hold vocabulary is declared exactly once, fleet-wide, in "
            "omnimarket's src/omnimarket/merge_control/hold_marker.py, and this "
            "repository's gate reads THAT module through the shared reusable "
            "workflow. A local copy is how OMN-15483 got two divergent "
            "definitions inside one repo; at fleet scale it is the same bug "
            "with more copies. Delete the copy and call the shared gate.\n"
            f"\n  scanned: {tree_root} [{scanned}]"
        )

    if unscannable:
        listed = "\n".join(f"  - {u}" for u in unscannable)
        return EXIT_OFFENDER, (
            "FAIL (fail-closed) — the scan could not CLEAR every file, so it "
            "cannot report that exactly one vocabulary exists. This is NOT a "
            f"vocabulary violation:\n{listed}\n"
            "\n"
            "Most likely cause: the interpreter is older than the source it is "
            "parsing (PEP 695 generics, `type` statements and match patterns "
            "all parse only on new enough Pythons). Check the interpreter "
            "before touching any of the listed files.\n"
            f"\n  interpreter: {interpreter}"
            f"\n  scanned: {tree_root} [{scanned}]"
        )

    return EXIT_OK, (
        "PASS — exactly one merge-hold vocabulary.\n"
        f"  scanned: {tree_root} [{scanned}]\n"
        f"  canonical tokens derived from: {canonical_module_path}\n"
        f"  token skeletons: {', '.join(skeletons)}\n"
        f"  interpreter: {interpreter}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 clean, 1 a second vocabulary or an unloadable one.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Fail when a repository declares a SECOND merge-hold vocabulary "
            "(OMN-15484 AC1). Tokens are derived from the canonical module at "
            "runtime; this scanner declares none of its own."
        )
    )
    parser.add_argument(
        "--tree",
        type=Path,
        required=True,
        help="Repository root to scan (the adopting repo's checkout).",
    )
    parser.add_argument(
        "--roots",
        default="src scripts",
        help=(
            "Space-separated directories inside --tree to walk. Missing "
            "directories are skipped. Default: 'src scripts'."
        ),
    )
    parser.add_argument(
        "--module-path",
        type=Path,
        default=CANONICAL_HOLD_MODULE,
        help="Path to the canonical hold_marker.py (default: the in-repo module).",
    )
    args = parser.parse_args(argv)

    code, report = run(
        tree_root=args.tree,
        scan_roots=[r for r in str(args.roots).split() if r],
        canonical_module_path=args.module_path,
    )
    if code == EXIT_OK:
        print(report)
    else:
        print(report, file=sys.stderr)
        print(
            f"::error::Single Hold Vocabulary: {report.splitlines()[0]}",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
