# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Evidence collector — loads contract YAML, runs dod_evidence checks, returns results.

Responsibilities:
  1. Locate and load a ticket's contract YAML (auto-detect or explicit path).
  2. Iterate over ``dod_evidence[]`` items.
  3. For each item's ``checks[]``, execute the check.
  4. Return a list of ModelEvidenceCheckResult for the handler to tally.

This module is the I/O boundary — it reads files and runs subprocesses.
The handler itself remains pure (no I/O) and continues to work when callers
pre-populate evidence_results (tests, event-bus consumers).
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml
from omnibase_core.enums.ticket.enum_dod_evidence_execution_scope import (
    EnumDodEvidenceExecutionScope,
)
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
    ModelDodEvidenceGithubLookupResultEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    EnumOccRefRefreshOutcome,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_item_checks,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    apply_supersessions,
)
from omnimarket.occ_evidence_probative_class import (
    EnumEvidenceProbativeClass,
    classify_check_value,
    surrogate_refusal_reason,
)

logger = logging.getLogger(__name__)

# Default contract search roots (first match wins)
_DEFAULT_CONTRACT_ROOTS: list[str] = [
    "${ONEX_CC_REPO_PATH}/contracts",
    "${OMNI_HOME}/onex_change_control/contracts",
]

# OMN-13888 (scope 6): OCC governance is dev-targeted — contracts and receipts
# land on the OCC ``dev`` branch first and are batched to ``main`` later. The
# canonical omni_home clones track ``main``, so a contract merged only to ``dev``
# is invisible to a working-tree search (the OMN-13899 "No contract found",
# correlation 93b4e964). Resolve from this ref instead. Overridable via
# ``OCC_GOVERNANCE_REF`` for tests / operators. Mirrors
# ``DurableEvidenceGate.DEFAULT_OCC_GOVERNANCE_REF``.
_DEFAULT_OCC_GOVERNANCE_REF = "origin/dev"

# OMN-15443: ModelContractDodItem is the authoritative owner of item-level DoD
# contract fields.  The local Done-gate consumer additionally supports explicit
# PR bindings used by its live-state verifier; those fields are local execution
# metadata, not a second evidence-audience schema.  Deriving the base field set
# and default from the core model prevents this consumer from drifting when the
# canonical contract evolves.
_CANONICAL_DOD_ITEM_FIELDS = frozenset(ModelContractDodItem.model_fields)
_LOCAL_DOD_ITEM_EXTENSION_FIELDS = frozenset({"pr", "repo", "pr_number"})
_LOCAL_DOD_ITEM_FIELDS = _CANONICAL_DOD_ITEM_FIELDS | _LOCAL_DOD_ITEM_EXTENSION_FIELDS
_DEFAULT_EXECUTION_SCOPE = cast(
    EnumDodEvidenceExecutionScope,
    ModelContractDodItem.model_fields["execution_scope"].default,
)

# Wall-clock ceiling for the OCC git worktree/fetch subprocesses. A shared OCC
# clone can hit lock contention under concurrent collect() calls, or a fetch can
# stall on the network; without a timeout a stuck git op would block the whole
# collect() with no recovery (CodeRabbit — Stability). Kept generous because a
# fetch of the OCC repo may transfer real objects.
#
# OMN-16787: raised from 60 s on a measurement, not a guess. `git worktree add
# --detach origin/dev` on the live OCC repo checks out 32,382 files and takes
# ~34.5 s single-threaded; the beta sweep runs dod_verify 5-way parallel
# against the SAME clone. A 60 s ceiling therefore tripped on ordinary load,
# and — before the fail-closed rule below — that trip degraded silently into a
# stale-working-tree read reported as CONTRACT_MISSING. The ceiling has to sit
# far enough above a real cold checkout that hitting it means something is
# genuinely wrong, because hitting it now REFUSES rather than degrades.
_DEFAULT_GIT_OP_TIMEOUT_S = 300

# Operator override for the ceiling above, for hosts whose OCC clone or disk
# is slower (or faster) than the machine the default was measured on.
_GIT_OP_TIMEOUT_ENV = "DOD_VERIFY_GIT_OP_TIMEOUT_S"


def _git_op_timeout_s() -> float:
    """Resolve the git-subprocess ceiling, honouring the operator override.

    Read per call rather than captured at import so a test or an operator can
    set it without reloading the module. A malformed or negative value falls
    back to the default rather than disabling the timeout — an unbounded git
    op is the failure mode the ceiling exists to prevent.
    """
    raw = os.environ.get(_GIT_OP_TIMEOUT_ENV, "").strip()
    if not raw:
        return float(_DEFAULT_GIT_OP_TIMEOUT_S)
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using the %ss default.",
            _GIT_OP_TIMEOUT_ENV,
            raw,
            _DEFAULT_GIT_OP_TIMEOUT_S,
        )
        return float(_DEFAULT_GIT_OP_TIMEOUT_S)
    if value < 0:
        logger.warning(
            "%s=%r is negative; using the %ss default.",
            _GIT_OP_TIMEOUT_ENV,
            raw,
            _DEFAULT_GIT_OP_TIMEOUT_S,
        )
        return float(_DEFAULT_GIT_OP_TIMEOUT_S)
    return value


# OMN-15454: a failed OCC ref refresh (git fetch) used to be swallowed at
# logger.info and the collector proceeded against whatever the local
# remote-tracking ref already had, while still logging that the run resolved
# "dev-first" — a fail-open on the ONLY sanctioned Done-flip tool's evidence
# source. Default behaviour is now fail-closed: FETCH_FAILED refuses the
# whole collect() rather than silently grounding a verdict in a possibly-stale
# clone. This named, logged override is the sole documented escape hatch (per
# the ticket's fix-item 2b) — proceeding under it marks every returned check
# result un-attributable to a verified-fresh origin/dev rather than pretending
# nothing happened.
_ALLOW_STALE_OCC_REF_ENV = "DOD_VERIFY_ALLOW_STALE_OCC_REF"

# Substring git prints for the specific ref-lock race this ticket's fix-item 4
# calls out: "cannot lock ref 'refs/remotes/origin/dev': is at X but expected
# Y" under a concurrent fetch/push into the SAME OCC clone (the ordinary state
# of this repo while the merge controller runs — i.e. precisely when
# dod_verify is invoked for a Done-flip). Retriable; distinct from an offline
# host or an absent remote, which a retry cannot fix.
_REF_LOCK_ERROR_MARKER = "cannot lock ref"

# OMN-14207: live GitHub PR-state verification.
#
# A dod_evidence item that BINDS to a GitHub PR is additionally verified against
# the LIVE PR state: the PR must be MERGED and all status checks green. This runs
# ALONGSIDE the contract's declared hash/receipt checks (it never replaces them).
#
# It closes a false-positive class: a static receipt can record ``status: PASS``
# while the product PR is actually unmerged / CI-red, so the grep-the-receipt
# check passes and dod_verify reports ``verified`` for work that is neither merged
# nor green. OMN-13996 is the discovery case — ``dod_verify`` said ``verified 3/3``
# while ``omnibase_infra#2216`` was OPEN with 7 failing required checks.
#
# The binding source is authoritative, not heuristic (evidence-item ``id`` slugs
# are unreliable repo names): an explicit ``pr`` field on the item, else the
# durable receipt's ``pr_number`` + probed ``--repo owner/repo`` — the SAME fields
# the DurableEvidenceGate binds against.
_LIVE_PR_CHECK_ENV = "DOD_VERIFY_LIVE_PR_CHECK"
_DEFAULT_GITHUB_ORG = "OmniNode-ai"
# NOTE: the gh-CLI timeout and check-green-state constants formerly declared
# here (``_GH_PR_TIMEOUT_S``, ``_GH_CHECK_GREEN_STATES``) moved to
# ``handler_dod_evidence_github_effect.py`` with the subprocess calls that used
# them (OMN-14400, RSD-1 of OMN-14398).

# OMN-14637: merged-state re-anchoring of a self-referential live-PR-state gate.
#
# A contract ``command`` check that asserts its product PR is still live-OPEN —
# the canonical ``gh pr view <n> --repo <r> --json state,... --jq '.state ==
# "OPEN" and ...'`` idiom — becomes PERMANENTLY false the moment the PR
# squash-merges: GitHub flips ``.state`` to ``MERGED`` and deletes the head
# branch. The sanctioned ``dod_verify`` closeout then fails-closed forever on a
# normal, successful merge (13/26 checks failed on the OMN-11878 re-run) unless a
# human hand-authors one more "merged" superseding evidence entry in the same
# breath as the merge.
#
# When an evidence item is authoritatively bound to a CONFIRMED-MERGED PR (the
# SAME binding + live-probe machinery the OMN-14207 live-state check uses), the
# collector re-anchors ONLY the ``.state == "OPEN"`` equality to the merged
# terminal state (``.state == "MERGED"``). Every other predicate in the command
# (``.headRefOid``, ``.files``, ``.title``, ``.baseRefName``, receipt greps) still
# runs — all of which ``gh pr view`` still reports for a merged PR — so a
# genuinely-incomplete ticket (merge commit missing the expected files) STILL
# FAILS. The relaxation is therefore verification-preserving and non-vacuous, and
# it never touches an unrelated ``"OPEN"`` literal (e.g. ``.title == "OPEN"``).
_PR_OPEN_STATE_PREDICATE_RE = re.compile(r"""(\.state\s*==\s*)(["'])OPEN\2""")

# OMN-15382: authoritative (owner/repo, pr_number) extraction from a
# dod_evidence item's ``id``, when that id follows the OCC Evidence-Source
# autobind naming convention ``dod-<owner>-<repo>-pr-<number>[-suffix]``
# (see contracts generated by the OMN-13317 F1 autobind path, e.g.
# ``dod-OmniNode-ai-omnibase_infra-pr-2536``). This is the SAME binding the
# autobind tooling stamped into the id at the moment it recorded which PR the
# item covers — not a guess derived from a description slug — so it is
# trusted at the same tier as an explicit ``item.pr`` field (see
# ``_resolve_pr_bindings``). Anchored on the known GitHub org so ``owner``
# and ``repo`` split unambiguously even though the org name itself contains a
# hyphen. Returns ``("", "")`` for ids that do not follow the convention
# (e.g. ``occ-self-bind-pr-5161``, hand-authored ids) — callers MUST fall
# back to REPO/PR_NUMBER resolution in that case; this is genuinely the only
# structured per-check repo signal available before any receipt exists, so
# where it does not match, this fix does not claim to resolve cross-repo
# ambiguity (see docstring on ``_lookup_repo_for_ticket`` below).
_EVIDENCE_ID_BINDING_RE = re.compile(
    rf"^dod-(?P<owner>{re.escape(_DEFAULT_GITHUB_ORG)})-(?P<repo>[A-Za-z0-9_]+)-pr-(?P<num>\d+)(?:-.*)?$"
)

