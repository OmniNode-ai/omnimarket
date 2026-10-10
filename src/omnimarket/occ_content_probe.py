# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared, pure content-bound DoD probe derivation (OMN-15247 slice 1).

Every function here is the ONE definition of its shape (memory
``feedback_one_canonical_model_per_shape``). ``SymbolCandidate``,
:func:`extract_symbol_candidates`, :func:`declaration_count`,
:func:`build_content_read_check` and :func:`select_asserted_check` were built
under **OMN-14619** inside ``node_occ_state_effect``'s private handler package;
they are MOVED here (not copied) so the born-path producer
(``OccCompanionEmitter``) can use them without importing another node's private
handler module — the repo rule that produced ``occ_git_transport`` (OMN-14622)
and ``events/occ_autoauthor`` (OMN-14393). ``handler_occ_state_effect`` re-exports
every name, so its public surface and existing tests are unchanged.

What OMN-15247 adds on top of the OMN-14619 machinery
-----------------------------------------------------
* :func:`resolve_red_ref` — the RED reference is the **merge base**, not
  ``pr.base.sha``. ``pr.base.sha`` is the base branch tip recorded on the PR
  object; on a branch that has moved since the PR was cut it is NOT the state
  the PR was derived from, so a probe "RED at base" proves nothing about the
  diff. OMN-15247's acceptance bar names the merge base explicitly. Both
  producers now resolve the same ref.
* :func:`is_shell_safe_check` — a mint-time fail-closed guard: a rendered
  ``check_value`` that carries a quote/backtick/backslash/``$`` outside its
  pinned URL is unquotable in the OCC compliance runner. Skip the candidate
  rather than emit it.
* :func:`is_yamlfmt_stable_check` — the MEASURED yamlfmt fold rule. The build
  spec's assumption that long double-quoted ``check_value`` scalars are left
  alone by yamlfmt is **false**; see that function for the counter-example and
  the real rule.
* :func:`render_check_value_field` (OMN-15247 foldproof follow-up) — the fix
  for the consequence above. A double-quoted plain scalar that would fold is
  rendered instead as a literal block scalar (``|-``), which yamlfmt NEVER
  refolds regardless of length. This is what makes ``content_bound`` a
  functioning binding rather than a fail-closed no-op: fold-safety moves from
  candidate SELECTION (rejecting anything long) to RENDERING (emitting
  anything, safely).

Everything in this module is pure; the network halves are injected as callables
so the whole derivation is unit-testable with no live GitHub.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DEPENDENCY_LOCK_BASENAMES",
    "DEPENDENCY_MANIFEST_BASENAMES",
    "LOCK_FILE_SUFFIXES",
    "MAX_CHECK_VALUE_LENGTH",
    "MAX_WORKFLOW_PIN_FILES",
    "WORKFLOW_PIN_DIR",
    "WORKFLOW_PIN_REPOSITORIES",
    "ConsideredPath",
    "SymbolCandidate",
    "build_considered_paths",
    "build_content_read_check",
    "classify_dependency_pin_only",
    "classify_workflow_core_pin_only",
    "declaration_count",
    "describe_uncandidated_path",
    "extract_contract_pin_candidates",
    "extract_lock_line_candidates",
    "extract_symbol_candidates",
    "is_contract_pin_advance_diff",
    "is_shell_safe_check",
    "is_yamlfmt_stable_check",
    "render_check_value_field",
    "render_considered_paths",
    "render_considered_paths_inline",
    "resolve_red_ref",
    "select_asserted_check",
]

