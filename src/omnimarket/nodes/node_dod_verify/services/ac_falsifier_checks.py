# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Turn a ticket's accepted acceptance-criteria falsifiers into evidence checks.

OMN-20153. The quality-evidence audit of 2026-09-30 measured that 1 of 41
tickets carried a check written from its own acceptance criteria; every other
verdict rested on machine-made PR-exists (90), grep (91) and diff-derived test
runs (63). The author's falsifiers were already in the contract, transcribed by
``occ-autobind`` from the ticket's creation revision
(:mod:`omnimarket.occ_ac_transcription`), and bound to whichever item the
companion happened to carry. Nothing ran them.

This module is the pure half of the fix. It reads a loaded contract and returns
one ``dod_evidence`` item per ACCEPTED criterion whose falsifier names a test
selector, in the same dict shape the collector executes for a hand-authored
item. The collector runs them through the ordinary item path, so they are
classified, budgeted and receipted exactly like every other check, and a
failing or zero-test falsifier makes the verdict FAILED.

What it refuses to do, on purpose:

* **It never runs prose.** A falsifier that is a lab query, a readback or a
  sentence yields no item and is counted in ``unrunnable_labels``, so the gap is
  reported rather than papered over.
* **It never trusts a draft.** Only a label whose ``ac_bindings`` record carries
  ``accepted_by`` is read. A draft is a machine's proposal.
* **It never carries the author's text into a command.** The command is rebuilt
  from allowlisted tokens, so a falsifier cannot smuggle a shell metacharacter
  into the runner.
* **It never hides a missing test.** A selector whose path exists in no
  candidate clone is still minted, against the first candidate, so pytest exits
  non-zero and the item reads FAILED (the OMN-19533 class).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from omnimarket.nodes.node_dod_verify.models.model_ac_falsifier_command import (
    ModelAcFalsifierCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_acceptance_summary import (
    ModelDodAcceptanceSummary,
)

__all__ = [
    "DERIVED_ITEM_ID_PREFIX",
    "derive_falsifier_items",
    "parse_falsifier_command",
]

#: Prefix of every derived evidence id. The label is appended lowercased.
DERIVED_ITEM_ID_PREFIX: Final[str] = "ac-falsifier-"

_FALSIFIER_MARKER = re.compile(r"(?is).*(?:\bfalsifier\s*:|\bfalsified by\b)")
_PYTEST_HEAD = re.compile(r"(?:\buv run\s+)?\bpytest\b")
_REPO_HINT = re.compile(r"\bin (omni[a-z_]+|onex_change_control)\b")
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9_./-]+(::[A-Za-z0-9_\[\]-]+)?$")
_K_EXPR = re.compile(r"^[A-Za-z0-9_ ()]+$")
_TRAILING_PUNCT = "`,.;:)'\""
_SAFE_FLAGS: Final[frozenset[str]] = frozenset({"-q", "-v", "-vv", "-x"})
_EXPR_FLAGS: Final[frozenset[str]] = frozenset({"-k", "-m"})


def _strip(token: str) -> str:
    return token.strip("`").rstrip(_TRAILING_PUNCT).lstrip("`")


def _is_test_path(token: str) -> bool:
    if not _PATH_TOKEN.match(token):
        return False
    bare = token.split("::", 1)[0]
    if bare.startswith("/") or ".." in bare.split("/"):
        return False
    return "/" in bare or bare.endswith(".py")


def parse_falsifier_command(text: str) -> ModelAcFalsifierCommand | None:
    """The runnable ``uv run pytest`` selector a falsifier names, or None.

    Reads the first ``pytest`` in the text and consumes tokens while they are a
    test path, an allowlisted flag, or ``-k``/``-m`` with a plain expression.
    The first token that is none of those ends the selector, which is how
    ``... -v selects no test`` and ``... -q in omnibase_internal`` parse to the
    command without the prose after it. A selector with no test path is
    unrunnable: ``uv run pytest over the verifier tests`` names nothing.
    """
    head = _PYTEST_HEAD.search(text)
    if head is None:
        return None
    tokens = text[head.end() :].split()
    kept: list[str] = []
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        token = _strip(raw)
        if token in _SAFE_FLAGS:
            kept.append(token)
        elif token in _EXPR_FLAGS and index + 1 < len(tokens):
            value = tokens[index + 1]
            consumed = 1
            if value[:1] in "\"'" and not (len(value) > 1 and value[-1] == value[0]):
                closing = value[0]
                parts = [value]
                probe = index + 2
                while probe < len(tokens) and not parts[-1].endswith(closing):
                    parts.append(tokens[probe])
                    probe += 1
                value = " ".join(parts)
                consumed = probe - (index + 1)
            expr = value.strip("`").strip("\"'").rstrip(_TRAILING_PUNCT)
            if not _K_EXPR.match(expr):
                break
            kept.extend([token, expr if " " not in expr else f'"{expr}"'])
            index += consumed
        elif _is_test_path(token):
            paths.append(token)
            kept.append(token)
        else:
            break
        index += 1
        # a token that ended with sentence punctuation ends the selector
        if raw.rstrip("`") != token and raw.rstrip("`")[-1:] in ",.;:":
            break
    if not paths:
        return None
    hint = _REPO_HINT.search(text[head.end() :])
    return ModelAcFalsifierCommand(
        command="uv run pytest " + " ".join(kept),
        first_path=paths[0].split("::", 1)[0],
        repo_hint=hint.group(1) if hint else None,
    )