# OMN-15382 (F2): ``::pr-live-state`` binding derivation for the auto-appended
# live-PR-state check (see ``_resolve_pr_bindings`` / ``_live_pr_checks_for_item``
# below). Discovery case: a fully valid, literally-pinned item
# (``dod-omn-14968-pr-2536-rebind-15382``, check_value ``gh pr view 2536 --repo
# OmniNode-ai/omnibase_infra ...``) had its live-state check derive
# ``(OmniNode-ai/omnibase_infra, 5458)`` instead of ``(OmniNode-ai/omnibase_infra,
# 2536)`` — the receipt's ``pr_number`` field records the TICKET-CARRIER PR (the
# PR under which the receipt was authored/committed), which is NOT necessarily
# the PR any given ``check_value``/``probe_command`` field pins; the old
# repo-extraction regex ignored ``pr_number`` entirely, so it paired whichever
# ``--repo`` it found first with whatever ``pr_number`` the receipt schema
# happened to carry — a cross-field mix with no guarantee the two describe the
# same PR.
#
# ``_hardcoded_pr_bindings_in_value`` extracts a (repo, number) pair only when
# BOTH come from the SAME clause of the SAME string (never a repo from one
# ``gh pr`` invocation paired with a number from a different one, and never a
# repo from one field paired with a number from another).
_HARDCODED_PR_NUM_RE = re.compile(r"gh pr (?:view|checks|diff)\s+(\d+)\b")
_REPO_FLAG_RE = re.compile(r"--repo(?:=|\s+)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
_GH_PR_URL_RE = re.compile(r"https://github\.com/([^\s\"')]+/[^\s\"')]+)/pull/(\d+)")
# OMN-15465: the two URL forms that carry repo AND number adjacent inside a
# SINGLE token — the ``gh api`` REST path (``repos/<owner>/<repo>/pulls/<N>``)
# and the github.com web URL (``.../<owner>/<repo>/pull/<N>``). These are a
# STRICTLY STRONGER same-clause guarantee than the ``--repo`` flag form, whose
# two halves are merely co-located in one clause: here they are one contiguous
# path, so they cannot be mixed even in principle. Before this, an item like
# ``occ-self-bind-pr-5495`` — whose own check_value reads ``gh api repos/
# OmniNode-ai/onex_change_control/pulls/5495/files ...`` — pinned its PR
# perfectly and still fell through tier 2 to the receipt-carrier tier, because
# the extractor only understood ``gh pr view|checks|diff``.
_PR_PATH_URL_RE = re.compile(
    r"(?:https://github\.com/|repos/)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/pulls?/(\d+)\b"
)
# Split a check_value into clauses at shell control operators so a --repo
# belonging to a DIFFERENT gh pr invocation in the same string never pairs
# with this clause's hardcoded number.
_SHELL_CLAUSE_SPLIT_RE = re.compile(r"&&|\|\||;|\|")

# OMN-16087: pr-live-state binder must not invert an intentional non-merged
# PR-state assertion.
#
# A dod_evidence item's own ``command`` check_value can legitimately assert
# that a referenced PR is OPEN (a seam guard: "the pinned lineage deliberately
# predates unmerged PR #N") or CLOSED (a supersession note: "PR #N was closed,
# replaced by #M"). The auto-appended ``::pr-live-state`` check
# (``_live_pr_checks_for_item`` / ``_verify_live_pr``) previously derived its
# usual "must be MERGED and all required checks green" judgement from the bare
# PR reference regardless of what the item's own predicate asserted —
# inverting the entry's declared intent the moment live state disagreed with
# the binder's blanket assumption. Two live discovery cases: OMN-16077's
# ``dod-pin-is-0386-head-predating-2736`` (OPEN assertion, inverts to a false
# FAILURE once the referenced PR merges) and OMN-16142's
# ``occ-self-bind-pr-6624-superseded-note`` (CLOSED assertion, false FAILURE
# immediately since the referenced PR is never merged by design).
#
# ``_PIPELINE_SPLIT_RE`` deliberately does NOT split on a single ``|`` (unlike
# ``_SHELL_CLAUSE_SPLIT_RE`` above) — a state assertion is almost always piped
# from the ``gh pr view`` invocation through ``--jq``/``grep`` in the SAME
# logical pipeline (e.g. ``gh pr view N --repo R --json state --jq '.state' |
# grep -qx OPEN``), so keeping single pipes joined is required to see the
# assertion at all. Splitting only at ``&&``/``||``/``;``/``;;`` still keeps
# two independent ``gh pr view`` invocations in one check_value (the OMN-16142
# shape: one clause asserts #6624 CLOSED, a second `&&`-joined clause asserts
# #6626 MERGED) from being confused with each other.
_PIPELINE_SPLIT_RE = re.compile(r"&&|\|\||;;|;")
# ``.state == "OPEN"`` / ``.state == 'CLOSED'`` — the inline jq-predicate
# shape. Generalizes ``_PR_OPEN_STATE_PREDICATE_RE`` above (OMN-14637, which
# is deliberately OPEN-only and rewrites rather than detects) to also
# recognise CLOSED, since this detector only needs to recognise an assertion,
# never rewrite one.
_PR_STATE_EQUALITY_RE = re.compile(r"""\.state\s*==\s*(["'])(OPEN|CLOSED)\1""")
# ``grep -qx OPEN`` / ``grep -qx 'CLOSED'`` / ``grep -q "OPEN"`` — the
# piped-grep shape. Matches any grep-family invocation (arbitrary short
# flags, e.g. ``-qx``, ``-Eq``) whose literal argument is exactly OPEN or
# CLOSED, optionally quoted. Deliberately excludes MERGED: an assertion of
# MERGED already agrees with the binder's default assumption, so that
# binding must keep receiving the real live derivation (see
# ``_asserted_non_merged_pr_states_for_item``).
_PR_STATE_GREP_RE = re.compile(r"""grep\s+(?:-[A-Za-z]+\s+)*(["']?)(OPEN|CLOSED)\b\1""")


def _pr_binding_asserted_state(value: str, repo: str, pr_number: int) -> str | None:
    """Return ``"OPEN"``/``"CLOSED"`` when ``value`` asserts that state for
    ``(repo, pr_number)`` in the SAME pipeline segment as the PR reference,
    else ``None``.

    Same-clause-but-not-same-pipe discipline, mirroring
    ``_hardcoded_pr_bindings_in_value``'s same-clause guarantee for bindings:
    the PR reference and the state assertion must sit in one
    ``&&``/``||``/``;``-delimited pipeline (pipes within that pipeline stay
    joined — see ``_PIPELINE_SPLIT_RE``), so an assertion belonging to a
    DIFFERENT PR reference in the same check_value never attaches to this
    one. ``repo`` must already be normalized (see
    ``EvidenceCollector._normalize_repo``).

    Recognises two same-clause binding shapes, mirroring
    ``_hardcoded_pr_bindings_in_value`` (OMN-16087 follow-up: the original cut
    only recognized the first shape, so a URL-form binding's own state
    assertion silently fell through to the default merged/green derivation —
    the exact inversion this ticket exists to fix, just for a binding shape
    the first cut missed):

    * ``gh pr view <N> --repo <repo>`` — the extracted ``--repo`` flag value
      is normalized the same way before comparison;
    * ``repos/<owner>/<repo>/pulls/<N>`` or
      ``https://github.com/<owner>/<repo>/pull/<N>`` — repo and number are
      one contiguous path (:data:`_PR_PATH_URL_RE`), so no separate
      ``--repo`` flag to cross-check.

    Pure function — no I/O.
    """
    for segment in _PIPELINE_SPLIT_RE.split(value):
        matched = False

        num_match = _HARDCODED_PR_NUM_RE.search(segment)
        if num_match is not None and int(num_match.group(1)) == pr_number:
            repo_match = _REPO_FLAG_RE.search(segment)
            if repo_match is not None:
                extracted_repo = repo_match.group(1).strip()
                if "/" not in extracted_repo:
                    extracted_repo = f"{_DEFAULT_GITHUB_ORG}/{extracted_repo}"
                if extracted_repo == repo:
                    matched = True

        if not matched:
            matched = any(
                str(url_repo) == repo and int(url_num) == pr_number
                for url_repo, url_num in _PR_PATH_URL_RE.findall(segment)
            )

        if not matched:
            continue

        state_match = _PR_STATE_EQUALITY_RE.search(segment) or _PR_STATE_GREP_RE.search(
            segment
        )
        if state_match is not None:
            return state_match.group(2).upper()
    return None


def _hardcoded_pr_bindings_in_value(value: str) -> list[tuple[str, int]]:
    """Return every same-clause literal ``(repo, pr_number)`` pin in ``value``.

    Recognises two shapes, both same-clause by construction:

    * ``gh pr view|checks|diff <N> ... --repo <owner>/<repo>`` — number and
      repo flag co-located within one shell clause;
    * ``repos/<owner>/<repo>/pulls/<N>`` or
      ``https://github.com/<owner>/<repo>/pull/<N>`` — number and repo in one
      contiguous URL path (OMN-15465).

    Pure function — no I/O. See the module comment above
    ``_HARDCODED_PR_NUM_RE`` for why this must be same-clause, same-string.
    """
    bindings: list[tuple[str, int]] = []
    for clause in _SHELL_CLAUSE_SPLIT_RE.split(value):
        num_match = _HARDCODED_PR_NUM_RE.search(clause)
        repo_match = _REPO_FLAG_RE.search(clause)
        if num_match and repo_match:
            bindings.append((repo_match.group(1), int(num_match.group(1))))
        for url_repo, url_num in _PR_PATH_URL_RE.findall(clause):
            bindings.append((str(url_repo), int(url_num)))
    return bindings


# OMN-15465: PR numbers an evidence item's OWN ``id`` literally pins, e.g.
# ``occ-self-bind-pr-4711`` -> {4711}, ``dod-omn-14968-pr-2536-rebind-15382``
# -> {2536}. Ids that name no PR (``dod-deploy-assessment``) yield the empty
# set and therefore constrain nothing.
_ID_PINNED_PR_RE = re.compile(r"-pr-(\d+)(?:\b|-)")


def _pr_numbers_pinned_by_item_id(item_id: object) -> frozenset[int]:
    """Return every PR number the item id itself asserts. Pure — no I/O."""
    if not isinstance(item_id, str) or not item_id:
        return frozenset()
    return frozenset(int(n) for n in _ID_PINNED_PR_RE.findall(item_id))


def _field_confirms_pair(value: str, repo: str, pr_number: int) -> bool:
    """Whether ``value`` names BOTH ``repo`` and ``pr_number`` together.

    Used to validate a receipt-derived (repo, pr_number) candidate: the repo
    and the number must be corroborated by the SAME field text, not merely
    present somewhere in the receipt (see ``_resolve_pr_bindings``). Pure
    function — no I/O.
    """
    if repo not in value:
        return False
    return re.search(rf"\b{re.escape(str(pr_number))}\b", value) is not None


# OMN-15382: command-check fail-closed hardening.
#
# _run_command_check previously ran check_value verbatim via
# ``subprocess.run(cmd, shell=True)`` (POSIX ``sh -c``, no ``pipefail``) and
# judged success on exit code alone. Two failure modes shared one root cause
# (trusting a shell string blindly):
#
#   * ``"Recorded product receipt: docker compose ... | sha256sum"`` exits 0
#     under plain ``sh -c`` — the first pipeline stage ("Recorded" — not a
#     command) fails with 127, but a non-pipefail shell's pipeline exit code
#     is the LAST stage's, and ``sha256sum`` happily hashes empty stdin and
#     exits 0. Vacuous GREEN.
#   * The same prose with no pipe ("Recorded product receipt: uv run pytest
#     x") exits 127 — RED, but for the wrong reason (command-not-found is
#     indistinguishable from a real check failure).
#
# Fixed two ways, deliberately NOT via a blanket "empty stdout is RED" rule
# (that would break legitimate quiet checks like ``grep -q``):
#   1. Execute via ``["bash", "-o", "pipefail", "-c", cmd]`` (list form, no
#      ``shell=True``) so a failing first pipeline stage fails the whole
#      check, closing the vacuous-GREEN mechanism for genuinely command-shaped
#      pipelines.
#   2. A pre-execution shape guard (_invalid_check_value_reason) rejects
#      prose before it is ever shelled out at all, giving a distinct,
#      unambiguous reason (INVALID_CHECK_VALUE_NOT_A_COMMAND) instead of a
#      command-not-found exit code that looks like a real check failure.
_VAR_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Shell control operators that can legitimately separate an assignment (or a
# preceding pipeline stage) from the command this guard actually judges —
# e.g. ``body="$(...)" && printf '%s' "$body" | grep -qF '<marker>'`` (the
# OMN-15170 sigpipe-safe shape, OMN-15430): the first word is the whole
# ``body=$(...)`` assignment, the next is ``&&``, and only the token after
# that is a real command name. These are skipped the same way a leading
# ``VAR=VAL`` assignment is skipped — they are punctuation, not prose and
# not a command.
_SHELL_CONTROL_OPERATORS = frozenset({"&&", "||", ";", ";;", "|", "&"})

# Leading modifiers that prefix a real command without being one themselves.
# ``!`` negates the *following* pipeline and ``(`` opens a subshell, so in
# both cases the command to judge is the token AFTER them. Both used to sit
# in _SHELL_KEYWORD_ALLOWLIST, which accepted the whole check_value on sight
# and therefore let ``! Recorded receipt: ...`` through; skipping instead
# keeps the prose judgement alive one token deeper.
_SHELL_LEADING_MODIFIERS = frozenset({"!", "("})

# Shell builtins that CONSUME arbitrary bare words and still exit 0, so a
# prose check_value leading with one is a VACUOUS GREEN if the guard accepts
# it on sight (OMN-15597 R2). Measured, not assumed — under both
# ``bash 3.2.57`` (macOS) and ``bash 5.3.3``, with stdin at ``/dev/null``:
#
#     set up the runtime and verified manually   -> 0
#     export the evidence to the ticket          -> 0
#     declare victory                            -> 0
#     unset the flag manually                    -> 0
#     readonly evidence recorded                 -> 0
#     let the record show 3                      -> 0   (last operand != 0)
#     read the receipt                           -> 0   (whenever stdin has a line)
#
# These were in _SHELL_KEYWORD_ALLOWLIST, which returns None on sight BEFORE
# any prose judgement, and ``_run_command_check`` then judges by exit code —
# so each of the seven strings above flipped from a hard
# INVALID_CHECK_VALUE_NOT_A_COMMAND to ``status=verified`` on the DoD
# evidence runner, re-opening the exact class the guard exists to close
# (OMN-15382).
#
# They are handled as PREFIXES instead of as commands, which is what they
# actually are: none of the seven produces evidence of its own — they set
# variables, shell options, or read stdin — so a check_value whose only
# command is one of them proves nothing either way. The scan consumes the
# builtin and its operands up to the next control operator and keeps
# judging, exactly like the leading ``VAR=VAL`` assignment it is a longhand
# for. ``export FOO=bar && gh api ...`` therefore still resolves to ``gh``;
# ``export the evidence to the ticket`` runs out of tokens and is rejected.
#
# The rest of the builtin list below stays terminal-accepting because it was
# measured NOT to swallow prose (``cd`` -> "too many arguments" 1,
# ``eval``/``exec``/``command`` -> 127, ``type``/``hash``/``jobs``/``trap``/
# ``builtin``/``.``/``source``/``popd``/``pushd``/``unalias``/``shift``/
# ``umask``/``ulimit`` -> 1, ``local``/``return`` -> "can only be used in a
# function" 1, ``time`` -> 127, structural keywords -> syntax error 2).
# Residual, deliberately NOT closed here: a BARE ``wait`` / ``jobs`` /
# ``alias`` / ``hash`` / ``exit`` with no operands exits 0, but a one-word
# check_value is not prose and is no weaker than the already-resolvable
# ``true`` — that is the vacuous-but-command-shaped class, not this one.
_NO_EVIDENCE_BUILTIN_PREFIXES = frozenset(
    {
        "declare",
        "export",
        "let",
        "read",
        "readonly",
        "set",
        "unset",
    }
)

# The stable marker inside the "the only command is a no-evidence prefix
# builtin" rejection, and the predicate that recognises it (OMN-15597 G2).
#
# SINGLE-SOURCED ON PURPOSE. R2 introduced that rejection by REUSING the older
# "no resolvable executable token ..." message; R3 then gave it its own
# wording. One of the two consumers was updated with R3 and the other — the
# AC5 corpus census, which selected offenders by the substring
# ``"no resolvable executable token"`` — silently stopped matching its own
# class and censused nothing. A census that cannot see the class it censuses
# reports 0 for the wrong reason. Callers that need to recognise this
# rejection MUST use ``is_no_evidence_builtin_only_reason`` rather than
# re-typing a substring, so the next wording change cannot re-vacuate them.
_NO_EVIDENCE_BUILTIN_REASON_MARKER = "no-evidence shell builtin"


def is_no_evidence_builtin_only_reason(reason: str | None) -> bool:
    """True when ``reason`` is the prefix-builtin-only rejection.

    ``reason`` is a return value of :func:`_invalid_check_value_reason`.
    ``None`` (accepted) and every other rejection class — prose, a trailing
    ``':'``, an unresolvable first token, an unparseable value — are False.
    """
    return reason is not None and _NO_EVIDENCE_BUILTIN_REASON_MARKER in reason


# Shell keywords/builtins that are legitimate as the first token of a real
# command but that ``shutil.which()`` cannot resolve (they are not
# standalone executables on PATH).
#
# ADMISSION RULE (OMN-15597 R2) — a name belongs here only if NO prose-shaped
# invocation of it exits 0. The allowlist short-circuits the prose judgement,
# so any name that violates the rule is a false-GREEN path on the DoD
# evidence runner. ``tests/unit/nodes/node_dod_verify/
# test_omn_15597_command_substitution_shape_guard.py::TestAllowlistAdmissionRule``
# enforces it against a REAL shell over this exact frozenset, so a future
# addition that swallows prose fails CI rather than shipping.
_SHELL_KEYWORD_ALLOWLIST = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "function",
        "select",
        "time",
        "{",
        "[",
        "[[",
        # ``:`` is deliberately ABSENT: ``: the evidence was recorded`` exits
        # 0 under both bashes, so it violates the admission rule above. It was
        # unreachable here anyway — the ``first.endswith(":")`` prose branch
        # in _invalid_check_value_reason fires first — and listing an
        # unreachable prose-swallower only invites a reordering to make it
        # live. See TestColonIsRejectedByTheProseBranch.
        # Shell BUILTINS (OMN-15597). Without these the guard's verdict is
        # PLATFORM-DEPENDENT: macOS ships /usr/bin/cd so ``shutil.which("cd")``
        # resolves there, while Linux has no such binary — so ``(cd x && ls)``
        # passed the guard on a developer Mac and hard-REDed on a Linux CI
        # runner. 18 checks in the OCC corpus lead with ``cd`` alone. Listed
        # here are builtins that no POSIX system is required to ship as an
        # executable; ones that DO exist as binaries everywhere (``test``,
        # ``echo``, ``printf``, ``pwd``, ``true``, ``false``, ``kill``) are
        # deliberately omitted — ``shutil.which`` already resolves them, and
        # a shorter allowlist is a smaller prose-laundering surface.
        ".",
        "alias",
        "builtin",
        "cd",
        "command",
        "eval",
        "exec",
        "exit",
        "hash",
        "jobs",
        "local",
        "popd",
        "pushd",
        "return",
        "shift",
        "source",
        "trap",
        "type",
        "ulimit",
        "umask",
        "unalias",
        "wait",
    }
)

# ---------------------------------------------------------------------------
# OMN-15597: command-substitution-aware word scanning.
#
# The guard used to tokenize with ``shlex.split(cmd_str, posix=True)``.
# ``shlex`` is a WORD SPLITTER, not a shell parser: it has no notion of
# command substitution, so the double quotes *inside* a ``$(...)`` are read
# as the outer string's quotes. On
#
#     state="$(gh pr view 239 ... --jq '.state + " " + (.oid // "none")')" \
#       && test "$state" = "MERGED <sha>"
#
# the jq program's ``" "`` closes the outer ``"`` early, shlex splits INSIDE
# the substitution, and the guard judges the jq fragment
# ``" + (.oid // none)')"`` as the command name — a hard
# INVALID_CHECK_VALUE_NOT_A_COMMAND on a string bash runs to exit 0
# (OMN-15430 residual #2; 59 checks across 33 OCC contracts, census at
# onex_change_control@1e6b75f8).
#
# ``_split_shell_words`` below is the smallest thing that yields the SHELL's
# own first command word: a single left-to-right scan that tracks quoting
# with a real nesting discipline. A ``$(...)`` (or backtick) region is
# copied into the current word VERBATIM and its interior never touches the
# outer quote state — which is precisely the property shlex lacks. It is not
# a general shell parser and does not try to be: it produces words and
# control operators, which is all the shape guard consumes.
#
# ``bash -n`` was considered and rejected as the oracle: it exits 0 on
# ``Recorded product receipt: see PR 123`` too, so parse-validity cannot
# discriminate prose — which is this guard's entire purpose.
# ``\n`` is intentionally absent: an unquoted newline is a command SEPARATOR,
# not blank space, and ``_split_shell_words`` emits it as a ``;`` token
# (OMN-15597 R2). Treating it as mere whitespace here would silently join two
# commands into one token run.
_SHELL_WORD_SEPARATORS = " \t\r"

# bash metacharacters that terminate a word even without surrounding
# whitespace. Longest-match-first so ``&&`` is not lexed as two ``&``.
# ``(`` and ``)`` are here because ``(`` is a grouping operator, not part of
# the following word: without them ``([ "$x" = "OPEN" ] || ...)`` yields a
# first token of ``([`` — a fragment no author ever wrote as a command
# (contracts/OMN-9278.yaml dod-001, the last survivor of the OMN-15597
# corpus census).
_SHELL_OPERATOR_SEQUENCES: tuple[str, ...] = (
    "&&",
    "||",
    ";;",
    ";",
    "|",
    "&",
    "(",
    ")",
)

# Escapes decoded inside ``$'...'`` (ANSI-C quoting). Only the forms that can
# plausibly appear in a check_value; anything else keeps its literal
# character, which is harmless for a shape judgement.
_ANSI_C_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "e": "\x1b",
    "0": "\0",
    "\\": "\\",
    "'": "'",
    '"': '"',
}


def _scan_command_substitution(s: str, start: int) -> int:
    """Return the index of the ``)`` closing the ``$(`` / ``(`` at ``start``.

    ``s[start]`` must be the opening ``(``. Nested ``$( ... )``, nested plain
    ``( ... )``, and quoted regions inside the substitution are all tracked,
    so a ``)`` that merely sits inside a quoted string (``--jq '(.a // ")")'``)
    does not close it. Raises ``ValueError`` if the substitution is never
    closed — fail-closed, exactly as bash refuses the string.
    """
    depth = 0
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            close = s.find("'", i + 1)
            if close == -1:
                raise ValueError("unterminated single quote inside $( ... )")
            i = close + 1
            continue
        if c == '"':
            i = _scan_double_quoted(s, i)[0]
            continue
        if c == "`":
            i = _scan_backquoted(s, i)[0]
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unterminated command substitution '$('")


def _scan_backquoted(s: str, start: int) -> tuple[int, str]:
    """Scan a legacy ```...``` substitution. Returns (index after it, raw text)."""
    i = start + 1
    n = len(s)
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i + 1, s[start : i + 1]
        i += 1
    raise ValueError("unterminated backquote substitution '`'")


def _scan_double_quoted(s: str, start: int) -> tuple[int, str]:
    """Scan a ``"..."`` region. Returns (index after the closing quote, content).

    This is the load-bearing half of the OMN-15597 fix: a ``$(`` encountered
    inside the double quotes is consumed as a whole substitution and copied
    verbatim, so quotes belonging to the substitution's own interior can
    never close this string.
    """
    out: list[str] = []
    i = start + 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            return i + 1, "".join(out)
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "\n":  # line continuation
                i += 2
                continue
            if nxt in '$`"\\':  # the only escapes bash honors in "..."
                out.append(nxt)
                i += 2
                continue
            out.append(c)
            i += 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            close = _scan_command_substitution(s, i + 1)
            out.append(s[i : close + 1])
            i = close + 1
            continue
        if c == "`":
            i, raw = _scan_backquoted(s, i)
            out.append(raw)
            continue
        out.append(c)
        i += 1
    raise ValueError("unterminated double quote '\"'")