# Matches an added top-level declaration line in a unified diff hunk, e.g.
# "+class HandlerCodegenOutcomeReducer:" or "+    async def handle(self, ...):".
# Deterministic, LLM-free stand-in for "pick a symbol the PR adds" — the exact
# authoring move the hand-authored reference companions (OCC#4135, OCC#4136)
# made manually ("class HandlerCodegenOutcomeReducer defined ... — head 1 / dev 404").
_DECLARATION_RE = re.compile(
    r"^\+\s*(async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

# A 40-hex (or abbreviated ≥7) git object id — the only ref form a generated
# content-bound check may pin. The OCC contract-compliance runner substitutes
# ONLY ``{pr}``/``{repo}``/``{ticket_id}`` and ``${PR_NUMBER}``/``${REPO}``/
# ``${TICKET_ID}``; there is no ``${SHA}`` / ``${MERGE_BASE}`` token, so a pinned
# ref MUST be a literal (contract_compliance_check.py ``_substitute_tokens``).
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Belt-and-braces bound. A check_value longer than this is rejected at mint time
# rather than risking a yamlfmt fold that would restale ``contract_sha256``
# (F-03 / OMN-14684). The generated form is ~120-200 chars in practice.
MAX_CHECK_VALUE_LENGTH = 400

# The rendered contract line is ``<8 spaces>check_value: "<value>"`` and the OCC
# ``.yamlfmt`` pins ``max_line_length: 100``. Both are inputs to the MEASURED
# fold rule in :func:`is_yamlfmt_stable_check`.
_CONTRACT_CHECK_VALUE_INDENT = 8
_OCC_YAMLFMT_MAX_LINE_LENGTH = 100

# Metacharacters that must not appear in a generated check_value. ``$`` is
# excluded deliberately: the ONLY sanctioned ``$`` in a generated check is the
# runner's own placeholder vocabulary, and a content-bound check pins a literal
# ref instead — so any ``$`` here is a defect. Single quotes would break the
# ``grep -c '<needle>'`` quoting; backticks/backslashes/double quotes are shell
# and YAML hazards.
_FORBIDDEN_CHECK_CHARS = ("'", '"', "`", "\\", "$")


# ``lock_line`` (OMN-16410) and ``text_line`` (OMN-18876) are both FIXED-STRING
# needles: counted with ``str.count`` and grepped with ``grep -cF``. They differ
# only in which extractor proposes them, which keeps each exemption-adjacent
# surface separately named and separately reviewable.
CandidateKind = Literal["class", "def", "lock_line", "text_line"]
_FIXED_STRING_KINDS: frozenset[str] = frozenset({"lock_line", "text_line"})


@dataclass(frozen=True)
class SymbolCandidate:
    """One (path, kind, symbol) triple extracted from an added diff line — pure."""

    path: str
    kind: CandidateKind
    symbol: str


def extract_symbol_candidates(
    files: list[dict[str, object]],
) -> tuple[SymbolCandidate, ...]:
    """Pure: parse GitHub PR-files ``patch`` hunks for added top-level class/def lines.

    Only considers Python files with status ``added``/``modified`` (never a pure
    rename/removal) — the diff patch is the only place we look, never file
    content, so this stays a function of ``files`` alone.

    KNOWN, DELIBERATE LIMITATION (OMN-15247 slice 1): the candidate grammar sees
    only added top-level ``class``/``def`` lines in ``.py`` files. A non-Python
    PR yields zero candidates here. For lockfiles see
    :func:`extract_lock_line_candidates` (OMN-16410) — a DIFFERENT grammar with
    a different signature, not merged into this one, because GitHub omits the
    ``patch`` field this function depends on once a file's diff crosses an
    undocumented per-file size threshold (MEASURED:
    omnibase_infra#2848's ``uv.lock``, 4396 changed lines, `patch` absent from
    `/pulls/{n}/files`) — exactly the shape a full ``uv.lock`` relock produces,
    so a patch-based lockfile grammar would silently yield nothing on the very
    PRs it exists to cover.
    """
    candidates: list[SymbolCandidate] = []
    for f in files:
        path = str(f.get("filename", ""))
        status = f.get("status")
        patch = f.get("patch")
        if not path.endswith(".py") or status not in ("added", "modified"):
            continue
        if not isinstance(patch, str):
            continue
        for line in patch.splitlines():
            match = _DECLARATION_RE.match(line)
            if not match:
                continue
            kind: Literal["class", "def"] = (
                "class" if match.group(1) == "class" else "def"
            )
            candidates.append(
                SymbolCandidate(path=path, kind=kind, symbol=match.group(2))
            )
    return tuple(candidates)


# A double-quoted run of 12-140 characters inside a lockfile line, excluding
# every char :data:`_FORBIDDEN_CHECK_CHARS` bars anywhere (so the extracted
# needle is shell-safe by construction, before ``is_shell_safe_check`` even
# runs — the ``'``/``"``/backtick/backslash/``$`` exclusion mirrors that
# constant, not a fresh policy). Matches uv.lock's TOML-ish quoted fields: a
# wheel/sdist URL (``"https://.../omnibase_core-0.46.11-py3-none-any.whl"``),
# a sha256 hash (``"sha256:...``), or a registry mirror host — whichever
# actually differs between the two refs.
#
# OMN-18848 CORRECTION: this comment used to name a bare version string
# (``"0.46.11"``) as a fourth supported needle. It is seven characters and the
# 12-char floor above provably cannot match it, so that example advertised a
# case this regex has never served — which is precisely why a pure
# post-release version bump yields zero candidates and the producer declines.
# The floor is CORRECT and is deliberately left alone: a probe asserting that
# the file containing ``version = "0.4.133"`` contains ``0.4.133`` is a
# tautology pinned to the head SHA, the non-falsifiable PR-existence-probe
# class OMN-15247 exists to refuse. That diff shape is handled by
# :func:`classify_dependency_pin_only`, an explicit exemption, never by
# lowering this floor until a tautology squeaks through.
_LOCK_QUOTED_RE = re.compile(r'"([^"\'`$\\]{12,140})"')

# uv.lock is the only lockfile OMN-13902's sibling-lock-refresh bot touches
# (verified live: omnibase_infra#2848 `gh pr view --json files` == ["uv.lock"]
# alone). Scoped narrowly on purpose — widening to poetry.lock/package-lock.json
# is a follow-up for whenever those bots exist, not speculative now.
LOCK_FILE_SUFFIXES = ("uv.lock",)

# Bounds the number of candidates (and therefore live GitHub content-API round
# trips) :func:`select_asserted_check` may spend per file walking lock_line
# candidates before it finds (or exhausts) a RED-controlled one.
_MAX_LOCK_LINE_CANDIDATES_PER_FILE = 5


def extract_lock_line_candidates(
    *, path: str, head_content: str | None, base_content: str | None
) -> tuple[SymbolCandidate, ...]:
    """Pure: derive ``lock_line`` candidates from FULL FILE CONTENT at two refs.

    (OMN-16410 residual gap — the class of PR this closes: a pure ``uv.lock``
    bump syncing a released sibling version, or a registry-mirror URL rewrite,
    has no Python declaration and, pre-fix, zero candidates from
    :func:`extract_symbol_candidates` — the emitter declined every such PR
    citing "no changed-file candidate could be proven RED" and required
    hand-authored evidence for a mechanically-provable fact, per OMN-15247's
    own non-falsifiability rule this producer must obey.)

    Deliberately does NOT read the GitHub ``patch`` field the Python grammar
    uses — GitHub omits ``patch`` once a file's diff crosses an undocumented
    per-file size threshold, and a full-relock ``uv.lock`` bump is exactly the
    shape most likely to cross it (see :func:`extract_symbol_candidates`'s
    docstring for the measured case). The diff is instead computed HERE,
    locally, from two already-fetched full file contents via a line-multiset
    difference — no patch parsing, no network inside this function (both
    contents are supplied by the caller, keeping this pure and unit-testable
    with no live GitHub).

    A line present in ``head_content`` strictly more often than in
    ``base_content`` is genuinely new (or newly-repeated) at head;
    :data:`_LOCK_QUOTED_RE` extracts safe quoted substrings from each such
    line, in file order. A substring that ALSO appears anywhere in
    ``base_content`` (even on a different, unchanged line — e.g. an unchanged
    sha256 hash sitting on an otherwise-edited line) is dropped here rather
    than proposed: it would never be RED-controlled, so there is no reason to
    spend a candidate slot or a downstream mint-time probe on it. Surviving
    candidates are capped at :data:`_MAX_LOCK_LINE_CANDIDATES_PER_FILE`. This
    membership check is still a cheaper, coarser proxy for the real bar —
    every surviving candidate goes through :func:`select_asserted_check`'s
    live RED/GREEN execution (a real ``gh api`` fetch + exact-count compare)
    before it can back a receipt, exactly like a Python symbol candidate does,
    with no new acceptance path.
    """
    if not head_content:
        return ()
    head_lines = Counter(head_content.splitlines())
    base_lines = Counter(base_content.splitlines()) if base_content else Counter()
    candidates: list[SymbolCandidate] = []
    found = 0
    for line, head_n in head_lines.items():
        if found >= _MAX_LOCK_LINE_CANDIDATES_PER_FILE:
            break
        if head_n <= base_lines.get(line, 0):
            continue  # not net-new at head -- unchanged or removed, not RED-controlled
        for m in _LOCK_QUOTED_RE.finditer(line):
            needle = m.group(1)
            # A net-new LINE can still repeat a substring that exists
            # elsewhere at base (e.g. an unchanged sha256 sitting on an
            # otherwise-edited line) -- excluded here, at the string level,
            # not just the line level, so a wasted mint-time RED/GREEN
            # execution is never spent on a needle this function can already
            # tell won't discriminate.
            if base_content and needle in base_content:
                continue
            candidates.append(
                SymbolCandidate(path=path, kind="lock_line", symbol=needle)
            )
            found += 1
            if found >= _MAX_LOCK_LINE_CANDIDATES_PER_FILE:
                break
    return tuple(candidates)


# ---------------------------------------------------------------------------
# OMN-18876 -- release-cut and runtime-pin text-line candidates.
# ---------------------------------------------------------------------------

# The release artefacts whose net-new lines may back a content-bound check.
# This is an EVIDENCE surface, not an exemption: every candidate still has to
# pass :func:`select_asserted_check`'s live GREEN-at-head / RED-at-merge-base
# bar. It is still scoped as narrowly as :data:`LOCK_FILE_SUFFIXES`, because a
# changelog heading proves only that a release was recorded, so it may stand in
# for a diff only when the diff carries nothing more behavioural than that
# (see :func:`is_release_artifact_only_diff`). Widening either tuple is a
# deliberate decision with its own review.
#
# A changelog contributes its net-new HEADING lines (the release train writes
# ``## v<version> (<date>)``); a runtime Dockerfile contributes its net-new
# double-quoted runs (the plugin pin cascade rewrites
# ``"<package>>=<floor>,<ceiling>"`` literals).
CHANGELOG_BASENAMES = ("CHANGELOG.md",)
RUNTIME_DOCKERFILE_PREFIXES = ("Dockerfile",)

# Paths that may ride along with a release artefact without disqualifying the
# diff: the version manifest and its lock. They carry no candidate of their own
# here; a manifest/lock-only diff stays on :func:`classify_dependency_pin_only`.
_RELEASE_COMPANION_BASENAMES = ("pyproject.toml", "uv.lock")

_MAX_TEXT_LINE_CANDIDATES_PER_FILE = 5
_MIN_TEXT_LINE_NEEDLE = 12
_MAX_TEXT_LINE_NEEDLE = 140


def _basename(path: str) -> str:
    return str(path).rsplit("/", 1)[-1]


def _is_changelog(path: str) -> bool:
    return _basename(path) in CHANGELOG_BASENAMES


def _is_runtime_dockerfile(path: str) -> bool:
    return _basename(path).startswith(RUNTIME_DOCKERFILE_PREFIXES)


def is_release_line_source(path: str) -> bool:
    """Pure: whether :func:`extract_release_line_candidates` can read ``path``."""
    return _is_changelog(path) or _is_runtime_dockerfile(path)


def is_release_artifact_only_diff(changed_paths: Sequence[str]) -> bool:
    """Pure: does every changed path belong to a release cut or a runtime pin bump?

    OMN-18876. True only when at least one path is a changelog or a runtime
    Dockerfile AND every other path is one of those or the version manifest /
    lock (:data:`_RELEASE_COMPANION_BASENAMES`). Fail-closed: an empty list is
    an unobservable diff, not an empty one, and any other path (a source file,
    a workflow, a config) disqualifies the whole diff so its real change can
    never be traded for a changelog line.
    """
    if not changed_paths:
        return False
    carries_claim = False
    for path in changed_paths:
        if is_release_line_source(path):
            carries_claim = True
            continue
        if _basename(path) in _RELEASE_COMPANION_BASENAMES:
            continue
        return False
    return carries_claim


def _is_safe_text_needle(needle: str) -> bool:
    return _MIN_TEXT_LINE_NEEDLE <= len(needle) <= _MAX_TEXT_LINE_NEEDLE and not any(
        char in needle for char in _FORBIDDEN_CHECK_CHARS
    )


def extract_release_line_candidates(
    *, path: str, head_content: str | None, base_content: str | None
) -> tuple[SymbolCandidate, ...]:
    """Pure: ``text_line`` candidates from a changelog or runtime Dockerfile.

    OMN-18876. Same net-new-line multiset difference as
    :func:`extract_lock_line_candidates` (full content at two refs, never the
    ``patch`` field). A changelog proposes each net-new heading line verbatim;
    a runtime Dockerfile proposes each net-new double-quoted run. A needle that
    occurs anywhere at base is dropped, since it can never go RED. Needles are
    12-140 characters and free of every :data:`_FORBIDDEN_CHECK_CHARS`
    character. Capped at :data:`_MAX_TEXT_LINE_CANDIDATES_PER_FILE`, in head
    file order, so the verdict is a function of the two contents alone.
    """
    if not head_content:
        return ()
    if not is_release_line_source(path):
        return ()
    changelog = _is_changelog(path)
    base_text = base_content or ""
    base_lines = Counter(base_text.splitlines())
    seen_lines: Counter[str] = Counter()
    candidates: list[SymbolCandidate] = []
    seen_needles: set[str] = set()
    for line in head_content.splitlines():
        seen_lines[line] += 1
        if seen_lines[line] <= base_lines.get(line, 0):
            continue  # not net-new at head
        if changelog:
            stripped = line.strip()
            needles = [stripped] if stripped.startswith("#") else []
        else:
            needles = [m.group(1) for m in _LOCK_QUOTED_RE.finditer(line)]
        for needle in needles:
            if needle in seen_needles or not _is_safe_text_needle(needle):
                continue
            if base_text and needle in base_text:
                continue
            seen_needles.add(needle)
            candidates.append(
                SymbolCandidate(path=path, kind="text_line", symbol=needle)
            )
            if len(candidates) >= _MAX_TEXT_LINE_CANDIDATES_PER_FILE:
                return tuple(candidates)
    return tuple(candidates)


# ---------------------------------------------------------------------------
# OMN-17292 -- bot contract-pin advance.
# ---------------------------------------------------------------------------

# omnibase_infra's omnimarket-contract-pin-refresh workflow rewrites exactly one
# line of this file, ``omnimarket_contract_ref: <40-hex>``, and commits the
# topology/catalog outputs derived from it. The new ref is falsifiable: present
# at head, absent at the merge base. Same RED/GREEN bar as every other grammar.
CONTRACT_PIN_BASENAMES = ("omnimarket-contract-pin.yaml",)
_CONTRACT_PIN_DERIVED_PREFIXES = (
    "src/omnibase_infra/topology/instances/",
    "docker/catalog/database-topology/",
)
_CONTRACT_PIN_REF_RE = re.compile(r"^omnimarket_contract_ref:\s*([0-9a-f]{40})\s*$")


def _is_contract_pin_file(path: str) -> bool:
    return _basename(path) in CONTRACT_PIN_BASENAMES


def is_contract_pin_advance_diff(changed_paths: Sequence[str]) -> bool:
    """Pure: is every changed path the contract pin or an output derived from it?

    OMN-17292. True only when the pin file is changed AND every other path is
    under a derived-output prefix the refresh workflow commits with it.
    Fail-closed: an empty list is an unobservable diff, and any other path (a
    source file, a workflow, a test) disqualifies the whole diff so its real
    change is never traded for a pin line.
    """
    if not changed_paths:
        return False
    carries_pin = False
    for path in changed_paths:
        if _is_contract_pin_file(path):
            carries_pin = True
        elif not str(path).startswith(_CONTRACT_PIN_DERIVED_PREFIXES):
            return False
    return carries_pin


def extract_contract_pin_candidates(
    *, path: str, head_content: str | None, base_content: str | None
) -> tuple[SymbolCandidate, ...]:
    """Pure: the net-new ``omnimarket_contract_ref`` sha as a ``text_line`` candidate.

    OMN-17292. A 40-hex sha that also occurs anywhere at base is dropped (it
    could never go RED). Zero network; the caller supplies both contents.
    """
    if not head_content or not _is_contract_pin_file(path):
        return ()
    base_text = base_content or ""
    candidates: list[SymbolCandidate] = []
    for line in head_content.splitlines():
        match = _CONTRACT_PIN_REF_RE.match(line)
        if match is None:
            continue
        needle = match.group(1)
        if needle in base_text or not _is_safe_text_needle(needle):
            continue
        candidates.append(SymbolCandidate(path=path, kind="text_line", symbol=needle))
    return tuple(candidates)


def declaration_count(content: str | None, kind: CandidateKind, symbol: str) -> int:
    """Pure: count ``class X`` / ``def X`` / ``async def X`` declaration lines,

    or (``kind="lock_line"``, OMN-16410) the literal-substring occurrence count
    of ``symbol`` — a quoted run extracted by :func:`extract_lock_line_candidates`
    from a net-new lockfile content line, e.g. a wheel URL, a sha256 hash, or a bare
    version string. ``str.count`` on purpose, not a regex: the needle already
    excludes every regex-hostile char that would need escaping (see
    :data:`_LOCK_QUOTED_RE`), and an exact substring count is precisely what the
    paired ``grep -cF`` in :func:`build_content_read_check` measures live.
    """
    if not content:
        return 0
    if kind in _FIXED_STRING_KINDS:
        return content.count(symbol)
    verb = "class" if kind == "class" else r"(?:async\s+def|def)"
    pattern = re.compile(rf"^\s*{verb}\s+{re.escape(symbol)}\b", re.MULTILINE)
    return len(pattern.findall(content))


def build_content_read_check(
    *,
    repo: str,
    path: str,
    kind: CandidateKind,
    symbol: str,
    head_sha: str,
) -> str:
    """Canonical honest content-read check_value (reference_occ_receipt_gate_flow).

    Pinned to the PR head SHA; ``grep -c``/``grep -cF`` exits non-zero (RED)
    when the declaration/needle is absent from the file at that ref — never a
    bare existence probe (a `.sha`/`.content` presence check would rubber-stamp
    a MODIFIED file that already existed on the base branch).

    ``kind="lock_line"`` (OMN-16410) greps ``symbol`` verbatim as a FIXED
    string (``-F``) rather than the ``"{kind} {symbol}"`` regex needle the
    Python branch builds: a lockfile needle is an arbitrary quoted run (a URL,
    a hash, a version) that may contain regex metacharacters (``.``, ``+``,
    ``[``) whose literal, not pattern, meaning is what was actually asserted
    RED/GREEN by :func:`select_asserted_check`'s live execution — ``-F`` keeps
    the mint-time-run command and the acceptance-time-run command asking the
    exact same question.

    NOTE (OMN-14619 live proof, 2026-07-14): the reference memory's form
    (``--jq -r .content``) is WRONG — ``gh api`` rejects it
    ("accepts 1 arg(s), received 2") because ``-r`` is not a valid ``--jq``
    sub-flag; ``gh api --jq`` already prints scalar results raw with no ``-r``
    needed. Live-verified against omnimarket#1760 (OMN-14608): the corrected
    form below returns ``1``/exit 0 at the PR head and ``0``/exit 1 (RED) at
    the PR base — this handler's own canary evidence, not the memory's text.
    """
    if kind in _FIXED_STRING_KINDS:
        needle = symbol
        grep_flags = "-cF"
    else:
        needle = f"{kind} {symbol}"
        grep_flags = "-c"
    return (
        f"gh api repos/{repo}/contents/{path}?ref={head_sha} --jq '.content' "
        f"| base64 -d | grep {grep_flags} '{needle}'"
    )


def is_shell_safe_check(check_value: str) -> bool:
    """Pure: True iff a rendered content-bound ``check_value`` is safe to emit.

    Fail-closed mint-time guard (OMN-15247 §B5). The generated form quotes its
    needle in single quotes and pins a literal ref, so the only characters that
    may legitimately appear are the two the template itself contributes: the
    ``'`` pair around ``--jq '.content'`` / ``grep -c '<needle>'`` and the ``|``
    pipes. Anything else — a stray quote, backtick, backslash, or ``$`` coming
    from a repo/path/symbol — would be unquotable in the compliance runner or an
    unstable yamlfmt scalar. Rather than emit it, the caller skips the candidate.

    The template's own single quotes are accounted for by stripping the two
    sanctioned quoted spans before scanning; ``symbol`` is a Python identifier by
    construction (``_DECLARATION_RE``), so this is belt-and-braces, not
    speculation — but a repo or path containing a metacharacter is not.
    """
    if not check_value or len(check_value) > MAX_CHECK_VALUE_LENGTH:
        return False
    # Exactly one pinned ``?ref=<sha>`` and nothing else resembling a token.
    refs = re.findall(r"\?ref=([^\s'\"]+)", check_value)
    if len(refs) != 1 or not _SHA_RE.match(refs[0]):
        return False
    # Strip the two sanctioned single-quoted spans the template contributes, then
    # assert no forbidden metacharacter survives anywhere else.
    stripped = re.sub(r"'[^']*'", "", check_value)
    return not any(char in stripped for char in _FORBIDDEN_CHECK_CHARS)


def is_yamlfmt_stable_check(
    check_value: str,
    *,
    indent: int = _CONTRACT_CHECK_VALUE_INDENT,
    max_line_length: int = _OCC_YAMLFMT_MAX_LINE_LENGTH,
    key: str = "check_value",
) -> bool:
    """Pure: True iff the rendered ``check_value:`` line is a yamlfmt FIXPOINT.

    MEASURED, not assumed (OMN-15247; yamlfmt v0.21.0 against the real
    onex_change_control ``.yamlfmt``). The build spec asserted that long
    double-quoted ``check_value`` scalars carrying ``|``/``$``/``'`` are left
    alone by yamlfmt. **That is false**, and the counter-example is the
    content-bound form itself. The actual rule:

        yamlfmt folds a double-quoted scalar at the FIRST SPACE occurring at a
        column greater than ``max_line_length`` (100). If every space in the
        rendered line falls at or before that column, the line is a fixpoint at
        any total length.

    That is why the pre-OMN-15407 deploy-assessment value (127 chars, last space
    at column 84) IS a fixpoint while a content-bound read of identical length
    (spaces at columns 101+, because the pinned URL is one long token that pushes
    ``--jq`` past the limit) is NOT. Its OMN-15407 successor
    (``occ_evidence_stamp.deploy_assessment_check_value``) is longer — a literal
    repo slug is wider than ``${REPO}`` — so whether it is a fixpoint now depends
    on the repo slug and PR width, and :func:`render_check_value_field` decides
    per value rather than the shape being known-stable by inspection. A fold
    rewrites the file, which
    restales ``contract_sha256`` (F-03 / OMN-14684) and fails the hosted yamlfmt
    pre-commit.

    Consequence, stated plainly: with a 40-hex pinned ref the space before
    ``--jq`` sits at column ``91 + len(repo) + len(path)``, so a stable
    content-bound check needs ``len(repo) + len(path) <= 9`` — unreachable for
    any real repo slug. The ``content_bound`` binding is therefore **fail-closed
    inert on realistic PRs today**: it emits nothing rather than emitting a byte
    that would restale the contract hash. Making it usable needs either
    deterministic pre-folding of the emitted scalar or a check grammar whose
    post-URL segment is space-free — deliberate follow-up, not slice 1, and the
    reason this binding ships default-OFF.

    ``tests/unit/.../test_occ_autobind_contention_omn_15247.py`` validates this
    predicate against the REAL yamlfmt binary, so it can never silently drift
    from the formatter it models.

    OMN-15247 foldproof follow-up: this predicate is no longer a mint-time
    ACCEPTANCE filter (a candidate is never rejected merely for being long) —
    it is the internal decision :func:`render_check_value_field` uses to pick
    between the byte-identical quoted form and the fold-proof literal-block
    form. ``key`` defaults to ``"check_value"`` for the contract's 8-indent
    line; the receipt's ``probe_command``/``actual_output`` lines pass their
    own key so the rendered prefix length (``key: "``) is measured correctly.
    """
    line = f'{" " * indent}{key}: "{check_value}"'
    return not any(
        char == " " and column > max_line_length for column, char in enumerate(line)
    )


def render_check_value_field(
    key: str,
    value: str,
    *,
    indent: int = _CONTRACT_CHECK_VALUE_INDENT,
) -> str:
    """Pure: render one ``{key}: <value>`` YAML mapping line, fold-proof.

    OMN-15247 foldproof follow-up. This is the fix for the "already-deployed
    OMNI_OCC_CHECK_BINDING=content_bound mode is a fail-closed no-op" defect:
    ``content_bound`` was structurally unable to emit ANY realistic check,
    because the emitter rejected every candidate whose quoted rendering would
    fold (:func:`is_yamlfmt_stable_check`) rather than emit a byte the hosted
    yamlfmt pre-commit would rewrite. Moving the fold-safety decision from
    candidate SELECTION to RENDERING removes that ceiling entirely:

    * If the double-quoted plain-scalar rendering at ``indent`` would survive
      yamlfmt unchanged (:func:`is_yamlfmt_stable_check`), keep that exact
      form — byte-for-byte identical to every pre-OMN-15247 companion (the
      short existence-probe placeholders, the private-repo hosted-safe
      ``receipt_local_check_value`` form, and any other value that happens to
      already fit).
    * Otherwise render ``value`` as a literal block scalar (``|-``). MEASURED
      against yamlfmt v0.21.0 with the real onex_change_control ``.yamlfmt``:
      a literal block is NEVER refolded regardless of length, for every
      realistic shape probed — the contract's indent-8 ``check_value`` line
      and the receipt's indent-0 ``check_value``/``probe_command``/
      ``actual_output`` lines alike (``TestContentBoundSurvivesRealYamlfmt``,
      ``test_the_content_bound_check_value_survives_real_yamlfmt_byte_identical``).
      The chomping indicator ``-`` strips the trailing newline so the parsed
      string carries none. ``value`` is guaranteed single-line by every caller
      in this seam (a generated shell command, or a fixed-shape prose string
      built from short prose + pinned SHAs), so no multi-line handling is
      needed here.

    Consumers that parse the resulting YAML (``lint_contract_check_values.py``,
    ``check_contract_substance_floor.py``, ``contract_compliance_check.py``,
    ``check_generated_checks_red_derivable.py``) all read the value via
    ``yaml.safe_load`` and compare the PARSED Python string, never the raw
    byte layout — so switching rendering STYLE changes zero consumer-gate
    behavior; only the on-disk bytes and their fold-stability change.
    """
    if is_yamlfmt_stable_check(value, indent=indent, key=key):
        return f'{" " * indent}{key}: "{value}"\n'
    body_indent = " " * (indent + 2)
    return f"{' ' * indent}{key}: |-\n{body_indent}{value}\n"


def select_asserted_check(
    candidates: Sequence[SymbolCandidate],
    *,
    repo: str,
    head_sha: str,
    base_sha: str,
    fetch_content: Callable[[str, str], str | None],
    accept: Callable[[str], bool] | None = None,
    on_reject: Callable[[SymbolCandidate, str], None] | None = None,
) -> str | None:
    """Pick the first candidate that is RED-controllable, or None.

    A candidate passes only when its declaration is present at ``head_sha`` AND
    strictly more numerous there than at ``base_sha`` (feedback_prove_red_against
    _exists_but_wrong: assert against the PR-introduced state, never a symbol
    that already existed before the PR). ``fetch_content`` is injected so this
    function stays pure and unit-testable without live network calls — the
    effect boundary supplies a real GitHub content reader.

    OMN-15247: ``base_sha`` should be the **merge base** (see
    :func:`resolve_red_ref`), not the PR object's ``base.sha``. A rendered check
    that fails :func:`is_shell_safe_check` is skipped rather than emitted.

    ``accept`` is an optional caller-supplied predicate for constraints that
    depend on WHERE the check will be written, which this function cannot know.
    The emitter passes :func:`is_yamlfmt_stable_check` because a contract's
    ``check_value:`` line sits at indent 8, while a receipt's sits at indent 0 —
    different fold budgets, so the guard cannot live here. Default ``None``
    preserves OMN-14619's behavior for existing callers exactly.

    ``on_reject`` (OMN-18876 AC1) is told, for every candidate this function
    drops, the exact reason it was dropped. It observes and never decides: the
    verdict is identical with or without it. Candidates are tried in order and
    the first one that passes is returned, so the rejected ones are always a
    prefix of ``candidates``; any after the selected one were never evaluated.
    """

    def _reject(candidate: SymbolCandidate, reason: str) -> None:
        if on_reject is not None:
            on_reject(candidate, reason)

    for candidate in candidates:
        head_content = fetch_content(candidate.path, head_sha)
        head_count = declaration_count(head_content, candidate.kind, candidate.symbol)
        if head_count < 1:
            _reject(
                candidate,
                _REASON_UNREADABLE_AT_HEAD
                if not head_content
                else "absent at head (count 0)",
            )
            continue
        base_content = fetch_content(candidate.path, base_sha)
        base_count = declaration_count(base_content, candidate.kind, candidate.symbol)
        if base_count >= head_count:
            # not RED-controlled: already present at base, same or more
            _reject(
                candidate,
                f"not RED-controlled: count {base_count} at the merge base, "
                f"{head_count} at head",
            )
            continue
        check = build_content_read_check(
            repo=repo,
            path=candidate.path,
            kind=candidate.kind,
            symbol=candidate.symbol,
            head_sha=head_sha,
        )
        if not is_shell_safe_check(check):
            _reject(candidate, "rendered check is not shell-safe")
            continue  # unquotable — try the next candidate
        if accept is not None and not accept(check):
            _reject(candidate, "rendered check refused by the destination constraint")
            continue  # caller-specific destination constraint — try the next
        return check
    return None


# ---------------------------------------------------------------------------
# OMN-18876 AC1 -- a legible decline.
# ---------------------------------------------------------------------------
#
# A ``skip:NO_RED_DERIVABLE_CHECK`` decline used to print only an aggregate
# count. The pieces below let the emitter name every changed file it looked at
# and why each one could not back a check: either a candidate from it failed the
# selection bar above (``on_reject``), or no candidate grammar proposed one.

_REASON_UNREADABLE_AT_HEAD = (
    "unreadable at head (no content returned; the contents API returns no body "
    "for a file over 1 MB)"
)


@dataclass(frozen=True)
class ConsideredPath:
    """One changed file a content-bound derivation looked at, and its outcome."""

    path: str
    reason: str


def describe_uncandidated_path(
    *,
    path: str,
    status: object,
    patch_present: bool,
    release_only_diff: bool,
    head_readable: bool | None,
) -> str:
    """Pure: why a changed file contributed no candidate at all.

    Mirrors the three grammars' own entry conditions
    (:func:`extract_symbol_candidates`, :func:`extract_lock_line_candidates`,
    :func:`extract_release_line_candidates`), so the reason names the grammar
    that skipped the file rather than a generic "not derivable".
    ``head_readable`` is whether the caller got content for the file at head;
    ``None`` means the caller never fetched it.
    """
    if status not in ("added", "modified"):
        return f"status {status!r}: no new content at head to prove"
    if path.endswith(".py"):
        if not patch_present:
            return (
                "Python file, but GitHub omitted its patch (diff too large), so "
                "no added declaration could be read"
            )
        return "Python file with no added class or def line in its patch"
    if path.endswith(LOCK_FILE_SUFFIXES):
        if head_readable is False:
            return f"lockfile {_REASON_UNREADABLE_AT_HEAD}"
        return (
            "lockfile with no net-new quoted run of 12-140 safe characters "
            "absent from the merge base"
        )
    if is_release_line_source(path):
        if not release_only_diff:
            return (
                "release-line source, but the diff also changes a path that is "
                "not a release artefact, so release lines are not offered"
            )
        if head_readable is False:
            return f"release artefact {_REASON_UNREADABLE_AT_HEAD}"
        return (
            "release artefact with no net-new heading or quoted run absent from "
            "the merge base"
        )
    if _is_contract_pin_file(path):
        return (
            "contract-pin file with no net-new omnimarket_contract_ref sha "
            "absent from the merge base, or the diff changes a path that is "
            "not the pin or an output derived from it"
        )
    return (
        "no candidate grammar reads this file type (only Python declarations, "
        "uv.lock lines, release-artefact lines and contract-pin lines are "
        "proposed)"
    )


_MAX_REASONS_PER_PATH = 5
_MAX_SYMBOL_IN_REASON = 80


def build_considered_paths(
    *,
    files: Sequence[dict[str, object]],
    candidates: Sequence[SymbolCandidate],
    rejections: Sequence[tuple[SymbolCandidate, str]],
    selected_outcome: str | None,
    release_only_diff: bool,
    head_readable: Callable[[str], bool | None],
) -> tuple[ConsideredPath, ...]:
    """Pure: one :class:`ConsideredPath` per changed file, in listing order.

    ``rejections`` is what :func:`select_asserted_check` reported through
    ``on_reject``; it is a prefix of ``candidates``, so the candidate right
    after it is the one that was selected (``selected_outcome`` says what then
    happened to it at mint time) and any later ones were never evaluated. A file
    that produced no candidate gets :func:`describe_uncandidated_path`'s reason.
    """
    if [c for c, _ in rejections] != list(candidates[: len(rejections)]):
        raise ValueError("rejections must be a prefix of candidates, in order")
    reasons: dict[str, list[str]] = {}
    for index, candidate in enumerate(candidates):
        if index < len(rejections):
            outcome = rejections[index][1]
        elif index == len(rejections) and selected_outcome is not None:
            outcome = selected_outcome
        else:
            outcome = "not evaluated (selection stops at the first passing candidate)"
        symbol = candidate.symbol
        if len(symbol) > _MAX_SYMBOL_IN_REASON:
            symbol = symbol[: _MAX_SYMBOL_IN_REASON - 3] + "..."
        reasons.setdefault(candidate.path, []).append(
            f"{candidate.kind} `{symbol}` {outcome}"
        )
    considered: list[ConsideredPath] = []
    for f in files:
        path = str(f.get("filename", ""))
        path_reasons = reasons.get(path)
        if path_reasons:
            shown = path_reasons[:_MAX_REASONS_PER_PATH]
            if len(path_reasons) > _MAX_REASONS_PER_PATH:
                shown.append(
                    f"and {len(path_reasons) - _MAX_REASONS_PER_PATH} more candidate(s)"
                )
            considered.append(ConsideredPath(path=path, reason="; ".join(shown)))
            continue
        considered.append(
            ConsideredPath(
                path=path,
                reason=describe_uncandidated_path(
                    path=path,
                    status=f.get("status"),
                    patch_present=isinstance(f.get("patch"), str),
                    release_only_diff=release_only_diff,
                    head_readable=head_readable(path),
                ),
            )
        )
    return tuple(considered)


def render_considered_paths(
    considered: Sequence[ConsideredPath], *, limit: int = 40
) -> str:
    """Pure: a markdown bullet list of every considered path and its reason."""
    if not considered:
        return "Considered 0 changed file(s)."
    lines = [f"Considered {len(considered)} changed file(s):"]
    lines.extend(f"- `{item.path}`: {item.reason}" for item in considered[:limit])
    if len(considered) > limit:
        lines.append(f"- ... and {len(considered) - limit} more changed file(s)")
    return "\n".join(lines)


def render_considered_paths_inline(
    considered: Sequence[ConsideredPath], *, limit: int = 10
) -> str:
    """Pure: the same report on ONE line, for a ``reason=`` outcome field."""
    head = f"considered {len(considered)} changed file(s)"
    if not considered:
        return head
    parts = [f"{item.path} ({item.reason})" for item in considered[:limit]]
    if len(considered) > limit:
        parts.append(f"... and {len(considered) - limit} more")
    return f"{head}: " + "; ".join(parts)


def resolve_red_ref(
    *,
    pr_data: dict[str, object],
    compare: Callable[[str, str], dict[str, object]],
    commit: Callable[[str], dict[str, object]],
) -> str | None:
    """Resolve the ref a generated check must go RED against — the MERGE BASE.

    OMN-15247's acceptance bar is *"for every generated check, the same check run
    against the PR's merge-base must return non-zero."* The pre-existing
    OMN-14619 code used ``pr.base.sha``, which is the base branch tip recorded on
    the PR object — on a base branch that moved after the PR was cut, that commit
    may already contain the symbol (false negative: candidate wrongly rejected)
    or may predate unrelated work (false positive risk). The merge base is the
    only commit that is by construction the pre-diff state.

    * **Merged PR** → the first parent of ``merge_commit_sha``. On these
      squash-merge-only repos the squash commit's first parent IS the base tip
      the diff was applied to — exactly the pair OMN-15247 re-verified for
      OMN-15232 (``6e91834b`` vs ``f7fb7cdeb…``).
    * **Open PR** → ``GET /repos/{o}/{r}/compare/{base.sha}...{head.sha}`` →
      ``merge_base_commit.sha``.
    * **Unresolvable** → ``None``. The caller then emits NO content-bound check
      (fail-closed, OMN-15247 §B4) rather than falling back to a ref that cannot
      prove RED.

    ``compare`` and ``commit`` are injected callables (``(base, head) -> payload``
    and ``(sha) -> payload``) so this stays unit-testable with no network. Either
    may raise; the caller is responsible for degrading to ``None`` — see
    ``OccCompanionEmitter._resolve_red_ref_live``.
    """
    merged = bool(pr_data.get("merged")) or bool(pr_data.get("merged_at"))
    merge_commit_sha = pr_data.get("merge_commit_sha")
    if merged and isinstance(merge_commit_sha, str) and _SHA_RE.match(merge_commit_sha):
        payload = commit(merge_commit_sha)
        parents = payload.get("parents")
        if isinstance(parents, list) and parents:
            first = parents[0]
            if isinstance(first, dict):
                parent_sha = first.get("sha")
                if isinstance(parent_sha, str) and _SHA_RE.match(parent_sha):
                    return parent_sha
        return None

    head = pr_data.get("head")
    base = pr_data.get("base")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    base_sha = base.get("sha") if isinstance(base, dict) else None
    if not (isinstance(head_sha, str) and _SHA_RE.match(head_sha)):
        return None
    if not (isinstance(base_sha, str) and _SHA_RE.match(base_sha)):
        return None
    payload = compare(base_sha, head_sha)
    merge_base = payload.get("merge_base_commit")
    if isinstance(merge_base, dict):
        merge_base_sha = merge_base.get("sha")
        if isinstance(merge_base_sha, str) and _SHA_RE.match(merge_base_sha):
            return merge_base_sha
    return None


# ---------------------------------------------------------------------------
# OMN-18848 -- dependency-pin-only classification.
# ---------------------------------------------------------------------------

# The only two paths a post-release dependency bump may touch. Scoped as
# narrowly as :data:`LOCK_FILE_SUFFIXES` and for the same reason: this set is
# an EXEMPTION surface, so every name added to it is a file whose change no
# longer needs falsifiable evidence. Widening it (poetry.lock,
# package-lock.json) is a deliberate decision with its own review, never a
# convenience taken to unblock one PR.
DEPENDENCY_MANIFEST_BASENAMES = ("pyproject.toml",)
DEPENDENCY_LOCK_BASENAMES = ("uv.lock",)

# The dotted TOML key prefixes a dependency bump is allowed to move. A key
# OUTSIDE this set differing between the two refs disqualifies the diff, which
# is what makes "pyproject.toml changed" insufficient on its own: a bump that
# also edits `project.scripts`, `build-system` or `tool.hatch.force-include`
# is a behavioural change wearing a manifest's filename.
_PIN_ONLY_TOML_PREFIXES = (
    "project.version",
    "project.dependencies",
    "project.optional-dependencies",
    "dependency-groups",
    "tool.uv.sources",
    # OMN-20074: the sibling lock refresh moves a git rev here as well as in
    # tool.uv.sources (omniclaude#2646); an override is a pin, like a source.
    "tool.uv.override-dependencies",
    "tool.poetry.version",
    "tool.poetry.dependencies",
    "tool.poetry.group",
)


def _flatten_toml(value: object, prefix: str = "") -> dict[str, object]:
    """Pure: flatten a parsed TOML document to ``{dotted.key: scalar-or-list}``.

    Tables recurse; everything else (scalars, arrays, arrays-of-tables) is a
    leaf compared by equality. Comparing leaves by equality rather than
    recursing into arrays is deliberate: a dependency list is a single pin
    surface, and an element-wise walk would invent key paths (``[3]``) that no
    prefix in :data:`_PIN_ONLY_TOML_PREFIXES` could name.
    """
    if isinstance(value, dict):
        flat: dict[str, object] = {}
        for key, sub in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten_toml(sub, child))
        return flat
    return {prefix: value}