def _falsifier_text(statement: str) -> str | None:
    match = _FALSIFIER_MARKER.match(statement)
    if match is None:
        return None
    text = statement[match.end() :].strip()
    return text or None


def _accepted_labels(dod_items: Sequence[Any]) -> frozenset[str]:
    accepted: set[str] = set()
    for item in dod_items:
        if not isinstance(item, Mapping):
            continue
        records = item.get("ac_bindings")
        if not isinstance(records, list):
            continue
        for record in records:
            if (
                isinstance(record, Mapping)
                and record.get("accepted_by")
                and isinstance(record.get("label"), str)
            ):
                accepted.add(str(record["label"]))
    return frozenset(accepted)


def _declared_criteria(contract: Mapping[str, Any]) -> list[tuple[str, str]]:
    requirements = contract.get("requirements")
    if not isinstance(requirements, list):
        return []
    found: list[tuple[str, str]] = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        acceptance = requirement.get("acceptance")
        if not isinstance(acceptance, list):
            continue
        for criterion in acceptance:
            if (
                isinstance(criterion, Mapping)
                and isinstance(criterion.get("id"), str)
                and isinstance(criterion.get("statement"), str)
            ):
                found.append((criterion["id"], criterion["statement"]))
    return found


def derive_falsifier_items(
    contract: Mapping[str, Any],
    dod_items: Sequence[Any],
    *,
    repo_candidates: Sequence[str],
    path_exists: Callable[[str, str], bool],
) -> tuple[list[dict[str, Any]], ModelDodAcceptanceSummary]:
    """One executable evidence item per accepted, runnable criterion falsifier.

    ``repo_candidates`` are the repository directory names the contract's own
    PR-bound items name, in contract order. ``path_exists(repo, path)`` says
    whether a clone holds the selector's first path; the repo that holds it
    runs it, and when none does the first candidate runs it and fails visibly.
    """
    accepted = _accepted_labels(dod_items)
    items: list[dict[str, Any]] = []
    unrunnable: list[str] = []
    declared = 0
    for label, statement in _declared_criteria(contract):
        if label not in accepted:
            continue
        falsifier = _falsifier_text(statement)
        if falsifier is None:
            continue
        declared += 1
        parsed = parse_falsifier_command(falsifier)
        if parsed is None:
            unrunnable.append(label)
            continue
        # The repository the author named ('... in omnibase_internal') is tried
        # first, but only a repository whose clone actually holds the selector's
        # path can win on the strength of that name: a hint at a repository the
        # verifier cannot reach (one cloned outside $OMNI_HOME) must not turn a
        # reachable-only-by-the-author test into a spurious FAILED.
        searched = list(repo_candidates)
        if parsed.repo_hint is not None:
            searched = [
                parsed.repo_hint,
                *(r for r in searched if r != parsed.repo_hint),
            ]
        holder = next((r for r in searched if path_exists(r, parsed.first_path)), None)
        if holder is not None:
            repo = holder
        elif parsed.repo_hint is None and repo_candidates:
            # No hint and no clone holds the path: mint against the first
            # repository the contract names so the missing test FAILS visibly.
            repo = repo_candidates[0]
        else:
            unrunnable.append(label)
            continue
        item_id = f"{DERIVED_ITEM_ID_PREFIX}{label.lower()}"
        items.append(
            {
                "id": item_id,
                "description": (
                    f"{label} falsifier, run from the ticket's own accepted "
                    f"criterion: {parsed.command}"
                ),
                "source": "generated",
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": parsed.command,
                        "cwd": "${OMNI_HOME}/" + repo,
                    }
                ],
                "binds_ac": [label],
            }
        )
    summary = ModelDodAcceptanceSummary(
        declared_falsifier_count=declared,
        runnable_count=len(items),
        unrunnable_labels=tuple(unrunnable),
        derived_item_ids=tuple(str(item["id"]) for item in items),
    )
    return items, summary