def _scan_ansi_c_quoted(s: str, start: int) -> tuple[int, str]:
    """Scan a ``$'...'`` ANSI-C quoted region. Returns (index after it, value)."""
    out: list[str] = []
    i = start + 2
    n = len(s)
    while i < n:
        c = s[i]
        if c == "'":
            return i + 1, "".join(out)
        if c == "\\" and i + 1 < n:
            out.append(_ANSI_C_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
            continue
        out.append(c)
        i += 1
    raise ValueError('unterminated ANSI-C quote "$\'"')


def _split_shell_words(s: str) -> list[str]:
    """Split ``s`` into shell words and control operators.

    Quote-removing like ``shlex.split(..., posix=True)``, but correct about
    command substitution: the text of a ``$(...)`` or ```...``` is kept
    verbatim in the word that contains it and never alters the surrounding
    quote state. Control operators (``&&``, ``||``, ``;``, ``;;``, ``|``,
    ``&``) are emitted as their own tokens so the caller can skip them.

    Raises ``ValueError`` on a string the shell itself could not parse
    (unbalanced quote, unterminated substitution) — and only on those: a
    rejection bash would not make is a false RED, which is the defect class
    this function exists to close.
    """
    words: list[str] = []
    buf: list[str] = []
    started = False  # distinguishes an empty word ('' / "") from no word
    i = 0
    n = len(s)

    def flush() -> None:
        nonlocal started
        if started:
            words.append("".join(buf))
            buf.clear()
            started = False

    while i < n:
        c = s[i]
        if c == "\n":
            # An UNQUOTED newline is a command SEPARATOR in shell, not
            # whitespace (OMN-15597 R2, CodeRabbit). Emitting it as ``;``
            # matters because the caller skips a
            # ``_NO_EVIDENCE_BUILTIN_PREFIXES`` builtin's operands only up to
            # the next control operator: without this,
            # ``export FOO=bar\ngh api ...`` would have ``gh api ...``
            # consumed as operands of ``export`` and be rejected as having no
            # resolvable executable — a NEW false RED of exactly the class
            # this ticket closes. A newline INSIDE quotes or a ``$(...)`` is
            # untouched: those regions are consumed by their own scanners
            # before reaching here, and a backslash-newline continuation is
            # handled below and is NOT a separator.
            flush()
            words.append(";")
            i += 1
            continue
        if c in _SHELL_WORD_SEPARATORS:
            flush()
            i += 1
            continue
        if c == "\\":
            if i + 1 >= n:
                # Line continuation with nothing after it. bash accepts this
                # (``bash -n -c 'echo hi \'`` exits 0), so refusing it here
                # would be a new false RED of exactly the class this ticket
                # closes.
                break
            if s[i + 1] == "\n":  # line continuation
                i += 2
                continue
            buf.append(s[i + 1])
            started = True
            i += 2
            continue
        if c == "'":
            close = s.find("'", i + 1)
            if close == -1:
                raise ValueError('unterminated single quote "\'"')
            buf.append(s[i + 1 : close])
            started = True
            i = close + 1
            continue
        if c == '"':
            i, content = _scan_double_quoted(s, i)
            buf.append(content)
            started = True
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "'":
            i, content = _scan_ansi_c_quoted(s, i)
            buf.append(content)
            started = True
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            close = _scan_command_substitution(s, i + 1)
            buf.append(s[i : close + 1])
            started = True
            i = close + 1
            continue
        if c == "`":
            i, raw = _scan_backquoted(s, i)
            buf.append(raw)
            started = True
            continue
        operator = next(
            (op for op in _SHELL_OPERATOR_SEQUENCES if s.startswith(op, i)), None
        )
        if operator is not None:
            flush()
            words.append(operator)
            i += len(operator)
            continue
        buf.append(c)
        started = True
        i += 1

    flush()
    return words


def _invalid_check_value_reason(cmd_str: str, *, cwd: str | None = None) -> str | None:
    """Return a reason string when ``cmd_str`` looks like prose, not a command.

    Tokenizes with ``_split_shell_words`` (OMN-15597) — a
    command-substitution-aware scan that yields the SHELL's own first
    command word. Its predecessor, ``shlex.split(cmd_str, posix=True)``, is
    a word splitter with no notion of ``$(...)``: on
    ``state="$(gh pr view N --jq '.state + " " + (.oid // "none")')" && test
    ...`` the jq program's inner ``"`` closed the outer quote early and a jq
    fragment was judged as the command name — a false
    ``INVALID_CHECK_VALUE_NOT_A_COMMAND`` on a string bash runs to exit 0.

    Strips leading ``VAR=VAL`` assignment tokens, leading shell control
    operators (``&&``, ``||``, ``;``, ``;;``, ``|``, ``&`` — punctuation
    that can legitimately separate an assignment from the command this
    guard judges), a leading ``!`` negation, and any
    ``_NO_EVIDENCE_BUILTIN_PREFIXES`` builtin together with its operands
    (``set``/``export``/``declare``/``unset``/``readonly``/``let``/``read``
    — longhand assignments that produce no evidence and that a shell runs to
    exit 0 over arbitrary prose words, OMN-15597 R2). It then inspects the
    first remaining token: if it ends with ``:`` (e.g. a stray
    ``"Recorded:"`` label) or cannot be resolved and is not a known shell
    keyword, this is prose that must never be shelled out. Returns ``None``
    when the shape looks like a real command — this is a pure shape check;
    it never executes anything and never judges by output content.

    A check_value whose ONLY command is one of those prefix builtins
    (``unset FOO``, ``read -r a b <<< "$(gh api ...)"``) is therefore
    rejected — correct, not a regression: such a value proves nothing, and
    accepting it is indistinguishable from accepting ``unset the flag
    manually``. The reason names that builtin explicitly rather than
    blaming "leading VAR=VAL assignments/shell operators", which for those
    two inputs is a construct that is not present (OMN-15597 R3).

    A first token that *is* or *contains* a command substitution
    (``$(...)`` / ```...```) is accepted: what it expands to is unknowable
    statically, but a substitution is command-shaped by construction and no
    prose sample carries one. Bare ``$VAR`` expansion in command position is
    deliberately NOT covered here — that is OMN-15267's separate class.

    If ``cmd_str`` cannot be tokenized at all (unbalanced quote,
    unterminated substitution), ``_split_shell_words`` raises
    ``ValueError`` — treated as fail-closed INVALID: a check_value
    bash itself cannot parse is genuinely invalid, never silently passed
    through. Note the converse does NOT hold, which is why ``bash -n`` is
    not used as the oracle: prose parses clean under ``bash -n``.

    ``cwd`` is the check's OMN-10078-resolved working directory (or
    ``None`` to inherit the caller's cwd). A first token containing a path
    separator (e.g. ``"./verify.sh"``, ``"scripts/run.sh"``) is a
    relative-script invocation, not a PATH lookup: ``shutil.which`` never
    resolves those against a caller-supplied ``cwd`` (it only ever inspects
    this process's actual working directory), so it is checked directly
    against ``cwd`` (or the process cwd when ``cwd`` is ``None``) instead of
    going through ``shutil.which``.
    """
    stripped = cmd_str.strip()
    if not stripped:
        return "empty command"
    try:
        tokens = _split_shell_words(stripped)
    except ValueError as exc:
        return (
            f"command could not be parsed as shell syntax ({exc}) — this "
            "looks like prose, not a command"
        )
    if not tokens:
        return "empty command"
    idx = 0
    consumed_builtin: str | None = None
    while idx < len(tokens):
        token = tokens[idx]
        if (
            _VAR_ASSIGNMENT_RE.match(token)
            or token in _SHELL_CONTROL_OPERATORS
            or token in _SHELL_LEADING_MODIFIERS
        ):
            idx += 1
            continue
        if token in _NO_EVIDENCE_BUILTIN_PREFIXES:
            # Longhand for a leading VAR=VAL assignment: consume the builtin
            # AND its operands, then keep judging whatever follows the next
            # control operator (OMN-15597 R2). Accepting it on sight is what
            # let ``set up the runtime and verified manually`` reach the
            # shell and exit 0.
            consumed_builtin = token
            idx += 1
            while idx < len(tokens) and tokens[idx] not in _SHELL_CONTROL_OPERATORS:
                idx += 1
            continue
        break
    if idx >= len(tokens):
        # Name the construct that actually ran the tokens out (OMN-15597 R3).
        # These two cases are reached by DIFFERENT inputs and a single message
        # misdescribes one of them: ``read -r a b <<< "$(gh api ...)"`` has no
        # assignment and no operator, so blaming "leading VAR=VAL
        # assignments/shell operators" names a construct that is not present —
        # the same misidentification failure this ticket exists to close, just
        # on the rejection path instead of the acceptance path.
        if consumed_builtin is not None:
            return (
                f"the only command is the {_NO_EVIDENCE_BUILTIN_REASON_MARKER} "
                f"{consumed_builtin!r} and its operands — its operands, "
                "including any redirection/herestring/process-substitution "
                "operands, are consumed with it, and no ';'/'&&'/'||'"
                "-separated command follows. Such a value proves nothing, and "
                f"its shape is indistinguishable from prose leading with "
                f"{consumed_builtin!r}"
            )
        return (
            "command has no resolvable executable token after leading "
            "VAR=VAL assignments, shell control operators and '!'/'(' "
            "modifiers"
        )
    first = tokens[idx]
    if "$(" in first or "`" in first:
        return None
    if first.endswith(":"):
        return f"first token {first!r} looks like prose, not a command (ends with ':')"
    if first in _SHELL_KEYWORD_ALLOWLIST:
        # OMN-16752 — `[[` is admitted ONLY when its conditional is actually
        # closed. Every other name in the allowlist earns its place because a
        # prose-shaped invocation is a syntax error (exit 2) on every bash we
        # run, which is the admission rule stated above the frozenset. `[[` was
        # admitted on that same "structural keywords -> syntax error 2" premise,
        # and the premise is FALSE on bash 5.2:
        #
        #   bash 5.3.9 (macOS `.200`):     [[ the evidence was recorded  -> 2
        #   bash 5.2.15 (Linux .201 gate-  [[ the evidence was recorded  -> 0
        #     runner container)             (prints "conditional binary
        #                                    operator expected", exits 0 anyway)
        #
        # Measured on both hosts 2026-08-27; `[`, `{`, `if`, `for` and `case`
        # exit 2 on BOTH, so `[[` is the only affected name. Because
        # ``_run_command_check`` execs a PATH-resolved bare ``bash``, that made
        # ``[[ <prose>`` a real vacuous-GREEN path on any host whose bash is
        # 5.2.x — the exact false-GREEN class this guard exists to close, just
        # reachable only on some hosts, which is why it survived review.
        #
        # Requiring the closing ``]]`` is version-INDEPENDENT (it never consults
        # the running bash) and strictly stronger than the old behavior: a real
        # conditional such as ``[[ -f out.txt ]] && grep -q marker out.txt``
        # still resolves, while an unterminated ``[[`` — which is malformed
        # shell on every bash, whatever it exits — is judged as the prose it is.
        if first == "[[" and "]]" not in tokens[idx:]:
            return (
                "first token '[[' opens a conditional that is never closed "
                "with ']]' — this looks like prose, not a command (an "
                "unterminated '[[' exits 0 under bash 5.2, so accepting it "
                "would be a vacuous GREEN)"
            )
        return None
    if os.sep in first or (os.altsep and os.altsep in first):
        base = Path(cwd) if cwd else Path.cwd()
        candidate = base / first if not os.path.isabs(first) else Path(first)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return None
        return (
            f"first token {first!r} is not a resolvable executable relative "
            f"to cwd {str(base)!r} — this looks like prose, not a command"
        )
    if shutil.which(first) is not None:
        return None
    return (
        f"first token {first!r} is not a resolvable executable or a "
        "recognized shell keyword — this looks like prose, not a command"
    )


# ---------------------------------------------------------------------------
# OMN-15382 (runner-supersession follow-up): contract-entry supersession.
#
# The runner previously had ZERO handling of the ``evidence_artifact:
# "supersedes_dod_evidence:<id>"`` marker (the only supersession code,
# ``durable_evidence_gate.apply_supersessions``, reads a DIFFERENT surface —
# receipt files, not contract entries). A dod_verify run therefore executed
# every original item even when a later append-only entry in the SAME
# contract declared it superseded, and an original whose check_value had
# been intentionally retired (e.g. the ``occ-self-bind-pr-<n>`` /
# ``dod-...`` rebind idiom this ticket's own base commit produced) reported
# a hard FAIL instead of being recognized as superseded.
#
# Marker syntax and append-only ("target must already exist") semantics
# mirror onex_change_control's authoring-time lint (Rule B companion) and
# CI compliance runner field-for-field — see
# ``onex_change_control/scripts/lint_contract_check_values.py::_superseded_dod_ids``
# and
# ``onex_change_control/src/onex_change_control/scripts/contract_compliance_check.py::_superseded_dod_ids``/``_supersedes_marker``:
# both key off an item-level (not check-level) string field
# ``evidence_artifact`` whose value is the literal prefix
# ``"supersedes_dod_evidence:"`` followed by the superseded item's ``id``.
#
# OMN-15390: the ORDERING rule is part of that parity, not an optional
# refinement. ``_superseded_dod_ids`` supersedes a target only when the
# target id is already in ``seen`` — i.e. ONLY a LATER item may retire an
# EARLIER one, which is what makes the idiom append-only. This runner
# reproduces that rule exactly (see ``_resolve_supersessions``), so the set
# of superseded ids the two consumers compute is identical for every input;
# ``tests/fixtures/dod_supersession/parity_corpus.yaml`` is the shared
# artifact that asserts it case-for-case against OCC's own function.
# Resolving against the whole-contract id set instead (position-blind) would
# let a FORWARD marker retire the newest, most-correct entry in the runner
# while the OCC gate still executed it — a runner-more-permissive-than-gate
# divergence on the only sanctioned Done-flip path.
#
# The parity above is on the superseded SET ONLY, and it is unconditional:
# the marker is resolved BEFORE any judgement about the CARRYING item's own
# ``id``, because ``_superseded_dod_ids`` evaluates ``if supersedes in seen``
# whether or not the carrier has a usable id. An earlier revision of this
# runner gated on the carrier's id first and so MISSED four non-canonical
# shapes the gate honours (carrier with no ``id`` key, ``id: ""``, ``id: 7``,
# ``id: None``) — runner-STRICTER-than-gate, which is this ticket's original
# bug class re-created, and silent besides. Hence ``superseded`` is keyed to
# the superseder's INDEX and ``malformed`` to the carrier's INDEX; ids are
# used for display only.
#
# On the shapes where a marker resolves to NOTHING this runner is deliberately
# STRICTER than the OCC-side scripts. Those scripts simply no-op there (they
# are advisory lint/compliance surfaces); here supersession DELETES a FAILED
# verdict, so a marker that silently did nothing would leave an author
# believing a contract was repaired when it was not. Each of these is a hard
# RED on the entry CARRYING the marker — never on the target, never a silent
# skip, and reported even when the carrier has no usable id:
#   * "dangling" — the target id exists nowhere in the contract (typo, or the
#     target item was removed);
#   * "forward" — the target exists but is declared LATER, which supersedes
#     nothing under the ordering rule above;
#   * "self-reference" — an item naming its own id.
# The superseded SET is unaffected by these diagnostics, so set-parity with
# OCC holds: in every such case both consumers agree nothing was superseded.
#
# Resolution itself is a single forward pass, so IT always terminates. The
# relation it produces, however, is NOT acyclic — do not rely on that. Edges
# point backwards by INDEX, but ``superseded`` is keyed by ID, and ids are not
# unique in a contract. When an EARLIER item already declared the carrier's own
# id, ``target in seen`` is true for the carrier's own id and the recorded edge
# is an ID-LEVEL SELF-LOOP: ``superseded['dod-0'] == 1`` while
# ``id_at[1] == 'dod-0'``. Chains (A retired by B, B retired by C) are legal and
# terminate at C, which is the item that actually proves something — but the
# chain WALK (``_terminal_superseder``) is what has to cope with the self-loop,
# and it does so with a visited-set guard that is load-bearing, not decorative.
# See ``_terminal_superseder``; measurements are in its docstring.
#
# This is a consequence of matching the gate exactly (OMN-15390 R1):
# ``_superseded_dod_ids`` has no self-reference branch, so a self-referential
# marker on a duplicate id IS an accepted edge there too. Diverging here to keep
# the relation acyclic would re-create the runner-stricter-than-gate bug class
# this ticket exists to kill.
#
# WELL-FORMED IS NOT SUFFICIENT (OMN-15390 anti-laundering). Resolution says
# which edges are legal; it does NOT say which ones fire. An edge retires its
# target only when the item that ultimately carries the verdict is itself
# VERIFIED — see ``_supersession_is_in_effect``. A superseder that declares
# ``checks: []``, skips, or fails retires nothing, and its target is executed
# normally with the rejection stated on the result. Without that condition,
# appending one evidence-free marker item flips a FAIL receipt to PASS on the
# only sanctioned Done-flip path.
#
# A superseded item's checks are not executed and no ``::pr-live-state``
# check is appended for it (see ``_collect_impl``).
# ---------------------------------------------------------------------------

_SUPERSEDES_DOD_EVIDENCE_PREFIX = "supersedes_dod_evidence:"


def _supersedes_marker(value: object) -> str | None:
    """Return the superseded id an ``evidence_artifact`` names, else ``None``.

    Line-for-line mirror of
    ``contract_compliance_check._supersedes_marker`` (and the identical copy
    in ``lint_contract_check_values``): a non-string, a string without the
    exact prefix, or an empty/whitespace-only payload is NOT a marker. Kept
    as its own function so the parity differential in
    ``tests/unit/nodes/node_dod_verify/test_omn_15390_contract_entry_supersession.py``
    can compare it against OCC's directly. Pure — no I/O.
    """
    if not isinstance(value, str):
        return None
    if not value.startswith(_SUPERSEDES_DOD_EVIDENCE_PREFIX):
        return None
    superseded = value[len(_SUPERSEDES_DOD_EVIDENCE_PREFIX) :].strip()
    return superseded or None


@dataclass(frozen=True)
class _SupersessionResolution:
    """Result of resolving a contract's ``supersedes_dod_evidence`` markers.

    ``superseded`` maps a superseded item's id -> the INDEX of the LATER item
    that supersedes it; ``set(superseded)`` is exactly the set
    ``contract_compliance_check._superseded_dod_ids`` computes, for every
    input. The value is an index rather than an id because the OCC gate
    honours a marker regardless of whether its CARRYING item has a usable
    ``id``, so the superseder is not always nameable — but it must still be
    identifiable, both for the audit message and for the effectiveness check
    in ``_collect_impl``.

    ``malformed`` maps an item's INDEX -> a fail-closed reason string for a
    marker on THAT item that resolved to nothing (dangling target, forward
    reference, or self-reference). Indexed for the same reason: keying by id
    made a broken marker on an id-less item a SILENT no-op (OMN-15390
    remediation). A marker is never both superseding and malformed, since the
    two outcomes are exclusive branches of the same resolution step.
    """

    superseded: dict[str, int] = field(default_factory=dict)
    malformed: dict[int, str] = field(default_factory=dict)


class EvidenceCollector:
    """Loads a ticket contract and runs dod_evidence checks.

    Usage::

        collector = EvidenceCollector()
        results = collector.collect("OMN-9414")
        # results: list[ModelEvidenceCheckResult]
    """

    def __init__(self, timeout_per_check: int = 30) -> None:
        self._timeout = timeout_per_check
        # When set (during a dev-resolved collect), an origin/dev worktree of the
        # OCC repo. Contract-load AND the shell greps run inside it so dev-only
        # contracts + receipts are visible (OMN-13888 scope 6).
        self._occ_dev_root: str | None = None
        self._occ_governance_ref = (
            os.environ.get("OCC_GOVERNANCE_REF", _DEFAULT_OCC_GOVERNANCE_REF).strip()
            or _DEFAULT_OCC_GOVERNANCE_REF
        )
        # OMN-15382: the dod_evidence item id currently being checked, set by
        # ``_check_evidence_item`` before it runs that item's checks. Read by
        # ``_lookup_pr_for_ticket`` / ``_lookup_repo_for_ticket`` to recover an
        # authoritative (owner/repo, pr_number) binding from the id when it
        # follows the autobind naming convention, instead of guessing via an
        # unscoped gh search. Transient, per-item state (mirrors the existing
        # ``_occ_dev_root`` pattern) — never leaks across items or tickets.
        self._current_evidence_item_id: str | None = None
        # Set immediately after a failed _lookup_pr_for_ticket /
        # _lookup_repo_for_ticket call so _resolve_command_placeholders can
        # surface the specific fail-closed reason (PR_LOOKUP_AMBIGUOUS, etc.)
        # instead of a generic "cannot resolve" message.
        self._last_pr_lookup_error: str | None = None
        self._last_repo_lookup_error: str | None = None
        # OMN-15382 (F2): set by ``_resolve_pr_bindings`` when it found NO
        # trustworthy binding but SOME evidence the item is PR-related (a PASS
        # receipt recording a pr_number that could not be consistently paired
        # with a repo — see the fail-closed rewrite's module comment above
        # that method). Read by ``_live_pr_checks_for_item`` to surface a
        # visible SKIPPED note instead of silently omitting the live-state
        # check. Reset per item (mirrors ``_current_evidence_item_id``).
        self._last_binding_note: str | None = None
        # OMN-16788: set by ``_fetch_pr_checks_green`` from the EFFECT
        # handler's classification of WHY the required-context set was
        # unreadable, when that reason is a credential fact (HTTP 403 on the
        # branch-protection endpoint, or a bare HTTP 404 for a repo outside
        # the App installation) rather than a substantive one. Read by
        # ``_verify_live_pr``, which resets it before each fetch.
        #
        # A side channel rather than a wider return type because the two
        # fetch wrappers are the documented seam a dozen existing suites
        # monkeypatch with two-tuple stubs; a stub that never sets this
        # attribute leaves it None and therefore keeps its pre-OMN-16788
        # semantics exactly. Mirrors ``_last_binding_note`` above.
        self._last_checks_unreachable_cause: EnumEvidenceUnverifiableCause | None = None
        # OMN-15454 AC2: provenance of the OCC governance ref actually read by
        # the most recent auto-resolved collect() call. Populated by
        # ``collect()`` before it returns; read by ``handler_dod_verify`` to
        # stamp ``ModelDodVerifyState``. None when collect() was called with
        # an explicit contract_path (no OCC auto-resolution happened at all).
        self.occ_refresh_outcome: EnumOccRefRefreshOutcome | None = None
        self.occ_resolved_sha: str | None = None

    @property
    def occ_governance_ref(self) -> str:
        """The OCC governance ref this collector resolves against (e.g. ``origin/dev``)."""
        return self._occ_governance_ref

    @staticmethod
    def _github_lookup_result(
        output: ModelHandlerOutput[None],
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        """Type-narrow HandlerDodEvidenceGithubEffect's single emitted event.

        ``ModelHandlerOutput.events`` is ``tuple[Any, ...]`` (the generic
        dispatch-engine shape); this handler always emits exactly one
        ``ModelDodEvidenceGithubLookupResultEvent`` per call (OMN-14400).
        """
        return cast(ModelDodEvidenceGithubLookupResultEvent, output.events[0])

    def collect(
        self,
        ticket_id: str,
        contract_path: str | None = None,
    ) -> list[ModelEvidenceCheckResult]:
        """Load contract and run all dod_evidence checks.

        When no explicit ``contract_path`` is given, OCC governance is dev-first
        (contracts and receipts land on the OCC ``dev`` branch first and are
        batched to ``main`` later; the canonical clones track ``main``). Per the
        OMN-13888 scope-6 decision of record, an auto-detected OCC contract is
        ALWAYS resolved from an ``origin/dev`` worktree when that worktree can be
        materialised and carries the contract — even when a (possibly STALE) copy
        exists on the ``main``-tracking working tree. This closes the round-1
        residual edge where a stale ``main`` copy was used as-is because the
        contract was merely *present* on the working tree so the rider never
        fired. The working tree is used only as a fallback when the contract is
        ABSENT on dev — never when dev could not be READ. The worktree is
        removed before returning.

        OMN-16787: "could not be read" and "is not there" are different facts
        and must not share an outcome. ``_materialize_occ_dev_worktree`` has
        two failure classes; OMN-15454 closed only the fetch one. A failed or
        timed-out ``git worktree add`` used to fall through to the
        ``main``-tracking working tree while the run still stamped
        ``occ_governance_ref: origin/dev`` — and because OCC ``dev`` runs
        thousands of commits ahead of ``main``, a dev-only contract was then
        invisible and the run reported ``CONTRACT_MISSING``. Both classes now
        refuse by default, under the same named override.
        """
        if contract_path is not None:
            return self._collect_impl(ticket_id, contract_path)

        created_worktree: Path | None = None
        try:
            dev_root, created_worktree, refresh_outcome, resolved_sha = (
                self._materialize_occ_dev_worktree()
            )
            self.occ_refresh_outcome = refresh_outcome
            self.occ_resolved_sha = resolved_sha

            # OMN-15454: fail-closed by default. A failed refresh must not
            # silently yield an "origin/dev-resolved" verdict — the local
            # clone content at that point is UNKNOWN freshness, and UNKNOWN
            # must never read as fresh. The only sanctioned continuation is
            # the named, logged override below, which marks every returned
            # result un-attributable rather than pretending nothing happened.
            allow_stale = os.environ.get(
                _ALLOW_STALE_OCC_REF_ENV, ""
            ).strip().lower() in (
                "1",
                "true",
                "yes",
            )
            if (
                refresh_outcome is EnumOccRefRefreshOutcome.FETCH_FAILED
                and not allow_stale
            ):
                logger.error(
                    "Refusing to resolve %s for %s: OCC ref refresh failed and "
                    "%s is not set. Set %s=1 to proceed anyway (results will be "
                    "marked un-attributable to a verified-fresh origin/dev).",
                    self._occ_governance_ref,
                    ticket_id,
                    _ALLOW_STALE_OCC_REF_ENV,
                    _ALLOW_STALE_OCC_REF_ENV,
                )
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="occ_ref_refresh",
                        description=(
                            f"OCC ref refresh failed for {self._occ_governance_ref}"
                        ),
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=(
                            "OCC_REF_REFRESH_FAILED: git fetch of "
                            f"{self._occ_governance_ref} failed (after retrying "
                            "the ref-lock race once) — the local clone's "
                            "freshness is UNKNOWN, so this run refuses rather "
                            f"than resolving evidence against it. Set "
                            f"{_ALLOW_STALE_OCC_REF_ENV}=1 to override."
                        ),
                    )
                ]

            # OMN-16787: the second failure class, on the same terms. An OCC
            # root WAS resolvable (a fetch was attempted, so refresh_outcome is
            # not None) but the worktree could not be materialised — the
            # `git worktree add` failed or, in production, timed out. Falling
            # through here reads the `main`-tracking working tree while the
            # run reports `occ_governance_ref: origin/dev`, which is the lie
            # this refusal exists to stop.
            #
            # The `refresh_outcome is not None` guard is load-bearing: when no
            # OCC root resolves at all no fetch is attempted, and that is a
            # legitimate pre-existing shape (the caller's working-tree search)
            # rather than a fault. It keeps its existing reporting.
            worktree_unavailable = dev_root is None and refresh_outcome is not None
            if worktree_unavailable and not allow_stale:
                logger.error(
                    "Refusing to resolve %s for %s: the %s worktree could not "
                    "be materialised (git worktree add failed or timed out at "
                    "%ss) and %s is not set. Falling back to the working tree "
                    "would ground the verdict in a clone that tracks main.",
                    self._occ_governance_ref,
                    ticket_id,
                    self._occ_governance_ref,
                    _git_op_timeout_s(),
                    _ALLOW_STALE_OCC_REF_ENV,
                )
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="occ_worktree_unavailable",
                        description=(
                            "OCC worktree could not be materialised at "
                            f"{self._occ_governance_ref}"
                        ),
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=(
                            "OCC_WORKTREE_UNAVAILABLE: git worktree add of "
                            f"{self._occ_governance_ref} failed or timed out "
                            f"(ceiling {_git_op_timeout_s()}s, override with "
                            f"{_GIT_OP_TIMEOUT_ENV}). The only remaining "
                            "source is the working tree, which tracks main and "
                            "is therefore NOT the ref this run reports — so "
                            "this run refuses instead of silently reporting "
                            "the contract missing. Set "
                            f"{_ALLOW_STALE_OCC_REF_ENV}=1 to proceed against "
                            "the working tree anyway (every result is then "
                            "marked un-attributable)."
                        ),
                    )
                ]

            if dev_root is not None:
                dev_candidate = Path(dev_root) / "contracts" / f"{ticket_id}.yaml"
                if dev_candidate.exists():
                    # dev is authoritative — prefer it over any working-tree copy.
                    self._occ_dev_root = dev_root
                    logger.info(
                        "Resolved OCC contract for %s from %s worktree at %s "
                        "(dev-first, overrides any main working-tree copy; "
                        "refresh=%s, resolved_sha=%s)",
                        ticket_id,
                        self._occ_governance_ref,
                        dev_root,
                        refresh_outcome.value if refresh_outcome else None,
                        resolved_sha,
                    )
                elif self._find_contract(ticket_id) is None:
                    logger.info(
                        "Contract %s absent on %s and on the working tree; "
                        "collect will report it missing",
                        ticket_id,
                        self._occ_governance_ref,
                    )
            results = self._collect_impl(ticket_id, contract_path)
            if refresh_outcome is EnumOccRefRefreshOutcome.FETCH_FAILED:
                # allow_stale is True here (the refusal branch above already
                # returned otherwise). Disclosed, not buried: every result
                # this run produced is marked un-attributable, plus a
                # standalone item names the override explicitly.
                results = [
                    result.model_copy(
                        update={
                            "message": (
                                f"{result.message or ''} "
                                "[OMN-15454: UNATTRIBUTABLE — OCC ref refresh "
                                f"failed; {_ALLOW_STALE_OCC_REF_ENV} override "
                                "active, verdict not grounded in a verified-"
                                "fresh origin/dev]"
                            ).strip()
                        }
                    )
                    for result in results
                ]
                results.append(
                    ModelEvidenceCheckResult(
                        evidence_id="occ_ref_refresh_override",
                        description="OCC ref refresh failure — override active",
                        status=EnumEvidenceCheckStatus.SKIPPED,
                        message=(
                            f"{_ALLOW_STALE_OCC_REF_ENV} was set; every check "
                            "result above is un-attributable to a verified-"
                            f"fresh {self._occ_governance_ref}."
                        ),
                    )
                )
            elif worktree_unavailable:
                # OMN-16787, same disclosure contract as the fetch-failure
                # override above: proceeding is allowed, pretending is not.
                # These results came from the working tree, which tracks main.
                results = [
                    result.model_copy(
                        update={
                            "message": (
                                f"{result.message or ''} "
                                "[OMN-16787: UNATTRIBUTABLE — the "
                                f"{self._occ_governance_ref} worktree could "
                                "not be materialised; "
                                f"{_ALLOW_STALE_OCC_REF_ENV} override active, "
                                "this result was read from the working tree, "
                                "which tracks main]"
                            ).strip()
                        }
                    )
                    for result in results
                ]
                results.append(
                    ModelEvidenceCheckResult(
                        evidence_id="occ_worktree_unavailable_override",
                        description=(
                            "OCC worktree materialisation failure — override active"
                        ),
                        status=EnumEvidenceCheckStatus.SKIPPED,
                        message=(
                            f"{_ALLOW_STALE_OCC_REF_ENV} was set; every check "
                            "result above was resolved from the working tree, "
                            f"not from {self._occ_governance_ref}."
                        ),
                    )
                )
            return results
        finally:
            self._occ_dev_root = None
            if created_worktree is not None:
                self._remove_occ_dev_worktree(created_worktree)

    def _resolve_occ_root(self) -> Path | None:
        """Return the OCC repo root from the environment, or None."""
        cc_repo_path = os.environ.get("ONEX_CC_REPO_PATH", "").strip()
        if cc_repo_path and Path(cc_repo_path).is_dir():
            return Path(cc_repo_path)
        omni_home = os.environ.get("OMNI_HOME", "").strip()
        if omni_home:
            occ = Path(omni_home) / "onex_change_control"
            if occ.is_dir():
                return occ
        return None

    def _materialize_occ_dev_worktree(
        self,
    ) -> tuple[str | None, Path | None, EnumOccRefRefreshOutcome | None, str | None]:
        """Add a detached worktree of the OCC repo at ``self._occ_governance_ref``.

        Returns ``(worktree_path_str, worktree_path, refresh_outcome,
        resolved_sha)``. ``refresh_outcome`` is ``None`` only when no fetch
        was even attempted — no OCC root resolvable at all (a legitimate,
        pre-existing case: the caller falls back to the working-tree contract
        search) — never as a stand-in for a failed attempt; a real attempt
        always yields ``FETCHED`` / ``FETCH_FAILED`` / ``NOT_APPLICABLE``. The
        first two return values are ``(None, None)`` when the worktree could
        not be materialised at all (the ``git worktree add`` itself
        failed/timed out) — a different failure class from a refresh outcome
        of ``FETCH_FAILED``, where the worktree DOES materialise, just against
        whatever the local remote-tracking ref already had. ``resolved_sha``
        is the worktree HEAD's 40-char commit SHA (OMN-15454 AC2 provenance)
        when a worktree was created, else ``None``. The worktree is placed
        under ``OMNI_HOME`` (when set) so relative ``file_exists`` checks stay
        inside the containment boundary.
        """
        occ = self._resolve_occ_root()
        if occ is None:
            return None, None, None, None
        # Refresh the remote-tracking ref first so a long-lived OMNI_HOME clone
        # does not materialise a STALE origin/dev and miss the very contract this
        # rider exists to pick up (CodeRabbit — Data Integrity). The outcome is
        # now consumed by the caller (OMN-15454) rather than discarded — a
        # failed refresh no longer silently grounds a verdict in the local
        # clone as if it were fresh.
        refresh_outcome = self._refresh_occ_ref(occ)
        omni_home = os.environ.get("OMNI_HOME", "").strip()
        parent = Path(omni_home) if omni_home and Path(omni_home).is_dir() else None
        try:
            tmp = Path(tempfile.mkdtemp(prefix=".occ-dev-wt-", dir=parent))
        except OSError:
            return None, None, refresh_outcome, None
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(occ),
                    "worktree",
                    "add",
                    "--detach",
                    "--force",
                    str(tmp),
                    self._occ_governance_ref,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_git_op_timeout_s(),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Timed out materialising %s worktree of OCC after %ss",
                self._occ_governance_ref,
                _git_op_timeout_s(),
            )
            shutil.rmtree(tmp, ignore_errors=True)
            return None, None, refresh_outcome, None
        if proc.returncode != 0:
            logger.warning(
                "Could not materialise %s worktree of OCC: %s",
                self._occ_governance_ref,
                proc.stderr.strip(),
            )
            shutil.rmtree(tmp, ignore_errors=True)
            return None, None, refresh_outcome, None
        resolved_sha = self._resolve_worktree_head_sha(tmp)
        return str(tmp), tmp, refresh_outcome, resolved_sha

    def _resolve_worktree_head_sha(self, worktree: Path) -> str | None:
        """Return the 40-char commit SHA the worktree actually checked out.

        OMN-15454 AC2: "attribution must name what was actually read, not
        what was intended." Best-effort — a failure here does not roll back
        the worktree add; it only means provenance is unavailable, which the
        caller surfaces as ``None`` rather than fabricating a value.
        """
        try:
            proc = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_git_op_timeout_s(),
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        sha = proc.stdout.strip()
        return sha or None

    def _run_occ_fetch(
        self, occ: Path, remote: str, branch: str
    ) -> tuple[EnumOccRefRefreshOutcome, str]:
        """Run one ``git fetch`` attempt. Pure I/O helper, no retry logic."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(occ), "fetch", "--quiet", remote, branch],
                capture_output=True,
                text=True,
                check=False,
                timeout=_git_op_timeout_s(),
            )
        except subprocess.TimeoutExpired:
            return EnumOccRefRefreshOutcome.FETCH_FAILED, "timed out"
        if proc.returncode == 0:
            return EnumOccRefRefreshOutcome.FETCHED, ""
        return EnumOccRefRefreshOutcome.FETCH_FAILED, proc.stderr.strip()

    def _refresh_occ_ref(self, occ: Path) -> EnumOccRefRefreshOutcome:
        """``git fetch`` the OCC governance ref's remote branch — typed outcome.

        OMN-15454: previously logged a failure at ``logger.info`` and returned
        ``None`` unconditionally, which every caller discarded — the worktree
        add proceeded against the local clone regardless, while ``collect()``
        still logged that the run resolved "dev-first". Callers now consume
        this typed outcome and decide explicitly rather than continuing on a
        swallowed failure.

        Only fires for a ``<remote>/<branch>`` ref (e.g. ``origin/dev``); a bare
        local-branch ref (test override, AC4) has no remote and is
        ``NOT_APPLICABLE`` — that path is unchanged.

        The specific ``cannot lock ref ... is at X but expected Y`` race
        (fix-item 4) is a symptom of concurrent mutation of the SAME OCC
        clone — the *normal* state of this repo while the merge controller
        runs, i.e. precisely when a Done-flip is attempted — and is retried
        once. Any other failure (offline, no remote) is not retried; a second
        attempt cannot fix those.
        """
        ref = self._occ_governance_ref
        if "/" not in ref:
            return EnumOccRefRefreshOutcome.NOT_APPLICABLE
        remote, branch = ref.split("/", 1)
        outcome, stderr = self._run_occ_fetch(occ, remote, branch)
        if outcome is EnumOccRefRefreshOutcome.FETCHED:
            return outcome
        if _REF_LOCK_ERROR_MARKER in stderr:
            logger.info(
                "OCC ref refresh (git fetch %s %s) hit a ref-lock race; "
                "retrying once: %s",
                remote,
                branch,
                stderr,
            )
            outcome, stderr = self._run_occ_fetch(occ, remote, branch)
            if outcome is EnumOccRefRefreshOutcome.FETCHED:
                return outcome
        logger.warning(
            "OCC ref refresh (git fetch %s %s) failed: %s",
            remote,
            branch,
            stderr,
        )
        return EnumOccRefRefreshOutcome.FETCH_FAILED

    def _remove_occ_dev_worktree(self, worktree: Path) -> None:
        occ = self._resolve_occ_root()
        if occ is not None:
            try:
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(occ),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_git_op_timeout_s(),
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Timed out removing OCC worktree %s after %ss",
                    worktree,
                    _git_op_timeout_s(),
                )
                proc = None
            # A failed/timed-out `worktree remove` leaves a stale registration
            # under .git/worktrees/ even after rmtree deletes the directory;
            # prune it so the shared OCC clone does not accumulate dead entries
            # (CodeRabbit — Stability).
            if proc is None or proc.returncode != 0:
                if proc is not None:
                    logger.warning(
                        "git worktree remove failed for %s: %s; pruning registration",
                        worktree,
                        proc.stderr.strip(),
                    )
                self._prune_occ_worktrees(occ)
        shutil.rmtree(worktree, ignore_errors=True)

    def _prune_occ_worktrees(self, occ: Path) -> None:
        """Best-effort ``git worktree prune`` to clear stale registrations."""
        try:
            subprocess.run(
                ["git", "-C", str(occ), "worktree", "prune"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_git_op_timeout_s(),
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timed out pruning OCC worktrees under %s", occ)

    def _collect_impl(
        self,
        ticket_id: str,
        contract_path: str | None = None,
    ) -> list[ModelEvidenceCheckResult]:
        """Load contract and run all dod_evidence checks (worktree-agnostic core).

        Args:
            ticket_id: Linear ticket ID (e.g. OMN-1234).
            contract_path: Explicit path to contract YAML. If None, auto-detect.

        Returns:
            One ModelEvidenceCheckResult per dod_evidence item.
        """
        if contract_path is not None:
            path = Path(contract_path)
            if not path.exists():
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="contract",
                        description=f"Contract file not found: {contract_path}",
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=f"File does not exist: {contract_path}",
                    )
                ]
        else:
            found = self._find_contract(ticket_id)
            if found is None:
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="contract",
                        description=f"No contract found for {ticket_id}",
                        status=EnumEvidenceCheckStatus.SKIPPED,
                        message=(
                            f"Searched: {_DEFAULT_CONTRACT_ROOTS}. "
                            "Provide --contract-path or generate a contract."
                        ),
                    )
                ]
            path = found

        raw = self._load_yaml(path)
        if raw is None:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Failed to parse contract: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=f"YAML parse error in {path}",
                )
            ]

        # Validate contract belongs to the requested ticket
        contract_ticket_id = raw.get("ticket_id")
        if contract_ticket_id != ticket_id:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Contract ticket mismatch: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=(
                        f"Expected ticket_id {ticket_id!r}, "
                        f"found {contract_ticket_id!r}."
                    ),
                )
            ]

        dod_items = raw.get("dod_evidence", [])
        if not isinstance(dod_items, list):
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Invalid dod_evidence structure in contract: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message="dod_evidence must be a list of mappings.",
                )
            ]
        if not dod_items:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"No dod_evidence entries in contract: {path}",
                    status=EnumEvidenceCheckStatus.SKIPPED,
                    message="Contract has empty or missing dod_evidence[] section.",
                )
            ]

        # OMN-15443: validate the complete contract's execution audience before
        # resolving supersessions or running ANY declared/local-GitHub effect.
        # A later malformed item must not allow an earlier valid sibling to run.
        audience_failures = self._validate_evidence_audiences(dod_items)
        if audience_failures:
            return audience_failures

        supersession = self._resolve_supersessions(dod_items)

        # OMN-15390 (remediation): resolving the markers only says which EDGES
        # are well-formed. Whether an edge actually RETIRES its target is a
        # second, stricter question — it depends on the superseding item's own
        # executed verdict, which is not known until that item runs. Hence two
        # phases: run every item that is not a supersession target, then let
        # each target's terminal superseder decide whether the target is
        # retired or executed normally. See ``_supersession_is_in_effect``.
        target_ids = set(supersession.superseded)
        id_at: dict[int, str | None] = {
            index: (
                item.get("id")
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                else None
            )
            for index, item in enumerate(dod_items)
        }

        # OMN-15390 (residual R2): an entry the receipt cannot REPRESENT must
        # fail closed with a verdict, never abort the run. ``evidence_id`` is
        # typed ``str`` on ``ModelEvidenceCheckResult``, so a contract carrying
        # ``id: 7`` or ``id: null`` (or a ``dod_evidence`` element that is not a
        # mapping at all) raised an unhandled ``pydantic.ValidationError`` /
        # ``AttributeError`` straight out of ``collect()``: the process died and
        # NO receipt was written at all, on the only sanctioned Done-flip path.
        # No receipt is strictly worse than a FAIL receipt — nothing downstream
        # can even see that the contract was rejected. Rejected rather than
        # coerced to a positional label: the ``id`` is what binds an entry to
        # its evidence, so silently renaming a schema-invalid entry would let a
        # malformed contract keep producing PASS receipts.
        unrepresentable: dict[int, str] = {}
        for index, item in enumerate(dod_items):
            if not isinstance(item, dict):
                unrepresentable[index] = (
                    f"MALFORMED_EVIDENCE_ITEM: dod_evidence[{index}] is "
                    f"{type(item).__name__}, not a mapping. Every dod_evidence "
                    "entry must be a mapping with an 'id' and 'checks'. The "
                    "entry was not executed and cannot pass."
                )
                continue
            if "id" in item and not isinstance(item["id"], str):
                unrepresentable[index] = (
                    f"MALFORMED_EVIDENCE_ID: dod_evidence[{index}] declares "
                    f"id={item['id']!r} ({type(item['id']).__name__}), but a "
                    "dod_evidence id must be a string. The entry was not "
                    "executed and cannot pass. Fix the contract."
                )

        # Phase 1 — execute everything that is neither malformed nor a target.
        executed: dict[int, list[ModelEvidenceCheckResult]] = {}
        for index, item in enumerate(dod_items):
            if index in supersession.malformed or index in unrepresentable:
                continue
            if (id_at[index] or None) in target_ids:
                continue
            executed[index] = self._execute_item(item, ticket_id, path, index)

        # Phase 2 — an edge takes effect only if the item that ultimately
        # carries the verdict proved something in its own right.
        in_effect: dict[str, int] = {}
        for target_id in supersession.superseded:
            carrier = self._terminal_superseder(
                target_id, supersession.superseded, id_at
            )
            if self._supersession_is_in_effect(executed.get(carrier)):
                in_effect[target_id] = carrier

        # Phase 3 — emit one result group per item, in declaration order.
        results: list[ModelEvidenceCheckResult] = []
        for index, item in enumerate(dod_items):
            item_id = item.get("id") if isinstance(item, dict) else None
            item_id_str = item_id if isinstance(item_id, str) and item_id else None
            description = (
                str(item.get("description", item_id_str or "unknown"))
                if isinstance(item, dict)
                else "unknown"
            )

            # OMN-15390 (residual R2): an entry whose id the receipt cannot
            # represent is reported against its POSITION and never executed.
            # Checked ahead of the marker diagnostics because it is the more
            # fundamental defect; a marker fault on the same entry is appended
            # so neither is lost.
            unrepresentable_reason = unrepresentable.get(index)
            if unrepresentable_reason is not None:
                also = supersession.malformed.get(index)
                results.append(
                    ModelEvidenceCheckResult(
                        evidence_id=f"dod_evidence[{index}]",
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=(
                            f"{unrepresentable_reason} {also}"
                            if also is not None
                            else unrepresentable_reason
                        ),
                    )
                )
                continue

            # OMN-15708: an item's OWN retirement (is it validly superseded by
            # a LATER, verified item?) is checked BEFORE its own marker's
            # validity. A malformed marker on THIS item (dangling/forward/
            # self-referential — see below) only matters if this item is not
            # itself retired: an item that some other, well-formed, verified
            # marker successfully supersedes is SUPERSEDED regardless of
            # whether ITS OWN outbound marker (if any) was well-formed. Before
            # this reorder, an item carrying a malformed outbound marker (e.g.
            # OMN-15374's OCC#6080 comma-joined
            # ``supersedes_dod_evidence:<a>,<b>,<c>``, which resolves to one
            # nonexistent target id -> DANGLING_SUPERSESSION) hard-FAILED
            # unconditionally even when a LATER, separate, single-id marker
            # (OCC#6084) validly targeted and retired it — no append-only
            # repair could ever reach the now-unreachable retirement branch
            # below. The marker parser itself is unchanged: a comma-joined
            # marker still resolves to a single bogus id and a NON-superseded
            # carrier still hard-fails via the malformed-reason branch further
            # down — only the carrying item's OWN retirement path changed.
            carrier_index = (
                in_effect.get(item_id_str) if item_id_str is not None else None
            )
            if carrier_index is not None:
                # OMN-15382: an item a LATER, VERIFIED item in this contract
                # supersedes is not executed and gets no ::pr-live-state check
                # — its checks are preserved for audit while the superseding
                # item's checks carry the verdict.
                #
                # OMN-15708: a retirement is never a QUIET skip of this item's
                # OWN marker defect (the property the pre-15708 ordering
                # protected). If this item's own outbound marker was ALSO
                # malformed, that fact is folded into the SUPERSEDED message
                # rather than dropped — the item is still legitimately
                # retired (its own marker's validity is irrelevant to whether
                # SOMETHING ELSE validly retires it), but the audit trail
                # keeps naming the defect instead of erasing it.
                own_marker_defect = supersession.malformed.get(index)
                results.append(
                    ModelEvidenceCheckResult(
                        evidence_id=item_id_str,
                        description=description,
                        status=EnumEvidenceCheckStatus.SUPERSEDED,
                        message=(
                            f"SUPERSEDED by "
                            f"{self._carrier_label(carrier_index, id_at)!r} "
                            f"(evidence_artifact: "
                            f"'{_SUPERSEDES_DOD_EVIDENCE_PREFIX}{item_id_str}'); "
                            "not re-executed — preserved for audit."
                            + (
                                f" NOTE: this item's own outbound "
                                f"evidence_artifact marker was ALSO malformed "
                                f"({own_marker_defect}) — irrelevant to this "
                                "retirement (a marker's validity does not "
                                "affect whether the item carrying it can "
                                "itself be superseded), recorded so the "
                                "defect is not silently dropped."
                                if own_marker_defect is not None
                                else ""
                            )
                        ),
                    )
                )
                continue

            # OMN-15382: a malformed marker (dangling, forward, or
            # self-referential target) hard-fails the ITEM CARRYING the marker
            # — it is neither executed nor treated as a clean supersession.
            # Keyed by INDEX (OMN-15390 remediation) so a marker on an item
            # with a missing/empty/non-string ``id`` is still reported rather
            # than silently dropped. Reached only when this item is NOT itself
            # validly superseded (see OMN-15708 comment above).
            malformed_reason = supersession.malformed.get(index)
            if malformed_reason is not None:
                results.append(
                    ModelEvidenceCheckResult(
                        evidence_id=item_id_str or f"dod_evidence[{index}]",
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=malformed_reason,
                    )
                )
                continue

            group = executed.get(index)
            if group is None:
                # A declared target whose supersession did NOT take effect:
                # execute it now so its own verdict stands, and say loudly why
                # the marker did not retire it (OMN-15390 remediation — the
                # anti-laundering rule). Never a silent pass.
                group = self._execute_item(item, ticket_id, path, index)
                if group:
                    carrier = self._terminal_superseder(
                        item_id_str or "", supersession.superseded, id_at
                    )
                    group[0] = group[0].model_copy(
                        update={
                            "message": (
                                "SUPERSESSION_NOT_IN_EFFECT: a later item "
                                f"({self._carrier_label(carrier, id_at)}) declares "
                                f"'{_SUPERSEDES_DOD_EVIDENCE_PREFIX}{item_id_str}' "
                                "but did not verify in its own right — a "
                                "superseder that declares no checks, skips, or "
                                "fails retires NOTHING — so this entry was "
                                f"executed normally. {group[0].message}"
                            )
                        }
                    )
            results.extend(group)

        return results

    @staticmethod
    def _validate_evidence_audiences(
        dod_items: list[Any],
    ) -> list[ModelEvidenceCheckResult]:
        """Validate every item-level execution audience before any effect.

        The authoritative core model owns the item field set and default; the
        authoritative enum owns the accepted audience values.  This boundary is
        deliberately scoped to item-level structure and ``execution_scope`` so
        the local collector's established check aliases and explicit PR-binding
        extensions remain backward compatible.  Unknown item fields still fail
        loud, which prevents a misspelled ``execution_scope`` key from silently
        taking the canonical default.
        """
        failures: list[ModelEvidenceCheckResult] = []
        allowed_values = ", ".join(
            scope.value for scope in EnumDodEvidenceExecutionScope
        )

        for index, item in enumerate(dod_items):
            fallback_id = f"dod_evidence[{index}]"
            if not isinstance(item, dict):
                failures.append(
                    ModelEvidenceCheckResult(
                        evidence_id=fallback_id,
                        description="Invalid DoD evidence item",
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=(
                            "MALFORMED_EVIDENCE_ITEM: item must be a mapping; "
                            "refusing to execute any evidence check from an "
                            "audience-ambiguous contract."
                        ),
                    )
                )
                continue

            raw_id = item.get("id")
            evidence_id = raw_id if isinstance(raw_id, str) and raw_id else fallback_id
            description = str(item.get("description", evidence_id))

            unknown_fields = [key for key in item if key not in _LOCAL_DOD_ITEM_FIELDS]
            if unknown_fields:
                rendered = ", ".join(
                    f"{key!r}={item[key]!r}"
                    for key in sorted(unknown_fields, key=lambda value: repr(value))
                )
                failures.append(
                    ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=(
                            "INVALID_DOD_EVIDENCE_ITEM: strict canonical field "
                            f"set rejected unknown field(s): {rendered}; refusing "
                            "to execute any evidence check from this contract."
                        ),
                    )
                )
                continue

            raw_scope = item.get("execution_scope", _DEFAULT_EXECUTION_SCOPE)
            try:
                EnumDodEvidenceExecutionScope(raw_scope)
            except (TypeError, ValueError):
                failures.append(
                    ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=(
                            "UNKNOWN_EXECUTION_SCOPE: item "
                            f"{evidence_id!r} declares execution_scope={raw_scope!r}; "
                            f"allowed values: {allowed_values}. Refusing to execute "
                            "with an ambiguous evidence audience."
                        ),
                    )
                )

        return failures

    def _execute_item(
        self,
        item: Any,
        ticket_id: str,
        path: Path | None,
        index: int,
    ) -> list[ModelEvidenceCheckResult]:
        """Execute one dod_evidence item and return its full result group.

        The group is the item's own check result followed by any OMN-14207
        live-PR-state checks: verify the LIVE PR state for a PR-bound item and
        emit it ALONGSIDE the item's declared checks, so a static
        ``status: PASS`` receipt can no longer mask an unmerged or CI-red
        product PR. Group element 0 is always the item's own result.

        OMN-15390 (residual R2): the whole group is executed inside a
        fail-CLOSED boundary. ``_collect_impl`` rejects the two malformed
        shapes we know about up front, but an unforeseen one must not be able
        to abort ``collect()`` either — an exception escaping here means the
        run dies with NO receipt at all, which reads downstream as "the
        verification never happened" rather than "the verification refused".
        Anything unexpected is therefore converted into a receipted FAILED
        entry naming the item's position. ``BaseException`` (KeyboardInterrupt,
        SystemExit) is deliberately NOT caught.
        """
        try:
            results = [self._check_evidence_item(item, ticket_id, path)]
            if isinstance(item, dict):
                results.extend(self._live_pr_checks_for_item(item, ticket_id, path))
            return self._demote_non_probative(item, results)
        except Exception as exc:
            item_id = item.get("id") if isinstance(item, dict) else None
            label = item_id if isinstance(item_id, str) and item_id else None
            logger.exception(
                "dod_evidence[%d] of %s raised while executing; failing closed",
                index,
                ticket_id,
            )
            return [
                ModelEvidenceCheckResult(
                    evidence_id=label or f"dod_evidence[{index}]",
                    description=label or f"dod_evidence[{index}]",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=(
                        f"EVIDENCE_ITEM_ERROR: executing dod_evidence[{index}] "
                        f"raised {type(exc).__name__}: {exc}. The entry cannot "
                        "pass. This is a fail-closed conversion — the run still "
                        "produces a receipt instead of aborting without one."
                    ),
                )
            ]

    @staticmethod
    def _non_probative_reason(item: Any) -> str | None:
        """Reason this item cannot bear a verdict, or ``None`` if it can.

        OMN-15391. An item is non-probative only when EVERY check it declares
        is a command whose exit status is invariant over the product diff (see
        ``omnimarket.occ_evidence_probative_class``). One probative check makes
        the whole item probative: an item whose checks must ALL pass carries a
        real verdict as soon as one of them can go red for a product reason.

        ``file_exists`` is always probative — a file is present or it is not,
        and that is a fact about the tree under test. An item declaring no
        checks at all is not classified here; ``_check_evidence_item`` already
        SKIPs it, and a SKIP is not a green that needs demoting.
        """
        if not isinstance(item, dict):
            return None
        checks = item.get("checks")
        if not isinstance(checks, list) or not checks:
            return None

        reasons: list[str] = []
        for check in checks:
            if not isinstance(check, dict):
                return None
            if check.get("check_type") not in ("command", "test_passes"):
                return None
            # The EFFECTIVE command, resolved exactly as ``_run_command_check``
            # resolves it (``command`` first, ``check_value`` as fallback).
            # Reading only ``check_value`` would let a check spelled with the
            # ``command`` key execute as a surrogate while classifying as
            # probative — a complete bypass of this refusal, since its green
            # would still count. Found by CodeRabbit on omnimarket#2168.
            check_value = check.get("command") or check.get("check_value")
            probative_class = classify_check_value(
                check_value if isinstance(check_value, str) else None
            )
            if probative_class is EnumEvidenceProbativeClass.PROBATIVE:
                return None
            reasons.append(surrogate_refusal_reason(probative_class, str(check_value)))
        return " ".join(reasons)

    def _demote_non_probative(
        self, item: Any, results: list[ModelEvidenceCheckResult]
    ) -> list[ModelEvidenceCheckResult]:
        """Reclassify an item's GREENS when the item cannot bear a verdict.

        OMN-15391 — the load-bearing refusal, and the reason it is applied here
        rather than before execution.

        **Only a VERIFIED result is demoted.** A FAILED one is left exactly as
        it was, and the checks are still EXECUTED rather than short-circuited.
        That is deliberate: it makes this change monotone toward refusal — it
        can subtract a green and it can never manufacture one, so no contract
        that is red today can go green because of it. Skipping execution would
        have been cheaper, but it would silently convert a genuine red (a PR
        the token cannot see, a foreign suite that is actually broken) into a
        non-verdict, which is a loosening in the one direction that matters.

        The item's OMN-14207 ``::pr-live-state`` legs are demoted with it.
        Those legs assert the bound PR is merged with green CI — provenance
        about the very PR the surrogate names, not about the behaviour claimed.
        Leaving them VERIFIED would defeat the whole refusal: an all-surrogate
        contract would still read green on its live-state legs alone (measured
        on ``contracts/OMN-16667.yaml``: 5 declared surrogate checks plus 4
        passing live-state legs). Their anti-laundering force is untouched —
        a live-state leg that goes RED still fails the contract.
        """
        reason = self._non_probative_reason(item)
        if reason is None:
            return results
        return [
            result.model_copy(
                update={
                    "status": EnumEvidenceCheckStatus.NON_PROBATIVE,
                    "message": (
                        f"{reason}"
                        + (
                            f" (check output: {result.message})"
                            if result.message
                            else ""
                        )
                    ),
                }
            )
            if result.status is EnumEvidenceCheckStatus.VERIFIED
            else result
            for result in results
        ]

    @staticmethod
    def _carrier_label(index: int, id_at: dict[int, str | None]) -> str:
        """Human label for the item at ``index`` — its id, else its position."""
        return id_at.get(index) or f"dod_evidence[{index}]"

    @staticmethod
    def _terminal_superseder(
        target_id: str,
        superseded: dict[str, int],
        id_at: dict[int, str | None],
    ) -> int:
        """Follow a supersession chain to the item that carries the verdict.

        With ``A`` superseded by ``B`` and ``B`` in turn superseded by ``C``,
        the OCC gate retires both ``A`` and ``B`` and ``C`` is what actually
        proves anything — so ``C``'s verdict, not ``B``'s, decides whether
        ``A``'s edge takes effect.

        THE ``visited`` GUARD IS LOAD-BEARING — it is the only thing that
        terminates this walk, not a defensive backstop. Do not remove it.

        Edges point strictly backwards by INDEX, but ``superseded`` is keyed by
        ID and ids are not unique in a contract, so the walked relation is NOT
        acyclic. A duplicate id admits an id-level SELF-LOOP: for
        ``[{id: dod-0}, {id: dod-0, evidence_artifact:
        'supersedes_dod_evidence:dod-0'}]`` resolution records
        ``superseded['dod-0'] == 1`` while ``id_at[1] == 'dod-0'``, so
        ``index -> superseded[id_at[index]]`` maps 1 to 1 forever. Delete the
        guard and this function hangs on that input (verified: an unguarded
        walk did not terminate in 10_000 steps).

        Measured over the parity domain in
        ``test_omn_15390_contract_entry_supersession.py``: the guard fires on
        **176 of 296 edges (59.5%)** of ``_duplicate_id_contracts()`` and on
        **0 of 75** edges of the unique-id ``_small_contracts()`` — i.e. it is
        dead only on the domain the pre-OMN-15390-R1 code was tested against.
        ``test_the_visited_set_guard_is_required_not_defensive`` pins this.

        Returning the revisited index is the fail-safe answer: the caller
        (``_collect_impl`` phase 2) looks that index up in ``executed``, a
        self-looping carrier is itself a supersession target so it was never
        executed, ``_supersession_is_in_effect(None)`` is False, and the edge
        does not fire — both entries execute normally and carry their own
        verdicts.
        """
        index = superseded[target_id]
        visited = {index}
        while True:
            carrier_id = id_at.get(index)
            if carrier_id is None or carrier_id not in superseded:
                return index
            following = superseded[carrier_id]
            if following in visited:
                return index
            visited.add(following)
            index = following

    @staticmethod
    def _supersession_is_in_effect(
        carrier_group: list[ModelEvidenceCheckResult] | None,
    ) -> bool:
        """True only if the superseding item proved something in its own right.

        OMN-15390 anti-laundering, and the reason a well-formed marker is not
        sufficient on its own: supersession may remove a FALSE red, never
        manufacture a green. Appending ONE marker item that declares no passing
        check of its own (``checks: []``, a check that skips, or a check that
        fails) would otherwise retire a genuinely-failing entry, drop
        ``failed`` to 0, and land as a PASS receipt on the only sanctioned
        Done-flip path — strictly worse than the FAIL it replaced. A repair
        must carry its own proof, so the carrier's own result must be VERIFIED
        and nothing in its group may have FAILED.
        """
        if not carrier_group:
            return False
        if carrier_group[0].status is not EnumEvidenceCheckStatus.VERIFIED:
            return False
        return not any(
            result.status is EnumEvidenceCheckStatus.FAILED for result in carrier_group
        )

    @staticmethod
    def _resolve_supersessions(dod_items: list[Any]) -> _SupersessionResolution:
        """Resolve ``evidence_artifact: "supersedes_dod_evidence:<id>"`` markers.

        Pure function — no I/O. See the module-level comment above
        :class:`_SupersessionResolution` for the marker syntax and the
        append-only ORDERING rule this mirrors field-for-field from
        onex_change_control's lint/compliance scripts, plus the fail-closed
        diagnostics (dangling / forward / self target) this runner adds
        beyond those advisory scripts without changing the superseded set.

        Single forward pass in declaration order, mirroring
        ``_superseded_dod_ids``'s ``seen``/``if supersedes in seen`` loop, so
        THIS function terminates in O(len(dod_items)) unconditionally.

        The relation it RETURNS is not acyclic, though: edges point backwards
        by index while ``superseded`` is keyed by id, so a duplicate id admits
        an id-level self-loop. Any consumer that WALKS the relation needs a
        cycle guard — see ``_terminal_superseder``.
        """
        # Pre-pass: every id in the contract, used ONLY to tell a forward
        # reference (target exists, declared later) apart from a dangling one
        # (target exists nowhere). Neither is a supersession.
        all_ids: set[str] = set()
        for item in dod_items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                all_ids.add(item_id)

        seen: set[str] = set()
        superseded: dict[str, int] = {}
        malformed: dict[int, str] = {}

        for index, item in enumerate(dod_items):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            item_id_str = item_id if isinstance(item_id, str) and item_id else None
            label = item_id_str or f"dod_evidence[{index}]"

            # OMN-15390 (remediation): the marker is read and resolved BEFORE
            # any judgement about the CARRYING item's own id, because
            # ``_superseded_dod_ids`` evaluates ``if supersedes in seen``
            # unconditionally. Gating on the carrier's id first (the earlier
            # shape of this loop) made the runner MISS supersessions the gate
            # honours whenever the carrier's ``id`` was missing, empty,
            # non-string or null — leaving the runner STRICTER than the gate
            # and re-creating this ticket's original bug class, silently.
            target = _supersedes_marker(item.get("evidence_artifact"))

            if target is not None:
                if target in seen:
                    # The OCC rule verbatim: a LATER item retires an EARLIER
                    # one. Recorded by INDEX, not id, so an id-less carrier is
                    # still an identifiable superseder for the effectiveness
                    # check in ``_collect_impl``.
                    #
                    # OMN-15390 (residual R1): this test runs FIRST, ahead of
                    # the self-reference test below, because that is the order
                    # ``_superseded_dod_ids`` evaluates in — it has no
                    # self-reference branch at all, only ``if supersedes in
                    # seen``. A self-reference is normally inert BECAUSE
                    # ``seen.add`` happens after the marker check, so the
                    # carrier's own id is not yet in ``seen``. But when an
                    # EARLIER item already declared the same id (a duplicate-id
                    # contract), ``seen`` DOES contain it and the gate retires
                    # that earlier entry. Testing self-reference first made the
                    # runner hard-RED a carrier the gate treats as a valid
                    # superseder — runner-STRICTER-than-gate on the Done-flip
                    # path, this ticket's original bug class, on 144 of the 544
                    # contracts in the duplicate-id domain.
                    superseded[target] = index
                elif target == item_id_str:
                    malformed[index] = (
                        "MALFORMED_SUPERSESSION: item "
                        f"{label!r} declares evidence_artifact "
                        f"'{_SUPERSEDES_DOD_EVIDENCE_PREFIX}{target}', which "
                        "supersedes itself and therefore retires nothing. Fix "
                        "the marker before this item can execute."
                    )
                elif target in all_ids:
                    malformed[index] = (
                        "FORWARD_SUPERSESSION: item "
                        f"{label!r} declares evidence_artifact "
                        f"'{_SUPERSEDES_DOD_EVIDENCE_PREFIX}{target}' but "
                        f"{target!r} is declared LATER in this contract. "
                        "Supersession is append-only — only a later item may "
                        "retire an earlier one — so this marker retires "
                        "nothing. Move the repair below the entry it replaces."
                    )
                else:
                    malformed[index] = (
                        "DANGLING_SUPERSESSION: item "
                        f"{label!r} declares evidence_artifact "
                        f"'{_SUPERSEDES_DOD_EVIDENCE_PREFIX}{target}' but no "
                        f"dod_evidence item with id {target!r} exists in this "
                        "contract. Fix the marker (typo, or the target item "
                        "was removed) before this item can execute."
                    )

            # Mirrors ``_superseded_dod_ids`` exactly, including its acceptance
            # of the empty string and its placement AFTER the marker check
            # (which is what makes a self-reference match nothing in either
            # consumer).
            if isinstance(item_id, str):
                seen.add(item_id)

        return _SupersessionResolution(superseded=superseded, malformed=malformed)

    def _find_contract(self, ticket_id: str) -> Path | None:
        """Search standard locations for a ticket contract."""
        # OMN-13888 (scope 6): a materialised origin/dev worktree wins so a
        # dev-only contract resolves instead of falling through to "No contract".
        if self._occ_dev_root:
            dev_candidate = Path(self._occ_dev_root) / "contracts" / f"{ticket_id}.yaml"
            if dev_candidate.exists():
                logger.info("Found contract at %s (dev worktree)", dev_candidate)
                return dev_candidate
        for root_template in _DEFAULT_CONTRACT_ROOTS:
            root = Path(os.path.expandvars(root_template))
            candidate = root / f"{ticket_id}.yaml"
            if candidate.exists():
                logger.info("Found contract at %s", candidate)
                return candidate

        # Fallback: resolve via OMNI_HOME env var
        omni_home = os.environ.get("OMNI_HOME", "")
        candidate = (
            Path(omni_home) / "onex_change_control" / "contracts" / f"{ticket_id}.yaml"
        )
        if candidate.exists():
            logger.info("Found contract at %s", candidate)
            return candidate

        return None

    def _load_yaml(self, path: Path) -> dict[str, Any] | None:
        """Load and return YAML content, or None on error."""
        try:
            content = path.read_text(encoding="utf-8")
            raw = yaml.safe_load(content)
            if not isinstance(raw, dict):
                logger.error("Contract %s root is not a mapping", path)
                return None
            return raw
        except Exception as exc:
            logger.error("Failed to parse %s: %s", path, exc)
            return None

    def _check_evidence_item(
        self,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None = None,
    ) -> ModelEvidenceCheckResult:
        """Run checks for a single dod_evidence item."""
        evidence_id = item.get("id", "unknown")
        description = item.get("description", evidence_id)
        checks = item.get("checks", [])

        # OMN-15382: publish this item's id for _lookup_pr_for_ticket /
        # _lookup_repo_for_ticket to consult (see _current_evidence_item_id
        # docstring in __init__). Reset per item so it never leaks.
        self._current_evidence_item_id = (
            evidence_id if isinstance(evidence_id, str) else None
        )

        if not isinstance(checks, list):
            return ModelEvidenceCheckResult(
                evidence_id=evidence_id,
                description=description,
                status=EnumEvidenceCheckStatus.FAILED,
                message="checks must be a list of mappings.",
            )

        # OMN-15911: what this item BINDS, decided from the commands it
        # declares rather than from its prose or its exit code. Resolved once,
        # before execution, and stamped on whichever verdict this item reaches
        # — a FAILED behavior check is still a behavior check, and the
        # ``behavior_proving_count`` roll-up (which requires VERIFIED) is what
        # keeps a failure from releasing a flip.
        item_proof_class = classify_item_checks(checks)

        if not checks:
            return ModelEvidenceCheckResult(
                evidence_id=evidence_id,
                description=description,
                status=EnumEvidenceCheckStatus.SKIPPED,
                message="No checks defined for this evidence item.",
                proof_class=item_proof_class,
            )

        # OMN-14637: when this evidence item is authoritatively bound to a PR that
        # GitHub now confirms MERGED, relax any live-OPEN-state gate in its command
        # checks to the merged terminal state, so the sanctioned closeout does not
        # fail-closed forever on a normal, successful merge (branch deleted +
        # ``.state`` flipped to MERGED). Resolved ONCE per item; the merge state is
        # read from the live GitHub surface, never caller-supplied.
        relax_merged_state = self._item_bound_to_merged_pr(
            item, ticket_id, contract_path
        )

        # Run each check; all must pass for the item to be VERIFIED
        messages: list[str] = []
        for check in checks:
            check_type = check.get("check_type") or ""
            if check_type in ("command", "test_passes"):
                # ``test_passes`` is a semantic alias for ``command`` that signals
                # the command is a test runner (typically ``uv run pytest ...``).
                # Both share the same execution path: run the shell command and
                # treat exit code 0 as VERIFIED. The alias exists so contracts
                # can declare intent (running tests) distinct from generic
                # commands without forcing every shell-based check into the same
                # bucket. Regression for OMN-10046.
                ok, msg = self._run_command_check(
                    check,
                    ticket_id,
                    contract_path,
                    relax_merged_state=relax_merged_state,
                )
                if not ok:
                    return ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=msg,
                        proof_class=item_proof_class,
                    )
                messages.append(msg)
            elif check_type == "file_exists":
                ok, msg = self._run_file_exists_check(check, contract_path)
                if not ok:
                    return ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=msg,
                        proof_class=item_proof_class,
                    )
                messages.append(msg)
            else:
                # Unknown or missing check_type must FAIL, not SKIPPED.
                # Silently skipping unknown types is the OMN-9571 bug class:
                # a misspelled or unregistered check_type would let DoD evidence
                # pass trivially without running any real check.
                label = check_type if check_type else "<missing check_type key>"
                return ModelEvidenceCheckResult(
                    evidence_id=evidence_id,
                    description=description,
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=(
                        f"Unknown check_type: {label!r}. "
                        "Supported: command, test_passes, file_exists."
                    ),
                    proof_class=item_proof_class,
                )

        return ModelEvidenceCheckResult(
            evidence_id=evidence_id,
            description=description,
            status=EnumEvidenceCheckStatus.VERIFIED,
            message="; ".join(messages) if messages else None,
            proof_class=item_proof_class,
        )

    def _resolve_cwd(
        self,
        cwd_template: str,
        ticket_id: str,
    ) -> tuple[str | None, str | None]:
        """Resolve a ``cwd`` template string into an absolute, contained path.

        Supports the ``${OMNI_HOME}``, ``${PR_NUMBER}``, ``${REPO}``, and
        ``${TICKET_ID}`` template tokens introduced by OMN-10078 (mirroring
        the OMN-10086 substitution pattern from the contract-compliance
        runner). Returns ``(resolved_path, None)`` on success or
        ``(None, error_message)`` on failure.

        Containment rules (defence-in-depth — the model itself does not
        validate `cwd`):

        - ``..`` segments in the raw input are rejected up-front
        - the resolved path must be relative to ``OMNI_HOME`` (when set);
          paths that escape via symlinks are rejected after ``Path.resolve()``
        - the resolved path must exist and be a directory
        """
        if ".." in Path(cwd_template).parts:
            return None, f"cwd path traversal not allowed: {cwd_template}"

        # Build the substitution table. Missing tokens leave the literal
        # ``${TOKEN}`` in place — the existence/containment check below is
        # what flags a bad cwd.
        substitutions = {
            "OMNI_HOME": os.environ.get("OMNI_HOME", ""),
            "PR_NUMBER": os.environ.get("PR_NUMBER", ""),
            "REPO": os.environ.get("REPO", ""),
            "TICKET_ID": ticket_id,
        }
        rendered = cwd_template
        for token, value in substitutions.items():
            rendered = rendered.replace(f"${{{token}}}", value)
        # Also support bare $TOKEN form via os.path.expandvars for any
        # tokens we did not template explicitly (e.g. user-set vars).
        rendered = os.path.expandvars(rendered)

        if "${" in rendered or rendered == "":
            return None, (
                f"cwd contains unresolved template tokens or is empty after "
                f"substitution: {cwd_template!r} -> {rendered!r}"
            )

        candidate = Path(rendered).resolve()

        omni_home = os.environ.get("OMNI_HOME")
        if omni_home:
            base = Path(omni_home).resolve()
            if not candidate.is_relative_to(base):
                return None, (
                    f"cwd escapes OMNI_HOME containment: {cwd_template!r} "
                    f"resolved to {candidate}"
                )

        if not candidate.exists():
            return None, f"cwd does not exist: {cwd_template!r} -> {candidate}"
        if not candidate.is_dir():
            return None, f"cwd is not a directory: {candidate}"

        return str(candidate), None

    @staticmethod
    def _repo_and_pr_from_evidence_id(evidence_item_id: str | None) -> tuple[str, str]:
        """Extract an authoritative ``(owner/repo, pr_number)`` binding from
        the current dod_evidence item's id — see ``_EVIDENCE_ID_BINDING_RE``.
        Returns ``("", "")`` when the id does not follow the convention.
        """
        if not evidence_item_id:
            return "", ""
        match = _EVIDENCE_ID_BINDING_RE.match(evidence_item_id)
        if not match:
            return "", ""
        return f"{match.group('owner')}/{match.group('repo')}", match.group("num")

    def _lookup_pr_for_ticket(self, ticket_id: str) -> str:
        """Return the merged PR number string for ticket_id, or empty string.

        OMN-15382 fail-closed rewrite. Resolution order:

        1. ``PR_NUMBER`` env var (not gh I/O — stays here, unchanged).
        2. The current evidence item's id, when it embeds an authoritative
           ``(owner/repo, pr_number)`` binding (see
           ``_repo_and_pr_from_evidence_id``) — zero gh calls, deterministic.
        3. HandlerDodEvidenceGithubEffect's ``gh pr list`` search, but ONLY
           when a repo can be resolved first (``REPO`` env var, else the
           evidence-id-derived repo). The prior implementation ran this
           search with no ``--repo`` flag at all — silently resolving
           whatever repo the process cwd's git remote pointed at — which is
           the OMN-15382 root cause (``${PR_NUMBER}`` bound PR #2454 instead
           of #2536). When no repo can be resolved, this now fails closed
           immediately rather than guessing via an unscoped search.

        Returns empty string when nothing can be resolved (caller must
        handle unresolved placeholders gracefully); ``self._last_pr_lookup_error``
        carries the specific fail-closed reason for the caller's message.
        """
        self._last_pr_lookup_error = None
        env_val = os.environ.get("PR_NUMBER", "").strip()
        if env_val:
            return env_val

        id_repo, id_pr = self._repo_and_pr_from_evidence_id(
            self._current_evidence_item_id
        )
        if id_pr:
            return id_pr

        repo = os.environ.get("REPO", "").strip() or id_repo
        if not repo:
            self._last_pr_lookup_error = (
                "PR_LOOKUP_FAILED: cannot resolve target repo for PR search "
                "(set REPO env var, or the evidence item id must embed "
                "owner/repo per the autobind naming convention)"
            )
            return ""

        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id=ticket_id,
            repo=repo,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = self._github_lookup_result(output)
        if not result.text_value and result.error_code:
            self._last_pr_lookup_error = result.error_code
        return result.text_value

    def _lookup_repo_for_ticket(self, ticket_id: str) -> str:
        """Return the ``owner/repo`` string for ticket_id, or empty string.

        Checks REPO env var first (not gh I/O — stays here), then the
        current evidence item's id-derived binding (OMN-15382, zero gh
        calls), then falls back to HandlerDodEvidenceGithubEffect's ``gh pr
        list`` search. That gh fallback is scoped by whatever repo the
        invoking process's cwd git remote resolves to — it CANNOT discover a
        repo different from the caller's own working tree, so it is not a
        substitute for an explicit REPO/id-derived binding when the target
        repo differs from cwd; see the handler's ``_lookup_repo_for_ticket``
        docstring. It is hardened with the same exact-ticket-token
        fail-closed filtering as PR lookup (OMN-14400, RSD-1 of OMN-14398 —
        the gh-CLI I/O itself lives in the canonical EFFECT handler).
        """
        self._last_repo_lookup_error = None
        env_val = os.environ.get("REPO", "").strip()
        if env_val:
            return env_val

        id_repo, _id_pr = self._repo_and_pr_from_evidence_id(
            self._current_evidence_item_id
        )
        if id_repo:
            return id_repo

        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id=ticket_id,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = self._github_lookup_result(output)
        if not result.text_value and result.error_code:
            self._last_repo_lookup_error = result.error_code
        return result.text_value

    def _resolve_command_placeholders(
        self,
        cmd_str: str,
        ticket_id: str,
    ) -> tuple[str, str | None]:
        """Substitute all placeholder forms in a command string.

        Supported forms:
          - ``{ticket_id}``, ``{pr}``, ``{repo}``  (Python format-style)
          - ``${TICKET_ID}``, ``${PR_NUMBER}``, ``${REPO}``  (shell-style)

        Returns ``(resolved_cmd, None)`` on success.
        Returns ``(original_cmd, error_message)`` when a required placeholder
        cannot be resolved (e.g. no merged PR found for ticket).
        """
        needs_pr = "{pr}" in cmd_str or "${PR_NUMBER}" in cmd_str
        needs_repo = "{repo}" in cmd_str or "${REPO}" in cmd_str

        pr_num = self._lookup_pr_for_ticket(ticket_id) if needs_pr else ""
        repo = self._lookup_repo_for_ticket(ticket_id) if needs_repo else ""

        if needs_pr and not pr_num:
            reason = self._last_pr_lookup_error
            detail = f" [{reason}]" if reason else ""
            return cmd_str, (
                f"Cannot resolve PR number for {ticket_id}: "
                f"set PR_NUMBER env var or ensure a merged PR exists.{detail}"
            )
        if needs_repo and not repo:
            reason = self._last_repo_lookup_error
            detail = f" [{reason}]" if reason else ""
            return cmd_str, (
                f"Cannot resolve repo for {ticket_id}: "
                f"set REPO env var or ensure a merged PR exists.{detail}"
            )

        # Apply shell-style substitutions first (${...} → value)
        cmd_str = cmd_str.replace("${TICKET_ID}", shlex.quote(ticket_id))
        if pr_num:
            cmd_str = cmd_str.replace("${PR_NUMBER}", shlex.quote(pr_num))
        if repo:
            cmd_str = cmd_str.replace("${REPO}", shlex.quote(repo))

        # Apply Python-format-style substitutions ({...} → value)
        cmd_str = cmd_str.replace("{ticket_id}", shlex.quote(ticket_id))
        if pr_num:
            cmd_str = cmd_str.replace("{pr}", shlex.quote(pr_num))
        if repo:
            cmd_str = cmd_str.replace("{repo}", shlex.quote(repo))

        return cmd_str, None

    def _infer_occ_cwd(self, contract_path: Path | None) -> str | None:
        """Return the onex_change_control repo path when contract is from OCC.

        Detects OCC contracts by checking whether the contract path contains
        ``onex_change_control`` as a path component. Returns None for all
        other contracts (cwd stays inherited).
        """
        # OMN-13888 (scope 6): during a dev-resolved collect, greps must run
        # inside the origin/dev worktree so dev-only receipt files are visible.
        if self._occ_dev_root:
            return self._occ_dev_root
        if contract_path is None:
            return None
        if "onex_change_control" not in contract_path.parts:
            return None
        omni_home = os.environ.get("OMNI_HOME")
        if not omni_home:
            return None
        occ_path = Path(omni_home) / "onex_change_control"
        if occ_path.is_dir():
            return str(occ_path)
        return None

    def _resolve_contract_repo_dir(self, contract_path: Path | None) -> str | None:
        """Resolve the OCC repo root for the ``$CONTRACT_REPO_DIR`` check token.

        OMN-13857: contract check commands embed ``$CONTRACT_REPO_DIR`` (e.g.
        ``grep -q ... "$CONTRACT_REPO_DIR/drift/dod_receipts/<T>/.../command.yaml"``).
        Before this fix the token was only satisfied when the *caller* happened to
        export ``CONTRACT_REPO_DIR``; unset it expanded to the empty string and
        every receipt-backed check FAILED with a missing-prefix path — a
        false-negative verdict on genuinely-passing evidence.

        Resolution is deterministic and does not depend on the caller's shell:

        1. An explicit non-empty ``CONTRACT_REPO_DIR`` in the environment wins
           (honours a deliberate CI / operator override).
        2. Otherwise, if the contract lives under ``onex_change_control/``, the
           OCC root is derived from the contract path itself — correct even when
           ``OMNI_HOME`` is unset or points elsewhere.
        3. Otherwise ``$ONEX_CC_REPO_PATH`` when set.
        4. Otherwise ``$OMNI_HOME/onex_change_control`` when that directory exists.

        Returns the absolute OCC root, or ``None`` when it cannot be resolved
        (the check then runs with the token unset — the fail-closed pre-fix
        behaviour — rather than silently guessing a wrong root).
        """
        explicit = os.environ.get("CONTRACT_REPO_DIR", "").strip()
        if explicit:
            return explicit

        # OMN-13888 (scope 6): a materialised origin/dev worktree is the OCC root
        # for receipt-backed greps during a dev-resolved collect.
        if self._occ_dev_root:
            return self._occ_dev_root

        # Derive from the contract path when it lives inside the OCC clone.
        if contract_path is not None and "onex_change_control" in contract_path.parts:
            parts = contract_path.parts
            occ_index = parts.index("onex_change_control")
            occ_root = Path(*parts[: occ_index + 1])
            if occ_root.is_dir():
                return str(occ_root)

        cc_repo_path = os.environ.get("ONEX_CC_REPO_PATH", "").strip()
        if cc_repo_path:
            return cc_repo_path

        omni_home = os.environ.get("OMNI_HOME", "").strip()
        if omni_home:
            occ_path = Path(omni_home) / "onex_change_control"
            if occ_path.is_dir():
                return str(occ_path)

        return None

    # ------------------------------------------------------------------
    # OMN-14207: live GitHub PR-state verification for PR-bound evidence.
    # ------------------------------------------------------------------

    @staticmethod
    def _live_pr_check_enabled() -> bool:
        """Whether the live PR-state check runs (default: on).

        Disabled only when ``DOD_VERIFY_LIVE_PR_CHECK`` is explicitly set to a
        falsey value (``0``/``false``/``off``/``no``). This is a deliberate,
        logged operator opt-out for environments without an authenticated
        ``gh`` CLI — NOT a silent fallback. When disabled, the live check is
        emitted as SKIPPED (surfaced, not swallowed).
        """
        raw = os.environ.get(_LIVE_PR_CHECK_ENV, "1").strip().lower()
        return raw not in ("0", "false", "off", "no")

    @staticmethod
    def _normalize_repo(repo: str) -> str:
        """Return ``owner/repo``, defaulting a bare name to the OmniNode org."""
        repo = repo.strip()
        return repo if "/" in repo else f"{_DEFAULT_GITHUB_ORG}/{repo}"

    def _resolve_pr_bindings(
        self,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None,
    ) -> list[tuple[str, int]]:
        """Resolve every ``(owner/repo, pr_number)`` this evidence item binds to.

        OMN-15382 (F2) fail-closed rewrite, in precedence order:

        1. An explicit ``pr`` mapping on the item (``{repo, number}``) or explicit
           ``repo`` + ``pr_number`` scalar fields — lets a contract declare the
           binding directly (future-proof).
        2. A hardcoded, same-clause literal ``gh pr view/checks/diff <N> ...
           --repo <owner>/<repo>`` pin within the item's OWN ``checks[]``
           (:func:`_hardcoded_pr_bindings_in_value`) — the repo and number come
           from the exact same string, so they can never be mixed.
        3. The item's ``id``, when it follows the autobind naming convention
           (:meth:`_repo_and_pr_from_evidence_id`) — same guarantee, a single
           anchored regex over one string.
        4. The durable receipt(s) for the item under
           ``<occ_root>/drift/dod_receipts/<ticket>/<item_id>/*.yaml`` — but
           ONLY a citation whose repo and ``pr_number`` are corroborated by the
           SAME receipt field (:func:`_field_confirms_pair`). A receipt's
           ``pr_number`` records the TICKET/CARRIER PR the receipt was authored
           under, which is not necessarily the PR any given
           ``check_value``/``probe_command`` pins — trusting a repo extracted
           from one field paired unconditionally with ``pr_number`` produced a
           mismatched pair (discovery case: ``dod-omn-14968-pr-2536-rebind-15382``
           derived ``(OmniNode-ai/omnibase_infra, 5458)`` — the carrier PR
           number paired with the pinned PR's repo — instead of the item's
           actual ``(OmniNode-ai/omnibase_infra, 2536)`` pin). A PASS receipt
           that names a ``pr_number`` but cannot be consistently paired sets
           ``self._last_binding_note`` (read by
           :meth:`_live_pr_checks_for_item`) instead of silently contributing
           nothing or trusting the mismatched pair.

           OMN-15465 adds a second refusal at this tier: a receipt pair that IS
           internally consistent but whose number CONTRADICTS a PR the item's
           own ``id`` literally pins (:func:`_pr_numbers_pinned_by_item_id`) is
           refused too. Internal consistency only proves the receipt describes
           *some* PR coherently — not that it describes *this item's* PR.

        Tiers 1-3 are deliberately NOT subject to the id-contradiction guard:
        an explicit ``pr`` field is an author declaration, the item's own
        ``check_value`` is the command that actually runs, and tier 3 reads the
        id itself. Only the receipt tier speaks about the item from outside it,
        so only the receipt tier can be wrong about which PR the item is.

        Returns a de-duplicated list; empty when the item does not bind to any PR
        (a non-PR evidence item is therefore unaffected by the live check).
        """
        self._last_binding_note = None
        bindings: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()

        def _add(repo_val: object, number_val: object) -> None:
            if not isinstance(repo_val, str) or not repo_val.strip():
                return
            # bool is an int subclass — reject it as a PR number.
            if isinstance(number_val, bool):
                return
            if isinstance(number_val, int):
                number = number_val
            elif isinstance(number_val, str) and number_val.strip().isdigit():
                number = int(number_val.strip())
            else:
                return
            if number <= 0:
                return
            key = (self._normalize_repo(repo_val), number)
            if key not in seen:
                seen.add(key)
                bindings.append(key)

        # 1. Explicit fields on the item.
        explicit = item.get("pr")
        if isinstance(explicit, dict):
            _add(
                explicit.get("repo"),
                explicit.get("number", explicit.get("pr_number")),
            )
        _add(item.get("repo"), item.get("pr_number"))

        # 2. Hardcoded, same-clause literal pin in the item's OWN checks.
        if not bindings:
            checks = item.get("checks")
            if isinstance(checks, list):
                for check in checks:
                    if not isinstance(check, dict):
                        continue
                    value = check.get("check_value") or check.get("command")
                    if not isinstance(value, str) or not value.strip():
                        continue
                    for repo_val, number_val in _hardcoded_pr_bindings_in_value(value):
                        _add(repo_val, number_val)

        # 3. id-convention parse.
        if not bindings:
            item_id_raw = item.get("id")
            item_id_for_parse = item_id_raw if isinstance(item_id_raw, str) else None
            id_repo, id_pr = self._repo_and_pr_from_evidence_id(item_id_for_parse)
            if id_repo and id_pr:
                _add(id_repo, id_pr)

        # 4. Receipt-derived bindings, hardened against cross-field mixing.
        if not bindings:
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                receipts = apply_supersessions(
                    self._load_item_receipts(item_id, ticket_id, contract_path)
                )
                untrusted = False
                # OMN-15465: PR numbers the item's own id asserts. A
                # receipt-derived pair can be internally consistent (repo and
                # number corroborated by the SAME field) and STILL describe a
                # different PR than the item does — the OMN-14623
                # "2nd consumer" merged-path supersede re-binds a prior entry
                # to an unrelated product PR, so the surviving receipt for
                # ``occ-self-bind-pr-4711`` reads ``pr_number: 2424`` +
                # ``gh pr view 2424 --repo OmniNode-ai/omnibase_infra``. The
                # F2 consistency check passes it; the resulting live-state
                # check then reports omnibase_infra#2424 under an item whose
                # id and description both say OCC #4711, and renders VERIFIED
                # whenever that carrier PR is merged and green. Census over
                # OCC origin/dev @5a19a5e1b: 55 non-superseded items affected
                # (plus 43 masked behind a contract-entry supersession).
                id_pinned = _pr_numbers_pinned_by_item_id(item_id)
                contradicted: list[int] = []
                for receipt in receipts:
                    if not isinstance(receipt, dict):
                        continue
                    if receipt.get("status") != "PASS":
                        continue
                    pr_number = receipt.get("pr_number")
                    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
                        continue
                    confirmed_repo = self._consistent_receipt_repo(receipt, pr_number)
                    if confirmed_repo is None:
                        untrusted = True
                        continue
                    if id_pinned and pr_number not in id_pinned:
                        # Refuse, never downgrade to "probe the carrier
                        # anyway": absent is honest, mis-bound is not.
                        contradicted.append(pr_number)
                        continue
                    _add(confirmed_repo, pr_number)
                if not bindings and contradicted:
                    self._last_binding_note = (
                        f"NO_CONSISTENT_PR_BINDING: item {item_id!r} pins PR "
                        f"{sorted(id_pinned)} in its own id, but its surviving "
                        f"PASS receipt(s) bind it to PR {sorted(contradicted)} "
                        "— a different PR entirely (the OMN-14623 2nd-consumer "
                        "supersede re-binds a prior entry to the current "
                        "product PR). No live-state binding derived, because "
                        "probing the carrier PR here would report VERIFIED "
                        "about a PR this item never referenced. Give the item "
                        "its own literal pin ('gh pr view <N> --repo "
                        "<owner>/<repo>' or 'repos/<owner>/<repo>/pulls/<N>'), "
                        "or give the 2nd consumer its own evidence item "
                        "instead of re-binding this one."
                    )
                elif not bindings and untrusted:
                    self._last_binding_note = (
                        f"NO_CONSISTENT_PR_BINDING: item {item_id!r} has a PASS "
                        "receipt recording pr_number, but no receipt field "
                        "consistently pairs that number with a repo (the "
                        "receipt's pr_number tracks the carrier PR, which may "
                        "differ from the PR any check_value/probe_command "
                        "actually pins) — no live-state binding derived. Pin "
                        "the item's own check_value with a literal 'gh pr "
                        "view <N> --repo <owner>/<repo>' or repair the "
                        "receipt so pr_number and the probed repo agree."
                    )

        return bindings

    @staticmethod
    def _consistent_receipt_repo(receipt: dict[str, Any], pr_number: int) -> str | None:
        """Return a repo for ``pr_number`` only when a receipt field names both.

        Checks ``probe_stdout``, ``probe_command``, ``check_value`` in that
        order for either a ``--repo <owner>/<repo>`` flag or a
        ``https://github.com/<owner>/<repo>/pull/<n>`` URL, and requires the
        SAME field to also confirm ``pr_number`` (:func:`_field_confirms_pair`
        for the flag form; the URL's own ``<n>`` for the URL form). Pure
        function — no I/O.
        """
        for field_name in ("probe_stdout", "probe_command", "check_value"):
            value = receipt.get(field_name)
            if not isinstance(value, str) or not value.strip():
                continue
            repo_match = _REPO_FLAG_RE.search(value)
            if repo_match is not None and _field_confirms_pair(
                value, repo_match.group(1), pr_number
            ):
                return repo_match.group(1)
            for url_repo, url_num in _GH_PR_URL_RE.findall(value):
                if int(url_num) == pr_number:
                    return str(url_repo)
        return None

    def _load_item_receipts(
        self,
        item_id: str,
        ticket_id: str,
        contract_path: Path | None,
    ) -> list[dict[str, Any]]:
        """Load the durable receipt payloads for a single evidence item.

        Receipts live at the canonical platform layout
        ``<occ_root>/drift/dod_receipts/<ticket>/<item_id>/*.yaml``. Each payload
        is tagged with ``__source_name__`` so :func:`apply_supersessions` can
        order any supersession chain by the unforgeable filename ordinal (matching
        the DurableEvidenceGate). Returns ``[]`` when the OCC root or the receipt
        directory cannot be resolved.
        """
        occ_root = self._resolve_contract_repo_dir(contract_path)
        if occ_root is None:
            return []
        receipt_dir = Path(occ_root) / "drift" / "dod_receipts" / ticket_id / item_id
        if not receipt_dir.is_dir():
            return []
        payloads: list[dict[str, Any]] = []
        for receipt_file in sorted(receipt_dir.glob("*.yaml")):
            raw = self._load_yaml(receipt_file)
            if raw is None:
                continue
            raw.setdefault("__source_name__", receipt_file.name)
            payloads.append(raw)
        return payloads

    def _live_pr_checks_for_item(
        self,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None,
    ) -> list[ModelEvidenceCheckResult]:
        """Emit the live PR-state check result(s) for a PR-bound evidence item.

        Returns ``[]`` for a non-PR item (leaving its declared-check result the
        sole authority) — UNLESS :meth:`_resolve_pr_bindings` found evidence the
        item IS PR-related (a PASS receipt naming a ``pr_number``) but could not
        safely derive which PR/repo it pins (OMN-15382 F2c); that case emits one
        SKIPPED, visibly-noted result instead of silently omitting the check.
        Otherwise one result per bound PR: VERIFIED when the PR is MERGED and
        all checks are green; FAILED otherwise, INCLUDING when the live state
        cannot be resolved (fail-closed — a Done-flip must not proceed on
        unverifiable PR state) — EXCEPT for the one shape OMN-16788 carves
        out, a MERGED PR whose required-context set the credential was not
        PERMITTED to read, which is SKIPPED with a named
        ``unverifiable_cause`` (still not verified, so still blocking; see
        :meth:`_verify_live_pr`) — and UNLESS the item's own check_value asserts a
        specific non-merged state (OPEN/CLOSED) for that exact PR reference
        (OMN-16087), in which case that ONE binding is SKIPPED instead: the
        item's declared ``command`` check already verifies the assertion
        directly, so deriving a second, contradictory "must be MERGED"
        judgement would invert the entry's stated intent rather than
        corroborate it. Every OTHER binding on the same item (e.g. a second PR
        reference the item asserts IS merged) is unaffected and still receives
        the ordinary live derivation.
        """
        bindings = self._resolve_pr_bindings(item, ticket_id, contract_path)
        item_id = str(item.get("id", "unknown"))
        description = str(item.get("description", item_id))
        if not bindings:
            if self._last_binding_note is not None:
                return [
                    ModelEvidenceCheckResult(
                        evidence_id=f"{item_id}::pr-live-state",
                        description=f"Live PR state for {description}",
                        status=EnumEvidenceCheckStatus.SKIPPED,
                        message=self._last_binding_note,
                        proof_class=EnumCheckProofClass.MERGE_STATE,
                    )
                ]
            return []

        if not self._live_pr_check_enabled():
            logger.warning(
                "Live PR check disabled via %s; %d binding(s) NOT verified for %s",
                _LIVE_PR_CHECK_ENV,
                len(bindings),
                item_id,
            )
            return [
                ModelEvidenceCheckResult(
                    evidence_id=f"{item_id}::pr-live-state",
                    description=f"Live PR state for {description}",
                    status=EnumEvidenceCheckStatus.SKIPPED,
                    message=(
                        f"Live PR check disabled via {_LIVE_PR_CHECK_ENV}; "
                        f"bindings not verified: {bindings}"
                    ),
                    proof_class=EnumCheckProofClass.MERGE_STATE,
                )
            ]

        asserted_states = self._asserted_non_merged_pr_states_for_item(item, bindings)

        results: list[ModelEvidenceCheckResult] = []
        multi = len(bindings) > 1
        for repo, pr_number in bindings:
            evidence_id = (
                f"{item_id}::pr-{pr_number}-live-state"
                if multi
                else f"{item_id}::pr-live-state"
            )
            asserted = asserted_states.get((repo, pr_number))
            if asserted is not None:
                results.append(
                    ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=(
                            f"Live GitHub state for {repo}#{pr_number} ({item_id})"
                        ),
                        status=EnumEvidenceCheckStatus.SKIPPED,
                        message=(
                            f"{repo}#{pr_number}: item's own check_value "
                            f"asserts {asserted} (not MERGED) for this PR — "
                            "an intentional non-merged assertion (OMN-16087), "
                            "not auto-bound to merged/green semantics. The "
                            "item's declared command check verifies this "
                            "assertion directly."
                        ),
                        proof_class=EnumCheckProofClass.MERGE_STATE,
                    )
                )
                continue
            status, message, cause = self._verify_live_pr(repo, pr_number)
            results.append(
                ModelEvidenceCheckResult(
                    evidence_id=evidence_id,
                    description=f"Live GitHub state for {repo}#{pr_number} ({item_id})",
                    status=status,
                    message=message,
                    unverifiable_cause=cause,
                    proof_class=EnumCheckProofClass.MERGE_STATE,
                )
            )
        return results

    @staticmethod
    def _asserted_non_merged_pr_states_for_item(
        item: dict[str, Any], bindings: list[tuple[str, int]]
    ) -> dict[tuple[str, int], str]:
        """Return ``{(repo, pr_number): "OPEN"|"CLOSED"}`` for every resolved
        ``binding`` whose exact PR reference is accompanied, in the SAME
        pipeline of the item's own ``checks[]`` text, by an explicit
        non-merged state assertion (OMN-16087).

        Scans ``check_value``/``command`` on every check in the item — not
        only the check(s) that produced the binding — because the binding may
        have been resolved from a different tier (id convention, receipt)
        than the check carrying the assertion; the assertion is still
        authoritative when it names the same (repo, pr_number) pair. Bindings
        with no matching assertion are simply absent from the returned dict.
        Pure — no I/O.
        """
        checks = item.get("checks")
        if not isinstance(checks, list) or not bindings:
            return {}
        values: list[str] = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            value = check.get("check_value") or check.get("command")
            if isinstance(value, str) and value.strip():
                values.append(value)
        asserted: dict[tuple[str, int], str] = {}
        for repo, pr_number in bindings:
            for value in values:
                state = _pr_binding_asserted_state(value, repo, pr_number)
                if state is not None:
                    asserted[(repo, pr_number)] = state
                    break
        return asserted

    def _verify_live_pr(
        self, repo: str, pr_number: int
    ) -> tuple[EnumEvidenceCheckStatus, str, EnumEvidenceUnverifiableCause | None]:
        """Return ``(status, message, cause)`` for the live state of
        ``repo#pr_number``.

        VERIFIED only when the PR is MERGED AND every REQUIRED status check is
        green (OMN-14390) — a red non-required/informational check does not
        block. A failure to resolve the merge state (gh
        missing/auth/network/not-found) fails closed as FAILED.

        SKIPPED (OMN-16788) in exactly one shape: the PR is CONFIRMED MERGED
        and the ONLY thing left unproven is a required-context set the
        verifying credential was not permitted to read. That is not a check
        that ran and found the evidence wanting, and recording it as a
        substantive failure is what made the scheduled CI sweep disagree with
        every local run. The caller keeps the check out of the failure count
        AND out of the verified count: ``HandlerDodVerify`` refuses to reach
        VERIFIED while any ``cause`` is present, so OMN-15715's fail-closed
        intent is preserved exactly — only its bookkeeping changes.

        Everything else stays FAILED, deliberately:

        * a PR that is genuinely not merged — read off a reachable API, a
          substantive fact that a credential gap elsewhere does not excuse;
        * a required context that actually ran RED;
        * a timeout / 5xx / OSError on the protection probe, which says
          nothing about a credential and must not become a laundering route.
        """
        self._last_checks_unreachable_cause = None
        merge = self._fetch_pr_merge_state(repo, pr_number)
        if merge is None:
            return (
                EnumEvidenceCheckStatus.FAILED,
                (
                    f"{repo}#{pr_number}: could not resolve live PR state via gh "
                    "(missing/auth/network/not-found). Failing closed — a Done-flip "
                    "must not proceed on unverifiable PR state."
                ),
                None,
            )
        merged, state = merge
        reasons: list[str] = []
        if not merged:
            reasons.append(f"PR not merged (state={state})")
        self._last_checks_unreachable_cause = None
        checks_green, checks_detail = self._fetch_pr_checks_green(repo, pr_number)
        checks_cause = self._last_checks_unreachable_cause
        if not checks_green:
            reasons.append(f"required checks not green ({checks_detail})")
        if not reasons:
            return (
                EnumEvidenceCheckStatus.VERIFIED,
                f"{repo}#{pr_number}: MERGED (state={state}); {checks_detail}",
                None,
            )
        if merged and checks_cause is not None:
            # Sole outstanding reason is an unread required-context set. The
            # ``merged`` guard is load-bearing: without it an OPEN PR in a
            # repo the credential cannot fully read would launder its
            # not-merged failure into a skip.
            return (
                EnumEvidenceCheckStatus.SKIPPED,
                (
                    f"{repo}#{pr_number}: MERGED (state={state}), but this "
                    f"check could not be evaluated — {checks_detail}. Recorded "
                    f"SKIPPED with cause '{checks_cause.value}', NOT verified: "
                    f"unread evidence never satisfies a Done-flip."
                ),
                checks_cause,
            )
        return (
            EnumEvidenceCheckStatus.FAILED,
            f"{repo}#{pr_number}: " + "; ".join(reasons),
            None,
        )

    def _fetch_pr_merge_state(
        self,
        repo: str,
        pr_number: int,
    ) -> tuple[bool, str] | None:
        """Return ``(merged, state)`` for ``repo#pr_number`` via the GitHub
        effect handler's ``gh pr view`` lookup (OMN-14400, RSD-1 of
        OMN-14398 — behavior-identical carve-out of the gh-CLI I/O into a
        canonical EFFECT handler).

        Returns ``None`` on any inability to resolve the PR (timeout, missing gh,
        non-zero exit, unparseable output) so the caller can fail closed.
        """
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_MERGE_STATE,
            repo=repo,
            pr_number=pr_number,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = self._github_lookup_result(output)
        if not result.resolved:
            return None
        return bool(result.merged), result.state or "UNKNOWN"

    def _fetch_pr_checks_green(
        self,
        repo: str,
        pr_number: int,
    ) -> tuple[bool, str]:
        """Return ``(all_green, detail)`` for ``repo#pr_number`` REQUIRED status
        checks via the GitHub effect handler (OMN-14400, RSD-1 of OMN-14398).

        Scoped to required checks only via required-context names read live from
        branch protection, cross-referenced against check-suites/check-runs for
        the PR's own head branch (OMN-15709) — a non-green *non-required* check
        (e.g. an informational/advisory job) must never fail a Done-flip, and a
        check-run produced only by a foreign PR/branch sharing the same head SHA
        must never satisfy OR redden this PR's evidence; only branch-protection-
        required contexts actually produced on the PR's own branch are
        load-bearing here. Fails closed: any non-green required check
        (FAILURE/CANCELLED/PENDING/...), a required context missing entirely or
        produced only by a foreign branch, an empty required-check set, or an
        inability to enumerate checks yields ``False``.
        """
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=repo,
            pr_number=pr_number,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = self._github_lookup_result(output)
        # OMN-16788: publish the handler's credential-reachability
        # classification on the side channel for ``_verify_live_pr``. Always
        # assigned (including to None) so a previous item's cause can never
        # leak into this one.
        self._last_checks_unreachable_cause = result.unreachable_cause
        return bool(result.checks_green), result.detail or ""

    # ------------------------------------------------------------------
    # OMN-14637: merged-state re-anchoring of self-referential live-OPEN gates.
    # ------------------------------------------------------------------

    def _item_bound_to_merged_pr(
        self,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None,
    ) -> bool:
        """Whether this evidence item binds to a PR that GitHub confirms MERGED.

        Reuses the SAME authoritative binding resolution the OMN-14207 live-state
        check uses (:meth:`_resolve_pr_bindings` — an explicit ``pr`` field or the
        durable receipt's ``pr_number`` + probed repo), then reads live merge
        state via :meth:`_fetch_pr_merge_state`. Returns ``True`` only when at
        least one bound PR is CONFIRMED MERGED.

        Fails safe (no relaxation → command runs verbatim) when:

        * the live-PR check is disabled (``DOD_VERIFY_LIVE_PR_CHECK`` off) — merge
          state cannot then be authoritatively confirmed;
        * the item binds to no PR;
        * the probe is unresolved / errored, or the PR is not merged (OPEN/CLOSED).

        The merge fact is therefore never caller-supplied — it is read from the
        live GitHub surface, mirroring the fail-closed posture of the live check.
        """
        if not self._live_pr_check_enabled():
            return False
        bindings = self._resolve_pr_bindings(item, ticket_id, contract_path)
        for repo, pr_number in bindings:
            merge = self._fetch_pr_merge_state(repo, pr_number)
            if merge is not None and merge[0]:
                logger.info(
                    "OMN-14637: evidence item %s binds to MERGED PR %s#%d — "
                    "relaxing any live-OPEN-state gate to the merged terminal "
                    "state for its command checks.",
                    item.get("id", "unknown"),
                    repo,
                    pr_number,
                )
                return True
        return False

    @staticmethod
    def _relax_merged_pr_state_predicate(cmd_str: str) -> tuple[str, bool]:
        """Rewrite ``.state == "OPEN"`` → ``.state == "MERGED"`` (OMN-14637).

        Applied ONLY when the evidence item is confirmed bound to a MERGED PR
        (see :meth:`_item_bound_to_merged_pr`). It targets the canonical
        ``gh pr view ... --json state ... --jq '.state == "OPEN" and ...'`` idiom
        precisely: only the ``.state`` equality against ``OPEN`` (single- or
        double-quoted) is rewritten, preserving the quote style. Every other
        predicate — ``.headRefOid``, ``.files``, ``.title``, ``.baseRefName``,
        receipt greps, and any unrelated ``"OPEN"`` literal such as
        ``.title == "OPEN"`` — is left untouched, so the check still re-verifies
        the merged PR's retained content rather than vacuously passing.

        Returns ``(new_cmd, changed)`` where ``changed`` is ``True`` when at least
        one predicate was rewritten.
        """
        new_cmd, count = _PR_OPEN_STATE_PREDICATE_RE.subn(
            lambda m: f"{m.group(1)}{m.group(2)}MERGED{m.group(2)}", cmd_str
        )
        return new_cmd, count > 0

    def _run_command_check(
        self,
        check: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None = None,
        *,
        relax_merged_state: bool = False,
    ) -> tuple[bool, str]:
        """Execute a command-type check. Returns (success, message).

        OMN-10078: when ``check["cwd"]`` is set, the runner expands its
        ``${OMNI_HOME}/${PR_NUMBER}/${REPO}/${TICKET_ID}`` template tokens,
        containment-checks the resolved path against ``OMNI_HOME``, and
        passes ``cwd=`` to ``subprocess.run``. When ``cwd`` is absent the
        runner inherits its caller's working directory (legacy behaviour).

        OMN-10476: placeholder substitution is applied to the command string
        for both ``{pr}/{repo}/{ticket_id}`` and ``${PR_NUMBER}/${REPO}/${TICKET_ID}``
        forms before execution. OCC contracts get automatic cwd injection.
        """
        # Prefer explicit `command` field; fall back to `check_value`
        cmd_str = check.get("command") or check.get("check_value", "")
        if not cmd_str:
            return False, "Empty command in check definition."

        # OMN-10476: resolve all placeholder forms before execution
        cmd_str, placeholder_err = self._resolve_command_placeholders(
            cmd_str, ticket_id
        )
        if placeholder_err is not None:
            return False, placeholder_err

        # OMN-14637: when the evidence item is bound to a CONFIRMED-MERGED PR,
        # re-anchor any live-OPEN-state gate to the merged terminal state so a
        # normal, successful merge (head branch deleted, ``.state`` → MERGED) no
        # longer fails the sanctioned closeout forever. All other predicates in the
        # command still execute, so an unmet DoD still FAILS (non-vacuous).
        if relax_merged_state:
            cmd_str, relaxed = self._relax_merged_pr_state_predicate(cmd_str)
            if relaxed:
                logger.info(
                    "OMN-14637: relaxed live-OPEN-state gate to MERGED for a "
                    "merged-PR-bound command check."
                )

        # OMN-10078: resolve optional cwd via template-substitution +
        # containment-check pipeline. None => inherit caller cwd. This MUST
        # run before the shape guard below (OMN-15382 verifier finding):
        # a relative script path (e.g. "./verify.sh") is only resolvable
        # against the check's declared cwd, not this process's cwd.
        run_cwd: str | None = None
        cwd_template = check.get("cwd")
        if cwd_template is not None:
            if not isinstance(cwd_template, str):
                return False, f"cwd must be a string, got {type(cwd_template).__name__}"
            resolved, err = self._resolve_cwd(cwd_template, ticket_id)
            if err is not None:
                return False, err
            run_cwd = resolved
        else:
            # OMN-10476: auto-inject OCC cwd when no explicit cwd is declared
            run_cwd = self._infer_occ_cwd(contract_path)

        # OMN-15382: reject prose masquerading as a command BEFORE ever
        # shelling out (see module-level comment above
        # _invalid_check_value_reason for the two bug mechanisms this closes).
        # Runs AFTER cwd resolution and is cwd-aware so a legitimate
        # relative-script + cwd: check (e.g. "./verify.sh" with
        # cwd: "${OMNI_HOME}/.../subdir") resolves against the check's
        # declared cwd instead of this process's actual cwd/PATH.
        invalid_reason = _invalid_check_value_reason(cmd_str, cwd=run_cwd)
        if invalid_reason is not None:
            return False, f"INVALID_CHECK_VALUE_NOT_A_COMMAND: {invalid_reason}"

        # OMN-13857: deterministically satisfy the ``$CONTRACT_REPO_DIR`` token
        # used by receipt-backed check commands, so the verdict does not depend
        # on the caller having exported it. Inherit the caller env and overlay
        # the resolved OCC root (never mutate os.environ in place).
        run_env: dict[str, str] | None = None
        contract_repo_dir = self._resolve_contract_repo_dir(contract_path)
        if contract_repo_dir is not None:
            run_env = dict(os.environ)
            run_env["CONTRACT_REPO_DIR"] = contract_repo_dir

        logger.info(
            "Running command check (cwd=%s, CONTRACT_REPO_DIR=%s): %s",
            run_cwd or "<inherit>",
            contract_repo_dir or "<unset>",
            cmd_str,
        )

        start = time.monotonic()
        try:
            # OMN-15382: list-form + explicit pipefail (not shell=True /
            # plain sh -c) so a failing first stage of a multi-stage pipeline
            # fails the whole check instead of being masked by the last
            # stage's exit code.
            result = subprocess.run(
                ["bash", "-o", "pipefail", "-c", cmd_str],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=run_cwd,
                env=run_env,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {self._timeout}s: {cmd_str}"
        except Exception as exc:
            return False, f"Execution error: {exc}"

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            detail = stderr or stdout or f"exit code {result.returncode}"
            return False, f"FAILED ({elapsed_ms}ms): {detail}"

        return True, f"OK ({elapsed_ms}ms): {stdout[:200]}"

    def _run_file_exists_check(
        self,
        check: dict[str, Any],
        contract_path: Path | None = None,
    ) -> tuple[bool, str]:
        """Verify a path exists within the repo-root containment boundary.

        Accepts ``path`` or ``check_value`` as the target. Resolution rules:

        - Relative paths resolve against the inferred OCC repo root when the
          contract lives under ``onex_change_control/`` (matches the cwd
          inference used by ``_run_command_check``); otherwise they resolve
          against ``OMNI_HOME`` (or ``Path.cwd()`` fallback). Regression for
          OMN-10542.
        - Absolute paths are permitted only if they resolve inside ``OMNI_HOME``
          (when set).
        - ``..`` segments in the raw input are rejected up-front.
        - Every candidate (and every glob match) is canonicalised via
          ``Path.resolve()`` — which follows symlinks — and checked against
          the containment base with ``is_relative_to``. Symlink escapes are
          therefore blocked.
        - Glob metacharacters (``*``, ``?``, ``[``) are expanded; at least one
          match must remain after containment filtering.
        """
        raw_path = check.get("path") or check.get("check_value", "")
        if not raw_path:
            return False, "Empty path in file_exists check definition."

        raw_path_obj = Path(raw_path)
        if ".." in raw_path_obj.parts:
            return False, f"Path traversal not allowed: {raw_path}"

        omni_home = os.environ.get("OMNI_HOME")
        # Containment base: OMNI_HOME (or cwd fallback). All resolved paths must
        # land inside this boundary regardless of where they were resolved from.
        base = Path(omni_home).resolve() if omni_home else Path.cwd().resolve()

        # Relative-path resolution root: prefer the OCC repo root when this
        # contract lives under onex_change_control/, mirroring the cwd
        # inference _run_command_check uses (OMN-10542). The OCC root stays
        # inside OMNI_HOME so the containment check below is unaffected.
        occ_cwd = self._infer_occ_cwd(contract_path)
        resolve_root = Path(occ_cwd).resolve() if occ_cwd else base

        candidate = (
            raw_path_obj if raw_path_obj.is_absolute() else resolve_root / raw_path_obj
        )
        has_glob = any(ch in raw_path for ch in ("*", "?", "["))

        if has_glob:
            safe_matches: list[Path] = []
            for match in glob.glob(str(candidate)):
                resolved_match = Path(match).resolve()
                if resolved_match.is_relative_to(base):
                    safe_matches.append(resolved_match)
            if not safe_matches:
                return False, f"No matches for glob: {raw_path}"
            return True, f"OK: {len(safe_matches)} match(es) for {raw_path}"

        resolved_target = candidate.resolve()
        if not resolved_target.is_relative_to(base):
            return False, f"Path traversal not allowed: {raw_path}"
        if not resolved_target.exists():
            return False, f"Path does not exist: {raw_path}"
        return True, f"OK: exists {raw_path}"


__all__ = ["EvidenceCollector"]