def _is_pin_key(key: str) -> bool:
    """Whether a dotted TOML key names a version or dependency-pin surface."""
    return any(
        key == prefix or key.startswith(f"{prefix}.")
        for prefix in _PIN_ONLY_TOML_PREFIXES
    )


def classify_dependency_pin_only(
    changed_paths: Sequence[str],
    *,
    pyproject_head: str | None,
    pyproject_base: str | None,
) -> tuple[bool, str]:
    """Pure: is this diff a dependency-pin-only change? ``(verdict, reason)``.

    OMN-18848. A post-release version bump carries no behavioural claim, so no
    changed-file candidate can be RED-derivable against the merge base and the
    OCC autobind producer correctly declines to mint a companion. Before
    OMN-18848 that decline was indistinguishable from "this PR owes evidence
    nobody wrote", and since OMN-18647 the companion-merged gate treats it as a
    permanent refusal -- so every post-release bump the release Dependency
    Cascade opens was unmergeable without a hand-authored companion.

    This function is the DERIVED half of the fix. It is not a token anybody can
    write: the verdict is computed from the diff itself, structurally, and the
    producer records it on a check-run bound to the PR's head SHA. A PR body
    cannot assert it, and a new head SHA has no outcome recorded against it.

    FAIL-CLOSED in every ambiguous direction, because a false positive here
    exempts a real change from evidence:

    * an empty changed-file list is NOT pin-only (an unobservable diff is not
      an empty one -- ``changed_files_from_diff_scope_probe`` returns ``()``
      when the probe itself failed);
    * any path whose basename is outside
      :data:`DEPENDENCY_MANIFEST_BASENAMES` + :data:`DEPENDENCY_LOCK_BASENAMES`
      disqualifies the whole diff, so a lockfile bump landing alongside one
      source file is not exempt;
    * if ``pyproject.toml`` changed, BOTH contents must be readable and must
      parse as TOML -- an unreadable or malformed manifest is not exempt;
    * every dotted key that differs between the two parses must be a version or
      dependency-pin key (:data:`_PIN_ONLY_TOML_PREFIXES`). A key added,
      removed or changed anywhere else disqualifies the diff.

    Comparing PARSED TOML rather than diff lines is deliberate. GitHub omits
    the ``patch`` field once a file's diff crosses an undocumented size
    threshold (the measured case in :func:`extract_lock_line_candidates`'s
    docstring), so a line-level classifier would silently see no forbidden
    lines on exactly the largest diffs and exempt them. A structural compare
    has no such blind spot: an unreadable side is a refusal, not an empty diff.
    """
    if not changed_paths:
        return False, "no changed files observed (unobservable diff, not an empty one)"

    allowed = set(DEPENDENCY_MANIFEST_BASENAMES) | set(DEPENDENCY_LOCK_BASENAMES)
    for path in changed_paths:
        basename = str(path).rsplit("/", 1)[-1]
        if basename not in allowed:
            return (
                False,
                f"changed path is not a dependency manifest or lockfile: {path}",
            )

    touches_manifest = any(
        str(path).rsplit("/", 1)[-1] in DEPENDENCY_MANIFEST_BASENAMES
        for path in changed_paths
    )
    if not touches_manifest:
        return True, "lockfile-only diff"

    if pyproject_head is None or pyproject_base is None:
        return False, "pyproject.toml content unreadable at one or both refs"

    try:
        head_doc = tomllib.loads(pyproject_head)
        base_doc = tomllib.loads(pyproject_base)
    except tomllib.TOMLDecodeError as exc:
        return False, f"pyproject.toml does not parse as TOML: {exc}"

    head_flat = _flatten_toml(head_doc)
    base_flat = _flatten_toml(base_doc)
    offending = sorted(
        key
        for key in set(head_flat) | set(base_flat)
        if head_flat.get(key) != base_flat.get(key) and not _is_pin_key(key)
    )
    if offending:
        return (
            False,
            "pyproject.toml changes outside version/dependency-pin keys: "
            + ", ".join(offending),
        )

    changed_pin_keys = sorted(
        key
        for key in set(head_flat) | set(base_flat)
        if head_flat.get(key) != base_flat.get(key)
    )
    if not changed_pin_keys:
        return True, "manifest and lockfile diff carries no semantic TOML change"
    return True, "version/dependency-pin keys only: " + ", ".join(changed_pin_keys)


