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

import re
import tomllib
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DEPENDENCY_LOCK_BASENAMES",
    "DEPENDENCY_MANIFEST_BASENAMES",
    "LOCK_FILE_SUFFIXES",
    "MAX_CHECK_VALUE_LENGTH",
    "SymbolCandidate",
    "build_content_read_check",
    "classify_dependency_pin_only",
    "declaration_count",
    "extract_lock_line_candidates",
    "extract_symbol_candidates",
    "is_shell_safe_check",
    "is_yamlfmt_stable_check",
    "render_check_value_field",
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


@dataclass(frozen=True)
class SymbolCandidate:
    """One (path, kind, symbol) triple extracted from an added diff line — pure."""

    path: str
    kind: Literal["class", "def", "lock_line"]
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


def declaration_count(
    content: str | None, kind: Literal["class", "def", "lock_line"], symbol: str
) -> int:
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
    if kind == "lock_line":
        return content.count(symbol)
    verb = "class" if kind == "class" else r"(?:async\s+def|def)"
    pattern = re.compile(rf"^\s*{verb}\s+{re.escape(symbol)}\b", re.MULTILINE)
    return len(pattern.findall(content))


def build_content_read_check(
    *,
    repo: str,
    path: str,
    kind: Literal["class", "def", "lock_line"],
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
    if kind == "lock_line":
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
    """
    for candidate in candidates:
        head_content = fetch_content(candidate.path, head_sha)
        head_count = declaration_count(head_content, candidate.kind, candidate.symbol)
        if head_count < 1:
            continue
        base_content = fetch_content(candidate.path, base_sha)
        base_count = declaration_count(base_content, candidate.kind, candidate.symbol)
        if base_count >= head_count:
            continue  # not RED-controlled: already present at base, same or more
        check = build_content_read_check(
            repo=repo,
            path=candidate.path,
            kind=candidate.kind,
            symbol=candidate.symbol,
            head_sha=head_sha,
        )
        if not is_shell_safe_check(check):
            continue  # unquotable — try the next candidate
        if accept is not None and not accept(check):
            continue  # caller-specific destination constraint — try the next
        return check
    return None


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