# ---------------------------------------------------------------------------
# OMN-17427 -- workflow checkout pin bumps (the OMN-9050 downstream pin bump).
# ---------------------------------------------------------------------------

# The checkout repositories whose 40-hex ``ref:`` pin a workflow-only diff may
# move without owing evidence. This is an EXEMPTION surface, scoped exactly as
# narrowly as :data:`DEPENDENCY_MANIFEST_BASENAMES`: omnibase_core's
# ``publish-downstream-pin-bump.yml`` rewrites the ``ref:`` of an
# ``actions/checkout`` step whose ``repository:`` is omnibase_core, and nothing
# else. A ref under any other repository (the OCC checkout in a CI workflow,
# say) is NOT normalised, so a diff that moves it is refused.
WORKFLOW_PIN_REPOSITORIES = ("OmniNode-ai/omnibase_core",)
WORKFLOW_PIN_DIR = ".github/workflows/"
MAX_WORKFLOW_PIN_FILES = 5

_WF_REPOSITORY_RE = re.compile(
    r"^(?P<indent>[ ]*)(?:-[ ]+)?repository:[ ]*['\"]?(?P<repo>[^'\"\s#]+)['\"]?[ ]*(?:#.*)?$"
)
_WF_REF_RE = re.compile(
    r"^(?P<indent>[ ]*)ref:[ ]*['\"]?(?P<sha>[0-9a-f]{40})['\"]?(?P<tail>[ ]*(?:#.*)?)$"
)
# The only comment lines a pin bump may add, drop or rewrite: the banner the
# bump engine owns (omnibase_core scripts/pin_bump.py). Any other comment line
# that differs disqualifies the diff, because a ``#`` line inside a block
# scalar is file content, not a YAML comment.
_WF_PIN_BANNER_RE = re.compile(
    r"^[ ]*#[ ]*(?:Pinned to omnibase_core|Update by running:|"
    r"Auto-bumped by omnibase_core publish-downstream-pin-bump\.yml)"
)
_WF_PIN_PLACEHOLDER = "<omnibase-core-pin>"


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _normalize_workflow_pins(content: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pure: ``(normalised lines, pinned shas)`` for one workflow file.

    Drops the pin-banner comment lines and replaces the SHA of every ``ref:``
    that is a sibling of an allowed ``repository:`` key in the same mapping
    with a placeholder. A ``ref:`` that precedes its ``repository:`` is left
    alone, which fails closed (the diff then shows it). The scope of a
    ``repository:`` ends at the first non-blank line indented less than the
    mapping it sits in.
    """
    out: list[str] = []
    pins: list[str] = []
    scope_repo: str | None = None
    scope_indent = -1
    for line in content.splitlines():
        if _WF_PIN_BANNER_RE.match(line):
            continue
        if line.strip():
            indent = _indent_of(line)
            if scope_repo is not None and indent < scope_indent:
                scope_repo, scope_indent = None, -1
            m_repo = _WF_REPOSITORY_RE.match(line)
            if m_repo:
                scope_repo = m_repo.group("repo")
                # "- repository:" opens a mapping whose keys sit past the dash.
                scope_indent = indent + (
                    len(line[indent:]) - len(line[indent:].lstrip("- "))
                )
            m_ref = _WF_REF_RE.match(line)
            if (
                m_ref
                and scope_repo in WORKFLOW_PIN_REPOSITORIES
                and indent == scope_indent
            ):
                pins.append(m_ref.group("sha"))
                line = (
                    f"{m_ref.group('indent')}ref: {_WF_PIN_PLACEHOLDER}"
                    f"{m_ref.group('tail')}"
                )
        out.append(line.rstrip())
    return tuple(out), tuple(pins)


def classify_workflow_core_pin_only(
    changed_paths: Sequence[str],
    *,
    contents: Mapping[str, tuple[str | None, str | None]],
) -> tuple[bool, str]:
    """Pure: is this diff only an omnibase_core workflow checkout pin bump?

    OMN-17427. omnibase_core's downstream pin bump (OMN-9050) opens one PR per
    downstream repo whose whole diff moves the 40-hex ``ref:`` of an
    ``actions/checkout`` step pinned to omnibase_core, plus the bump engine's
    banner comment. Like the manifest bump :func:`classify_dependency_pin_only`
    handles, it carries no behavioural claim, so no changed-file candidate is
    RED-derivable and the producer declined every such PR with
    ``NO_RED_DERIVABLE_CHECK``, which the companion-merged gate and the receipt
    gate read as "hand-authored evidence owed". Each bump therefore cost a
    hand-authored OCC companion per repo.

    ``contents`` maps each changed path to ``(head, base)`` file content.

    FAIL-CLOSED in every ambiguous direction:

    * an empty changed-file list, or more than a bounded number of files, is
      refused;
    * any path outside ``.github/workflows/`` or not ``.yml``/``.yaml`` is
      refused;
    * a file unreadable at either ref (absent, added or deleted) is refused;
    * after dropping the pin-banner comment lines and replacing every
      omnibase_core checkout pin with a placeholder, head and base must be
      identical line for line, so any other edit (a ref under another
      repository, a step, a trailing comment, whitespace) is refused;
    * at least one omnibase_core pin must actually move, and every pin at head
      must be a single SHA.
    """
    if not changed_paths:
        return False, "no changed files observed (unobservable diff, not an empty one)"
    if len(changed_paths) > MAX_WORKFLOW_PIN_FILES:
        return False, f"more than {MAX_WORKFLOW_PIN_FILES} workflow files changed"
    moved: list[str] = []
    head_pins_all: set[str] = set()
    for path in changed_paths:
        p = str(path)
        if not p.startswith(WORKFLOW_PIN_DIR) or not p.endswith((".yml", ".yaml")):
            return False, f"changed path is not a workflow file: {p}"
        head, base = contents.get(p, (None, None))
        if head is None or base is None:
            return False, f"workflow content unreadable at one or both refs: {p}"
        head_lines, head_pins = _normalize_workflow_pins(head)
        base_lines, base_pins = _normalize_workflow_pins(base)
        if head_lines != base_lines:
            return (
                False,
                f"{p} changes outside the omnibase_core checkout pin and its banner",
            )
        if head_pins != base_pins:
            moved.append(p)
        head_pins_all.update(head_pins)
    if not moved:
        return False, "no omnibase_core checkout pin moved"
    if len(head_pins_all) != 1:
        return False, "omnibase_core checkout pins at head do not name one SHA"
    sha = next(iter(head_pins_all))
    return True, (
        f"omnibase_core workflow checkout pin only -> {sha[:12]}: " + ", ".join(moved)
    )


# ---------------------------------------------------------------------------
# OMN-20074 -- plugin manifest version bumps (the OMN-20710 post-merge bump).
# ---------------------------------------------------------------------------

# The plugin manifests a version-only diff may move, by basename, and only
# inside a ``.claude-plugin/`` directory. An EXEMPTION surface, scoped as
# narrowly as :data:`DEPENDENCY_MANIFEST_BASENAMES`.
PLUGIN_MANIFEST_BASENAMES = ("plugin.json", "marketplace.json")
PLUGIN_MANIFEST_DIR = ".claude-plugin"
MAX_PLUGIN_MANIFEST_FILES = 5

# The only flattened JSON keys a version bump may change: the manifest's own
# ``version`` and, in a marketplace, each listed plugin's ``version``.
_PLUGIN_VERSION_KEY_RE = re.compile(r"^(?:version|plugins\.\d+\.version)$")
_PLUGIN_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _flatten_json(value: object, prefix: str = "") -> dict[str, object]:
    """Pure: flatten parsed JSON to ``{dotted.key: scalar}``, indexing lists."""
    if isinstance(value, dict):
        flat: dict[str, object] = {}
        for key, sub in value.items():
            flat.update(_flatten_json(sub, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(value, list):
        flat = {}
        for index, sub in enumerate(value):
            flat.update(
                _flatten_json(sub, f"{prefix}.{index}" if prefix else str(index))
            )
        return flat
    return {prefix: value}


def classify_plugin_manifest_version_only(
    changed_paths: Sequence[str],
    *,
    contents: Mapping[str, tuple[str | None, str | None]],
) -> tuple[bool, str]:
    """Pure: is this diff only a plugin manifest version bump?

    OMN-20074. omniclaude's post-merge plugin version bump (OMN-20710,
    omniclaude#2644) moves the ``version`` of ``.claude-plugin/plugin.json``
    and of the matching entry in a ``.claude-plugin/marketplace.json``, and
    nothing else. Like the pin bumps above it carries no behavioural claim, so
    no changed-file candidate is RED-derivable and evidence cannot be bound.

    ``contents`` maps each changed path to ``(head, base)`` file content.

    FAIL-CLOSED in every ambiguous direction:

    * an empty changed-file list, or more than a bounded number of files, is
      refused;
    * a path whose basename is not a plugin manifest, or whose parent
      directory is not ``.claude-plugin``, is refused;
    * a file unreadable, added or deleted at either ref, or not a JSON object,
      is refused;
    * any flattened key other than the manifest ``version`` or a marketplace
      entry's ``plugins.<n>.version`` that is added, removed or changed is
      refused;
    * every moved version must be ``MAJOR.MINOR.PATCH`` at both refs and must
      increase, and at least one version must move.
    """
    if not changed_paths:
        return False, "no changed files observed (unobservable diff, not an empty one)"
    if len(changed_paths) > MAX_PLUGIN_MANIFEST_FILES:
        return False, f"more than {MAX_PLUGIN_MANIFEST_FILES} plugin manifests changed"
    moved: list[str] = []
    for path in changed_paths:
        p = str(path)
        parts = p.split("/")
        if (
            parts[-1] not in PLUGIN_MANIFEST_BASENAMES
            or len(parts) < 2
            or parts[-2] != PLUGIN_MANIFEST_DIR
        ):
            return False, f"changed path is not a plugin manifest: {p}"
        head, base = contents.get(p, (None, None))
        if head is None or base is None:
            return False, f"plugin manifest unreadable at one or both refs: {p}"
        try:
            head_doc = json.loads(head)
            base_doc = json.loads(base)
        except json.JSONDecodeError as exc:
            return False, f"{p} does not parse as JSON: {exc}"
        if not isinstance(head_doc, dict) or not isinstance(base_doc, dict):
            return False, f"{p} is not a JSON object at one or both refs"
        head_flat = _flatten_json(head_doc)
        base_flat = _flatten_json(base_doc)
        offending = sorted(
            key
            for key in set(head_flat) | set(base_flat)
            if (
                key not in head_flat
                or key not in base_flat
                or head_flat[key] != base_flat[key]
            )
            and not _PLUGIN_VERSION_KEY_RE.match(key)
        )
        if offending:
            return False, f"{p} changes outside its version keys: " + ", ".join(
                offending
            )
        for key in sorted(set(head_flat) | set(base_flat)):
            if head_flat.get(key) == base_flat.get(key):
                continue
            new, old = head_flat.get(key), base_flat.get(key)
            m_new = _PLUGIN_SEMVER_RE.match(new) if isinstance(new, str) else None
            m_old = _PLUGIN_SEMVER_RE.match(old) if isinstance(old, str) else None
            if m_new is None or m_old is None:
                return False, f"{p} {key} is not MAJOR.MINOR.PATCH at one or both refs"
            if tuple(map(int, m_new.groups())) <= tuple(map(int, m_old.groups())):
                return False, f"{p} {key} does not increase: {old} -> {new}"
            moved.append(f"{p} {key} {old} -> {new}")
    if not moved:
        return False, "no plugin version moved"
    return True, "plugin manifest version only: " + "; ".join(moved)
