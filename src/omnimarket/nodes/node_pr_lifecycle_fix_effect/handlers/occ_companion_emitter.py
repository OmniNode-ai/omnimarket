# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The single canonical OCC companion producer (OMN-14285, S1 converge).

This is the **one** OCC-companion writer surface for the node. It replaces the
two ~80%-overlapping ``*Adapter`` classes that coexisted before S1:

  * ``OccAutobindAdapter`` (``receipt_evidence_source_autobind``: a product PR
    whose ``Evidence-Source`` pointed at its own head SHA instead of an OCC
    source), and
  * ``OccContractAdapter`` (``deploy_gate_contract_not_found``: a product PR with
    no OCC contract at all),

plus the bespoke ``omniclaude/scripts/scaffold_occ_receipt.py`` manual writer
(retired under the same ticket). All three authored load-bearing OCC evidence;
two of them carried divergent receipt shapes and the pre-OMN-14255 head-SHA bug.

Both failure classes are now ONE code path (:meth:`_emit_companion_sync`): the
autobind flow already authored the contract-if-absent, so the deploy-gate case is
a caller of the same core. Every committed byte — contract YAML, downstream
receipt, self-bind receipt, ``contract_sha256``/``contract_entry_sha256``
binding — is rendered by the pure :mod:`occ_evidence_stamp` seam (the COMPUTE
half); this class owns only the git/gh side effects (the EFFECT half). PR-body
``Evidence-Source`` / ``Evidence-Ticket`` authoring flows through the
:mod:`occ_stamp_authoring` seam (Piece 3, OMN-14189).

Hardened behaviors preserved from the autobind path: the real ``gh pr view``
probe (machine-observed, not fabricated — OMN-13990 item 4 / OMN-14055),
whole-file ``contract_sha256`` rebind across every matching receipt (friction #9;
now paired with a per-receipt ``contract_entry_sha256`` rebind, OMN-13888 /
OMN-14418 residual 3, so receipts survive a later append to the contract instead
of going stale), the two-stage OCC self-bind, open-**or-sync** idempotency (no
422 on ``synchronize`` re-fire), and force-push all-adds regeneration
(append-only gate stays green). Capabilities folded in from the deploy-gate
path: ``dry_run`` mode, the configurable ``verifier != runner`` self-attestation
guard (OMN-12791), and ``detect_occ_gap``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import urllib.parse
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

# OMN-16356: the SAME canonical judgment the hosted OCC Append-Only Gate makes
# about a contract diff — imported, never re-implemented, so this pre-push
# local guard can never be STRICTER than the gate it exists to pre-empt (the
# regression this ticket fixes: OMN-16071's PR #2086 hardened the local guard
# to a per-FILE git-status check, but the hosted gate's own semantics are
# per-ENTRY — a new dod_evidence id is always allowed; only a removed or
# content-altered existing id is a violation).
from omnibase_core.validation.validator_occ_append_only import evaluate_append_only

# OMN-13990 (D3, validator-parity ticket extraction) / OMN-16376 (title-only
# revision): the emitter's ``_extract_tickets`` calls this SAME gate-private
# helper (rather than re-deriving the ``OMN-\d+`` regex) so the two can never
# diverge on the token pattern — but only over the PR TITLE (empty body), since
# the gate's own identity check is title-anchored (see ``_extract_tickets``'
# docstring). omnibase-core is a declared omnimarket dependency.
#
# OMN-14418 residual 3: compute_contract_entry_sha256 is the SAME canonical
# per-entry hasher the consumer-side gates (check_receipt_contract_binding,
# check_receipt_hardening.py) recompute against — imported, never
# re-implemented, so the producer and the gates can never diverge on the hash.
from omnibase_core.validation.validator_receipt_gate import (
    ContractEntryNotFoundError,
    _extract_ticket_ids,
    compute_contract_entry_sha256,
)

from omnimarket.events.occ_autoauthor import OCC_AUTHOR_TIME_LABELS
from omnimarket.github_api import (
    GitHubApiError,
    rest_json,
    rest_json_array,
    split_repo,
)
from omnimarket.github_app_auth import resolve_app_installation_token_from_contract
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.merge_control.hold_marker import HOLD_MARKER_RE
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    SHA_RE,
    behavior_proof_check_value,
    changed_files_from_diff_scope_probe,
    ci_check_evidence_id,
    compute_contract_sha256,
    derive_behavior_test_paths,
    extract_evidence_item_id,
    rebind_contract_entry_sha256_in_text,
    rebind_contract_sha256_in_text,
    render_ci_check_receipt,
    render_ci_dod_evidence_item,
    render_companion_contract,
    render_downstream_dod_evidence_item,
    render_downstream_receipt,
    render_self_bind_dod_evidence_item,
    render_self_bind_receipt,
)

# OMN-14189 (Piece 3/5, epic OMN-14180): all PR-body Evidence-Source /
# Evidence-Ticket authoring and read-back flow through the single stamp seam,
# which delegates to the Piece-2 core renderer/parser over the Piece-1 models.
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_stamp_authoring import (
    product_pr_occ_binding,
    render_occ_companion_pr_body,
    render_product_pr_body_with_occ_source,
)
from omnimarket.occ_content_probe import (
    LOCK_FILE_SUFFIXES,
    extract_lock_line_candidates,
    extract_symbol_candidates,
    resolve_red_ref,
    select_asserted_check,
)

# OMN-15247: contention detection (ALWAYS-ON, no toggle) + the check-binding mode
# var. Both live in shared top-level modules (never a cross-node private import)
# so the born-path emitter and node_occ_state_effect derive from ONE definition
# each.
from omnimarket.occ_contention import (
    ContentionFinding,
    EnumCheckBinding,
    decide_contention,
    find_open_companions,
    resolve_occ_producer_policy,
)
from omnimarket.occ_git_transport import (
    OCC_REPO,
    acquire_occ_companion_lease,
    authenticated_occ_url,
    call_with_retry,
    release_occ_companion_lease,
    run_git,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# OMN-16356: matches ONLY the ticket-contract path shape `_allowed_paths`
# renders (`contracts/<ticket>.yaml`), never a receipt path — the
# content-verified append exception in `_assert_append_only` is scoped to
# this pattern exclusively.
_CONTRACT_YAML_PATH_RE = re.compile(r"^contracts/[^/]+\.yaml$")

# OMN-14031: bound every git subprocess so a stalled network git call (push /
# fetch under egress saturation) fails fast instead of wedging the fix-effect
# path — the same un-timed-subprocess hang class fixed in the inventory node.
_GIT_TIMEOUT_SECONDS = 120

_OCC_REPO = OCC_REPO

_DEFAULT_RUNNER = "node_pr_lifecycle_fix_effect"
_DEFAULT_VERIFIER = "occ-evidence-source-autobind"

# OMN-16339: a policy decline (the OMN-15247 RED-derivability gate refusing to
# mint) was previously visible ONLY as a PR comment — indistinguishable, from
# the checks rollup / workflow-run status / OCC PR list, from a stalled or
# broken pipeline (two independent false-stall diagnoses were filed against a
# correctly-functioning gate because of this). This NEUTRAL (never failing)
# check-run makes the decline visible on the surface an observer looks at
# first. Purely additive observability — it changes zero OMN-15247 gate
# behavior; the mint decision itself is untouched.
_MINT_STATUS_CHECK_NAME = "occ-autobind / mint status"
# Human-facing display permalink embedded in check-run output text — same
# class as a VCS display permalink; never dereferenced by code, no connection
# target, no routing-authority concern.
_MINT_STATUS_HAND_AUTHORING_URL = "https://linear.app/omninode/issue/OMN-15247"  # url-authority-ok: human-facing display permalink in check-run output text, never dereferenced by code

# OMN-14793 (OMN-14783 rec #2): the single-producer lease TTL. Floor is the
# worst-case mint duration (clone + double force-push + PR open) with margin; the
# per-git-op bound is ``_GIT_TIMEOUT_SECONDS`` (120s) x several network git ops,
# so 900s comfortably clears a slow-but-live producer while still self-healing a
# crashed one within 15 min. Too short → a live producer's lease is stolen
# mid-mint (re-introduces the race); too long → a crashed producer wedges a head.
_DEFAULT_LEASE_TTL_SECONDS = 900

# OMN-14741 F-17: emission is suppressed for a product PR that is closed, a draft,
# or explicitly marked do-not-merge — matched in the PR TITLE (case-insensitive)
# or a label name.
#
# OMN-15483: the vocabulary moved to ``omnimarket.merge_control.hold_marker``
# (imported above as ``HOLD_MARKER_RE``) and is now the SINGLE definition in the
# tree. It previously existed here AND, divergently, in
# ``node_occ_companion_compute``; the shared definition is the union of both, so
# every token this site suppressed on before still suppresses.


# OMN-14893: the OCC machine path defaults to the shared operator PAT
# (``pat`` mode, unchanged behavior) until ``ONEXBOT_OCC_APP_ID`` /
# ``ONEXBOT_OCC_PRIVATE_KEY`` are provisioned to this runtime. Flipping
# ``OMNI_OCC_GITHUB_AUTH_MODE=app`` routes through
# ``resolve_app_installation_token_from_contract`` instead, which NEVER reads
# ``GITHUB_TOKEN`` — the fallback that reproduced OMN-14893's original defect
# is not merely avoided by an ``if``, it is mechanically absent from that
# code path (see ``github_app_auth`` module docstring).
_GITHUB_AUTH_MODE_ENV_VAR = "OMNI_OCC_GITHUB_AUTH_MODE"

# OMN-15441: the product-repo-scoped credential for the one write this producer
# makes outside onex_change_control (the Evidence-Source PR-body stamp).
_PRODUCT_TOKEN_ENV_VAR = "OMNI_OCC_PRODUCT_TOKEN"


def _resolve_github_token() -> str:
    """Resolve the GitHub credential the OCC machine path authenticates with.

    ``OMNI_OCC_GITHUB_AUTH_MODE`` (OMN-14893) selects the auth path:

    * ``pat`` (default, unchanged behavior) — the contract-declared
      ``GITHUB_TOKEN`` ref, resolved via ``env_var_fallback`` (OMN-14452): the
      deployed effects lane's secret resolver is configured with an explicit
      LLM/Slack-only mapping and ``enable_convention_fallback: false``
      (delegation secrets, OMN-13861/13960) — it never resolves
      ``GITHUB_TOKEN``, which isn't an LLM secret and isn't in that mapping.
      ``GITHUB_TOKEN`` is passed straight through as a literal container env
      var (``runtime-effects.yaml`` ``required_env``), so falling back to
      reading it directly — the same mechanism already used for
      OpenRouter/Gemini provider-native names — resolves it instead of
      raising ``SecretResolutionError`` on a secret that is genuinely present
      in the environment.
    * ``app`` — mint a short-lived ``onexbot-occ-writer`` App installation
      token via ``ONEXBOT_OCC_APP_ID`` / ``ONEXBOT_OCC_PRIVATE_KEY``
      (contract-declared, required only in this mode). Raises immediately,
      naming the missing secret, if either credential is declared but
      unresolvable — no PAT fallback exists in this branch.
    """
    mode = os.environ.get(_GITHUB_AUTH_MODE_ENV_VAR, "pat").strip().lower() or "pat"
    if mode == "app":
        return resolve_app_installation_token_from_contract(_CONTRACT_PATH)
    if mode != "pat":
        raise RuntimeError(
            f"{_GITHUB_AUTH_MODE_ENV_VAR}={mode!r} is not a recognized OCC "
            "GitHub auth mode (expected 'pat' or 'app')."
        )
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


def _resolve_product_token(occ_token: str) -> tuple[str, bool]:
    """Resolve the credential for the product-repo PR-body patch (OMN-15441).

    The peer of ``HandlerOccCompanionEffect._resolve_product_token``, same
    contract-ref seam and same optional-fallback semantics.

    Why this producer needs it too, even though it works today: this emitter
    authenticates every call — including the ONE write that leaves
    ``onex_change_control``, the product PR-body PATCH in
    :meth:`_patch_evidence_source` — with :func:`_resolve_github_token`. Under
    the default ``pat`` mode that is a cross-repo operator PAT, so the patch is
    authorized and the 403 does not fire. Under ``OMNI_OCC_GITHUB_AUTH_MODE=app``
    — the planned OMN-14893 cutover — it becomes an ``onexbot-occ-writer``
    installation token whose installation is ``repository_selection: selected``
    over ``onex_change_control`` only (live readback of
    ``/orgs/OmniNode-ai/installations``, installation 148180820), which
    reproduces the exact OMN-15441 403 on the LIVE producer. Closing it now
    means the cutover is not gated on rediscovering this defect in production.

    ``required=False``: an absent product credential means the
    single-cross-repo-PAT path, which is the correct behavior for the ``.201``
    bus runtime.

    Returns:
        ``(token, dedicated)`` — ``dedicated`` is True when a distinct
        product-scoped credential resolved, False on fallback to ``occ_token``.
    """
    ref = contract_secret_ref(_CONTRACT_PATH, _PRODUCT_TOKEN_ENV_VAR)
    secret = resolve_api_key(ref, required=False, env_var_fallback=ref)
    product_token = (secret.get_secret_value() if secret is not None else "").strip()
    if product_token:
        return product_token, True
    return occ_token, False


class StaleCompanionBaseError(RuntimeError):
    """Raised on a same-ticket collision detected before a push (OMN-15845).

    ``_clone_and_branch`` performs a single shallow clone and captures
    ``base_sha`` once; the two force-pushes that follow never merge/rebase
    (OMN-16116: the freshness check itself now does a minimal single-commit
    fetch to diff trees when the remote HEAD has moved — see
    :meth:`OccCompanionEmitter._assert_base_still_fresh` — but this is a
    read-only diff, never a merge/rebase of the working branch), so a
    SIBLING companion for the SAME ticket that merges to OCC's default
    branch between this run's clone and either of its pushes is invisible to
    this run unless that check catches it. Concretely: ``contract_already_had_companion`` is
    evaluated against the stale clone snapshot, so this run silently treats
    the ticket as still companion-less, re-writes the ticket-scoped
    ``dod-occ-evidence-admissibility-validator`` receipt (meant to be
    write-once per ticket), and force-pushes a stale full-regenerate diff
    that either orphans the sibling's companion or produces an add/add
    conflict at merge time (the OMN-15845 incident: three sibling autobind
    companions minted for the same ticket ~70 minutes apart, one orphaned
    unmergeable).

    Raised instead of pushing so the caller's normal error path applies
    (the ``finally`` in :meth:`OccCompanionEmitter._emit_companion_sync`
    still releases the OMN-14793 lease). This is a fail-fast, safe-to-retry
    outcome, not a dead end: the emitter is re-invoked on the product PR's
    next lifecycle event (see ``_open_or_sync_occ_pr``'s ``synchronize``
    re-fire note), and a subsequent run clones a fresh, current base.
    """


class OccCompanionEmitter:
    """Author the OCC companion (contract + receipts) + rebind a product PR.

    A single producer for both OCC failure classes. The public methods
    (:meth:`autobind_evidence_source`, :meth:`create_occ_contract`) are thin
    entry points over the shared :meth:`_emit_companion_sync` core, so there is
    exactly one clone/branch/commit/push/PR/patch flow and one receipt vocabulary.
    """

    def __init__(
        self,
        *,
        occ_repo: str = _OCC_REPO,
        git_author_name: str = "omnimarket-bot",
        git_author_email: str = "bot@omninode.ai",
        mode: Literal["dry_run", "mutate"] = "mutate",
        runner: str = _DEFAULT_RUNNER,
        verifier: str = _DEFAULT_VERIFIER,
        producer_id: str | None = None,
        lease_ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS,
        check_binding: EnumCheckBinding | None = None,
    ) -> None:
        # OMN-15247: defer-on-contention is UNCONDITIONAL — it takes no
        # constructor argument and reads no env var, exactly like the OMN-14793
        # lease below. The check-binding mode resolves from the environment here
        # so the two no-arg construction sites
        # (handler_pr_lifecycle_fix_runtime.py and
        # handler_pr_lifecycle_orchestrator.py) need no change; tests inject
        # directly. Resolution is FAIL-CLOSED — an unrecognized value raises
        # naming the var and the accepted set, never silently defaults.
        #
        # OMN-15317: the shipped default is now CONTENT_BOUND. Because those two
        # sites construct with no kwargs and no env, the default IS production;
        # the former pr_existence default meant the contract check the OCC
        # compliance runner executes was `gh pr view … --json number,state`,
        # which exits 0 for any PR that exists — non-falsifiable, and the exact
        # defect OMN-15247 was filed for. Under CONTENT_BOUND a producer that
        # cannot derive a RED-proven probe DEFERS (see the fail-closed branch in
        # _emit_companion_sync); it never falls back to the existence probe.
        resolved_policy = resolve_occ_producer_policy(os.environ)
        self._check_binding = (
            check_binding
            if check_binding is not None
            else resolved_policy.check_binding
        )
        self._occ_repo = occ_repo
        self._git_author_name = git_author_name
        self._git_author_email = git_author_email
        self._mode = mode
        self._runner = runner
        self._verifier = verifier
        # OMN-14793: a per-host informational producer identity stamped into the
        # lease commit metadata for forensics. NOT the lease key (the lease keys on
        # PR head SHA so two hosts contend correctly regardless of identity). The
        # guard itself is ALWAYS-ON and has no enable/disable flag — an optional
        # toggle would be a silent no-check (memory feedback_optional_input…).
        self._producer_id = producer_id or f"{runner}@{socket.gethostname()}"
        self._lease_ttl_seconds = lease_ttl_seconds
        # verifier == runner is rejected at construction so a mis-wired producer
        # fails fast rather than authoring self-attesting receipts (OMN-12791).
        self._validate_verifier_not_runner(runner=runner, verifier=verifier)

    # ------------------------------------------------------------------
    # Public API — both former adapter surfaces, one core
    # ------------------------------------------------------------------

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        """Bind OCC receipt evidence for a PR and rewrite its Evidence-Source.

        The ``receipt_evidence_source_autobind`` failure class: the product PR's
        ``Evidence-Source`` points at its own head SHA (or is absent).
        """
        return await asyncio.to_thread(
            self._emit_companion_sync, repo, pr_number, ticket_id
        )

    async def create_occ_contract(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str,
        pr_head_sha: str | None = None,
    ) -> str:
        """Author the OCC companion when deploy-gate fires for a missing contract.

        The ``deploy_gate_contract_not_found`` failure class. ``pr_head_sha`` is
        accepted for call-compatibility but the authoritative head SHA is always
        re-observed live from GitHub inside the core (never a caller-supplied
        value), so a stale hint can never be stamped into a receipt.
        """
        return await asyncio.to_thread(
            self._emit_companion_sync, repo, pr_number, ticket_id
        )

    def detect_occ_gap(
        self,
        *,
        repo: str,
        pr_number: int,
        ticket_id: str,
        contract_exists: bool,
        receipt_exists: bool,
    ) -> dict[str, object]:
        """Detect whether OCC coverage is missing for a product PR (pure)."""
        if not contract_exists and not receipt_exists:
            reason = (
                f"missing contract and receipt for {ticket_id} on {repo}#{pr_number}"
            )
        elif not contract_exists:
            reason = f"missing contract for {ticket_id} on {repo}#{pr_number}"
        elif not receipt_exists:
            reason = f"missing receipt for {ticket_id} on {repo}#{pr_number}"
        else:
            return {"has_gap": False, "gap_reason": ""}
        return {"has_gap": True, "gap_reason": reason}

    # ------------------------------------------------------------------
    # verifier != runner enforcement (OMN-12791)
    # ------------------------------------------------------------------

    def _validate_verifier_not_runner(self, *, runner: str, verifier: str) -> None:
        """Raise ValueError if verifier == runner (self-attestation rejected)."""
        if runner == verifier:
            raise ValueError(
                f"self-attestation rejected: verifier ({verifier!r}) must differ "
                f"from runner ({runner!r}). Assign an independent verifier."
            )

    # ------------------------------------------------------------------
    # The one core flow
    # ------------------------------------------------------------------

    def _emit_companion_sync(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        # Dry-run: describe intent and make ZERO side effects (no token, no I/O).
        # This is a planning affordance (detect_occ_gap companion), not a
        # production path — the runtime/orchestrator always wire mutate mode.
        if self._mode == "dry_run":
            who = ticket_id or "auto-detected ticket(s)"
            action = (
                f"[dry-run] would author OCC companion for {who} on "
                f"{repo}#{pr_number} (no side effects performed)"
            )
            logger.info("occ_companion_emitter (dry-run): %s", action)
            return action

        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)

        # 1. Resolve the product PR snapshot: body, title, real head SHA + state.
        pr_data = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
        )
        body: str = pr_data.get("body") or ""
        title: str = pr_data.get("title") or ""
        head = pr_data.get("head") or {}
        head_sha = head.get("sha") if isinstance(head, dict) else None
        head_ref = head.get("ref") if isinstance(head, dict) else None
        pr_state = pr_data.get("state") or "open"
        # OMN-14766 F-16: a private product repo cannot be re-probed by the hosted
        # OCC contract-compliance runner (its token has no scope on the private
        # repo), so a `gh pr view --repo <private>` check_value fails hosted while
        # passing on this emitter (OCC#4307/#4318). Read the repo visibility from
        # the PR REST payload (`base.repo.private`) — no extra API call — so the
        # declared check_values can be rendered hosted-safe (receipt-local) below.
        is_private = self._is_private_repo(pr_data)
        if not isinstance(head_sha, str) or not SHA_RE.match(head_sha):
            raise RuntimeError(
                f"could not resolve product PR head SHA for {repo}#{pr_number}: "
                f"{head_sha!r}"
            )

        # OMN-14741 F-17: suppress companion emission for a product PR that will
        # never merge — closed, draft, or explicitly do-not-merge. Authoring a
        # companion for such a PR only manufactures queue noise and a failing
        # obsolete OCC PR (the OCC#4333 class: a companion minted for closed draft
        # `[WS4 PARITY PROBE - DO NOT MERGE]` omnimarket#1798). Fail-loud skip with
        # a reason code; ZERO side effects (no clone, no branch, no PR).
        suppression = self._suppression_reason(pr_data)
        if suppression is not None:
            action = (
                f"skip:{suppression} — {repo}#{pr_number} is not a mergeable "
                "product PR; OCC companion emission suppressed (OMN-14741 F-17)"
            )
            logger.warning("occ_companion_emitter: %s", action)
            return action

        # OMN-14255: the receipt must cite the actual squash ``mergeCommit.oid``
        # once the PR has landed — NOT the pre-merge ``headRefOid``. On these
        # squash-merge-only repos the merge commit is a brand-new SHA with no
        # ancestry to the head, so a head-SHA citation cannot satisfy the
        # DurableEvidenceGate CONTRACT_CITES_MERGE_COMMIT identity leg. When this
        # adapter runs pre-merge (its usual Evidence-Source repair timing) the
        # merge commit does not exist yet — GitHub's ``merge_commit_sha`` on an
        # OPEN PR is a throwaway test-merge SHA, so it MUST be ignored unless the
        # PR is actually merged. In that pre-merge case we fall back to the head
        # SHA; the gate's PR-commits membership leg (OMN-14255) still accepts it.
        receipt_commit_sha = self._receipt_commit_sha(pr_data, head_sha)

        # Idempotency guard: already bound to an OCC source — nothing to do.
        # Read the canonical stamp via the Piece-2 parser, not a local regex.
        #
        # OMN-16386: presence of a resolvable ``Evidence-Source: OCC#<n>`` line
        # is NOT proof this PR was ever bound — a cascade-template PR (e.g. a
        # release-cascade dependency bump opened from a template body) inherits
        # the template's Evidence-Source verbatim, naming a real OCC companion
        # PR that was minted for a DIFFERENT product PR. The old presence-only
        # check treated that as "nothing to do" and the cascade PR never got
        # its own ``occ-self-bind-pr-<n>`` receipt, stranding it at the Receipt
        # Gate (live: onex_change_control#6850, #6823, #6636 — all
        # pr_ticket_mismatch against a self-bind receipt scoped to a sibling
        # PR's number, never this one's). ``_occ_binding_matches_this_pr``
        # verifies the cited OCC PR's own branch was actually minted for THIS
        # (repo, pr_number) before trusting the no-op.
        already_bound = product_pr_occ_binding(body)
        if already_bound is not None and self._occ_binding_matches_this_pr(
            occ_pr_number=already_bound,
            repo=repo,
            pr_number=pr_number,
            token=token,
        ):
            action = (
                f"no-op: {repo}#{pr_number} already bound to "
                f"OCC#{already_bound} (Evidence-Source already an OCC source)"
            )
            logger.info("occ_companion_emitter: %s", action)
            return action
        if already_bound is not None:
            logger.warning(
                "occ_companion_emitter: %s#%s cites Evidence-Source OCC#%s but "
                "that companion's branch was not minted for this PR (inherited "
                "Evidence-Source, cascade-template class) — minting a fresh "
                "companion instead of no-op'ing (OMN-16386)",
                repo,
                pr_number,
                already_bound,
            )

        # 2. PR-TITLE ticket extraction (OMN-16376, revising OMN-13990 D3): the
        #    gate's own identity axis is title-anchored (see _extract_tickets'
        #    docstring), so the companion is authored for every ticket cited in
        #    the PR TITLE — never the body. Fall back to the caller-supplied
        #    ticket only when the title cites none.
        tickets = self._extract_tickets(title)
        if not tickets and ticket_id:
            tickets = [ticket_id]
        if not tickets:
            raise RuntimeError(
                f"could not detect any OMN-XXXX ticket id from {repo}#{pr_number} "
                "title/body; cannot author an OCC companion."
            )

        repo_slug = repo.replace("/", "-")
        # One OCC branch/PR per product PR (ticket-count agnostic) so a single
        # Evidence-Source: OCC#<n> covers every cited ticket and re-fires sync.
        branch = self._occ_branch_name(repo=repo, pr_number=pr_number)
        evidence_id = f"dod-{repo_slug}-pr-{pr_number}"
        ci_evidence_id = ci_check_evidence_id(evidence_id)

        # OMN-15247 deliverable A — DEFER-ON-CONTENTION, ALWAYS ON. Placed AFTER
        # the already-bound idempotency check and ticket extraction, and BEFORE
        # ``acquire_occ_companion_lease``, so a defer takes the lease-free,
        # zero-side-effect path (no lease, no clone, no branch, no push, no PR,
        # no ``_patch_evidence_source``).
        #
        # There is no policy argument and no observe mode: a guard that fires
        # only when an operator opts in is not a guard (memory
        # feedback_optional_input_means_the_check_does_not_exist), and the
        # displacement it prevents is structurally unrecoverable once the hollow
        # contract merges (OCC is append-only; the repair is rejected with
        # pr_ticket_mismatch). Same posture as the lease guard below.
        findings = self._find_contending_companions(
            tickets=tickets, own_branch=branch, token=token
        )
        should_defer, contention_reason = decide_contention(findings)
        for finding in findings:
            logger.warning(
                "occ_companion_emitter contention: ticket=%s occ_pr=%s "
                "provenance=%s defer=%s reason=%s",
                finding.ticket_id,
                finding.occ_pr_number,
                finding.provenance.value,
                should_defer,
                finding.reason,
            )
        if should_defer:
            action = (
                f"skip:DEFER_HAND_AUTHORED — {repo}#{pr_number}: "
                f"{contention_reason} (OMN-15247)"
            )
            logger.warning("occ_companion_emitter: %s", action)
            self._comment_deferred(
                repo=repo,
                pr_number=pr_number,
                findings=findings,
                token=token,
            )
            return action

        run_timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Genuine product-PR probe, observed once and shared across tickets.
        #
        # OMN-15247 R21b: restored to the pre-R21 form. R21 moved this probe to
        # ``gh api .../pulls/<n>/files --jq '.[].sha'`` so the recorded probe would
        # match the declared check_value. That check_value is reverted (it was a
        # PR-existence probe: exit 0 for every PR on GitHub that changes a file),
        # so the probe returns with it. Where a content-bound check IS derivable
        # this variable is overwritten below with that check -- which is a genuine
        # product observation, RED-proven at the merge base.
        downstream_probe_command = (
            f"gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
        )
        downstream_stdout, downstream_exit = self._observe_pr_probe(
            probe_command=downstream_probe_command,
            token=token,
            fallback={
                "number": pr_number,
                "state": pr_state,
                "headRefName": head_ref or head_sha,
            },
        )

        # OMN-14425 / OMN-14650: a second, falsifiable claim alongside the
        # existence probe above — the product PR's changed-file list. It derives
        # proof tier L1 (substance floor's diff-assert family: `--json files` names
        # the files the PR touches) and satisfies the OMN-14409 substance floor
        # WITHOUT gating on the source PR's CI being green (the deadlock the former
        # `gh pr checks <source>` probe created). The existence probe is kept, not
        # replaced; this adds a claim.
        #
        # OMN-14766 F-06: the RUNTIME probe is the GraphQL `gh pr view --json files`,
        # NOT the REST-fragile `gh pr diff ... --name-only` (which returned HTML/503
        # during a GitHub REST incident — OCC#4297). OMN-14741 already moved the
        # declared receipt/contract check_value to `--json files`; this closes the
        # remainder so the emitter's own probe matches the check_value it declares
        # (`probe_command == check_value` on the public path). `gh pr view --json
        # files` is pipe-free JSON, so _observe_pr_probe can shlex.split + json.loads
        # it directly.
        # OMN-15247 R21b: restored to the F-06 GraphQL form. R21 moved it to
        # `gh api .../pulls/<n>/files --jq '.[].status'` so the recorded probe
        # matched the (then-vacuous) declared check_value; with that check_value
        # reverted, keeping the probe on `gh api` would break the F-06 invariant
        # that the emitter's own probe matches the check_value it declares.
        ci_probe_command = f"gh pr view {pr_number} --repo {repo} --json files"
        ci_stdout, ci_exit = self._observe_pr_probe(
            probe_command=ci_probe_command,
            token=token,
            fallback={"number": pr_number, "note": "diff not observed"},
        )

        # OMN-16892: the product PR's changed-file list, read out of the probe
        # this producer ALREADY ran one line above. It decides whether the
        # companion can carry a diff-derived BEHAVIOR check or must instead
        # state what proof is OWED — see render_companion_contract.
        #
        # Sourced from that probe rather than a fresh `/pulls/<n>/files` fetch on
        # purpose: this is the exact payload the CI receipt records, so the
        # diff the contract is derived FROM and the diff the receipt ATTESTS TO
        # are one observation. A second fetch could disagree with the first
        # (a push between the two calls) and nothing would notice.
        #
        # Fail-closed: `_observe_pr_probe` returns its `{"number":..,"note":..}`
        # fallback whenever `gh` is unavailable or errors, which carries no
        # `files` key and so parses to (), which takes the OWED branch. An
        # unobservable diff yields an honest "unproven" statement, never a
        # surrogate that reads as proof.
        changed_files = changed_files_from_diff_scope_probe(ci_stdout)
        behavior_test_paths = derive_behavior_test_paths(changed_files)

        # The final (admissibility) dod_evidence slot: WHICH item fills it, what
        # it declares, and — load-bearing — the receipt FILENAME that backs it.
        # A PR-level fact, not a per-ticket one: every cited ticket's companion
        # is derived from the same diff, so deriving it once here keeps the
        # contract renderer, the receipt writer, the rebind pass and the
        # append-only allowed-path set reading one answer. A second derivation
        # would be a second thing to drift.
        if behavior_test_paths:
            slot_evidence_id = BEHAVIOR_PROOF_EVIDENCE_ID
            slot_check_type = "test_passes"
            slot_check_value = behavior_proof_check_value(behavior_test_paths)
            # HONESTY, since this is the field most easily faked: the declared
            # check runs in the PRODUCT repo at `cwd`, and this emitter runs
            # inside that repo's CI against the GitHub API, never a checkout of
            # it. So probe_command / probe_stdout / exit_code stay the live PR
            # read that ACTUALLY ran, and actual_output names which surface
            # executes the declared check. Writing a fabricated "N passed" here
            # is precisely the false-evidence class the Receipt Honesty Gate
            # exists to catch.
            slot_actual_output = (
                "PASS: product-repo test run executes the declared check; "
                "probe is the live PR read."
            )
        else:
            slot_evidence_id = ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
            slot_check_type = "command"
            slot_check_value = ADMISSIBILITY_VALIDATOR_CHECK_VALUE
            slot_actual_output = (
                "PASS: OCC runner executes the declared check; "
                "probe is the live PR read."
            )
        logger.info(
            "occ_companion_emitter behavior-proof: %s#%s changed=%d targets=%s",
            repo,
            pr_number,
            len(changed_files),
            list(behavior_test_paths) or "none (OWED branch)",
        )

        # OMN-14766 F-16: for a private product repo the DECLARED check_value the
        # hosted OCC runner re-runs is a receipt-local grep (computed per ticket
        # below, since the receipt path is ticket-scoped) instead of a
        # `gh pr view --repo <private>` re-probe. The live probe above still runs on
        # THIS emitter (which has repo scope) and is recorded in each receipt's
        # probe_command/probe_stdout/exit_code. Public repos keep None so the
        # OMN-14741 shape is preserved byte-for-byte.
        # OMN-15247 deliverable B — CONTENT-BOUND CHECKS. Derive a RED-proven
        # content read for the DOWNSTREAM item so the CONTRACT's declared check —
        # the one the OCC contract-compliance runner actually executes — is
        # falsifiable, not `gh pr view --json number,state` (which exits 0 for any
        # PR that exists, in any state, with any diff: the OMN-15247 defect).
        # OMN-14619 already computed a content read but only ever landed it in a
        # RECEIPT's check_value, which the runner does not execute — provenance,
        # never a gate.
        #
        # Private product repos keep the OMN-14766 F-16 hosted-safe receipt-local
        # form: a hosted `gh api …/contents` has no token scope on a private repo.
        # The `--json files` diff-scope item is UNCHANGED — it carries the
        # OMN-14409 substance floor and removing it is not in scope.
        content_bound_check: str | None = None
        content_bound_red_ref: str | None = None
        content_bound_red_exit: int | None = None
        if not is_private:
            content_bound_check, content_bound_red_ref, content_bound_red_exit = (
                self._derive_content_bound_check(
                    repo=repo,
                    owner=owner,
                    repo_name=repo_name,
                    pr_number=pr_number,
                    pr_data=pr_data,
                    evidence_ref=receipt_commit_sha,
                    token=token,
                )
            )
        if (
            self._check_binding is EnumCheckBinding.CONTENT_BOUND
            and not is_private
            and content_bound_check is None
        ):
            # FAIL-CLOSED (§B4): under content_bound a producer that cannot derive
            # a RED-proven check must NOT silently fall back to the hollow
            # existence probe — that silent fallback is exactly the behavior
            # OMN-15247 files as a defect, and would make this flag cosmetic.
            action = (
                f"skip:NO_RED_DERIVABLE_CHECK — {repo}#{pr_number}: no changed-file "
                "candidate is RED-derivable against the merge base; hand-authored "
                "evidence is required (OMN-15247)"
            )
            logger.warning("occ_companion_emitter: %s", action)
            self._comment_no_red_derivable(repo=repo, pr_number=pr_number, token=token)
            self._post_mint_status_check_run(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                token=token,
                reason="no-red-derivable",
                summary=(
                    "OCC autobind did not mint a companion for this PR: no "
                    "changed-file candidate could be proven RED against the "
                    "merge base, and emitting a PR-existence probe instead "
                    "would be non-falsifiable evidence (OMN-15247). "
                    "Hand-authored evidence is required — see "
                    f"{_MINT_STATUS_HAND_AUTHORING_URL}."
                ),
            )
            return action

        # §B3.7 — record the RED derivation in the receipt's EXISTING free-text
        # fields. ``ModelDodReceipt`` is ``extra="forbid"`` and frozen, so no
        # ``red_derivation:`` key can be invented; ``probe_command`` /
        # ``probe_stdout`` / ``actual_output`` are the only schema-compatible
        # carriers. ``probe_stdout`` stays a single compact JSON line so the YAML
        # block scalar shape is preserved exactly as today.
        downstream_actual_output: str | None = None
        if (
            self._check_binding is EnumCheckBinding.CONTENT_BOUND
            and content_bound_check is not None
        ):
            downstream_probe_command = content_bound_check
            downstream_stdout = json.dumps(
                {
                    "evidence_ref": receipt_commit_sha,
                    "green_exit": 0,
                    "red_ref": content_bound_red_ref,
                    "red_exit": content_bound_red_exit,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            downstream_exit = 0
            downstream_actual_output = (
                f"PASS: content-bound probe GREEN at {receipt_commit_sha}, "
                f"RED at merge-base {content_bound_red_ref} "
                f"(exit {content_bound_red_exit})."
            )

        def _hosted_safe_check_values(ticket: str) -> tuple[str | None, str | None]:
            # OMN-15247 R21: the OMN-14766 F-16 private-repo branch is GONE. It
            # returned the receipt-local grep for BOTH checks, which the
            # OMN-15309 predicate refuses unconditionally as INSIDE_OWN_DIFF —
            # the producer-side cause of the three-for-three born-red companions
            # (OCC#5406 / #5415 / #5418). ``None`` now means "use the defaults".
            #
            # OMN-15407: the defaults are now the LITERAL PR-pinned form
            # (occ_evidence_stamp.downstream_dod_evidence_check_value /
            # ci_dod_evidence_check_value), not the placeholder-var form — the
            # placeholder form is a Rule B violation on both items regardless of
            # repo privacy, since their ids always embed the PR number. This DOES
            # mean a private-repo companion's binding/diff-scope items now name
            # the private repo in a ``gh pr view --repo <private>`` command the
            # hosted OCC token cannot read; that is deliberate, not an oversight
            # left over from the F-16 removal above -- the OMN-15309 predicate
            # classifies ``gh pr view`` as inadmissible regardless of literal vs.
            # placeholder form, so ``_demote()`` downgrades it to WARN whatever it
            # returns (PASS or a 403/404 BLOCK), the same as the placeholder form
            # always did. Only the LITERAL content-bound pin below (a `gh api
            # .../contents/...` read, not `gh pr view`) genuinely needs
            # ``is_private`` to suppress it, because that value backs a PASS/BLOCK
            # verdict the predicate treats as admissible.
            if is_private:
                return None, None
            if (
                self._check_binding is EnumCheckBinding.CONTENT_BOUND
                and content_bound_check is not None
            ):
                return content_bound_check, None
            return None, None

        # OMN-14793 (OMN-14783 rec #2) single-producer lease: atomically claim
        # this product PR head in the shared OCC repo BEFORE any clone/branch/
        # push. Two producers (the local merge_sweep mint path and the .201
        # effects lane) build independent OccCompanionEmitter instances on
        # different hosts with no shared in-process state and force-push the
        # SAME deterministic auto/* branch, so "branch exists" is not a
        # discriminator. First-acquirer-wins keyed on the PR head SHA; a second
        # concurrent producer no-ops here with ZERO side effects — closing the
        # OCC#4406 dual-producer race that let a stale mint land first.
        lease_ok = acquire_occ_companion_lease(
            token=token,
            repo_slug=repo_slug,
            pr_number=pr_number,
            head_sha=head_sha,
            producer_id=self._producer_id,
            lease_ttl_seconds=self._lease_ttl_seconds,
            occ_repo=self._occ_repo,
        )
        if not lease_ok:
            action = (
                f"skip:LEASE_HELD — {repo}#{pr_number}@{head_sha[:8]} companion "
                "already being minted by another producer (OMN-14793 / OMN-14783)"
            )
            logger.warning("occ_companion_emitter: %s", action)
            return action

        try:
            with tempfile.TemporaryDirectory(prefix="occ-companion-") as tmpdir:
                clone_dir = Path(tmpdir) / "onex_change_control"
                base_sha = self._clone_and_branch(clone_dir, branch, tmpdir, token)

                contract_paths: dict[str, Path] = {}
                # OMN-15785: per-ticket "did this ticket already have a
                # companion before THIS run's clone" signal, captured BEFORE
                # any write can change contract_path.is_file()'s answer.
                # Mirrors node_occ_companion_compute's `state.exists and
                # state.merged` guard (OMN-15485) — that ticket fixed the
                # identical defect on the sibling compute-oracle path but
                # never ported the guard here, and this producer regressed
                # the exact same class: the admissibility-validator receipt's
                # evidence_item_id is a fixed, ticket-scoped constant (NOT
                # PR-scoped like the downstream/CI/self-bind ids), so a
                # SECOND companion for a ticket that already has one collides
                # on the SAME path a prior companion already merged. Live
                # incident: OCC#6264 (OMN-15789, omnibase_core#1550) merged
                # first; OCC#6276 (same ticket, omnibase_infra#2705) then
                # unconditionally rewrote #6264's already-merged
                # dod-occ-evidence-admissibility-validator receipt, tripping
                # the OCC Append-Only Gate (hand-repaired,
                # onex_change_control@6240bf817, 2026-08-09).
                contract_already_had_companion: dict[str, bool] = {}
                # OMN-16071 Defect 1: the guard above is a question about
                # ``contracts/<ticket>.yaml`` used to decide whether to open
                # ``drift/dod_receipts/<ticket>/<slot_evidence_id>/
                # <slot_check_type>.yaml`` for write. Two different files. Its
                # sibling emission sites in this same loop ask the DIRECT
                # question (``downstream_receipt_path.is_file()`` /
                # ``ci_receipt_path.is_file()``); this one never did, so the
                # add-only property held only while the two files happened to
                # co-exist. Where they diverge — a receipt tree that outlived
                # its contract, or a contract re-keyed to a different ticket
                # (OMN-16376) — the writer opens an already-merged receipt, and
                # since PR #2086 the pre-push ``_assert_append_only`` then
                # aborts the WHOLE mint on git status rather than the product
                # PR merely losing one file. Captured here, before any write
                # can change the answer, exactly like its sibling.
                slot_receipt_already_present: dict[str, bool] = {}
                # OMN-16356: per-ticket downstream/CI skip flags, populated in
                # this loop and read again by the pass-2 self-bind rebind below
                # — see the net-new-file-only guard at the receipt writes.
                downstream_already_merged_by_ticket: dict[str, bool] = {}
                ci_already_merged_by_ticket: dict[str, bool] = {}
                for ticket in tickets:
                    downstream_check_value, ci_check_value = _hosted_safe_check_values(
                        ticket
                    )
                    contract_path = clone_dir / "contracts" / f"{ticket}.yaml"
                    contract_path.parent.mkdir(parents=True, exist_ok=True)
                    contract_already_had_companion[ticket] = contract_path.is_file()
                    slot_receipt_already_present[ticket] = (
                        clone_dir
                        / "drift"
                        / "dod_receipts"
                        / ticket
                        / slot_evidence_id
                        / f"{slot_check_type}.yaml"
                    ).is_file()
                    if not contract_path.is_file():
                        contract_path.write_text(
                            render_companion_contract(
                                ticket_id=ticket,
                                repo=repo,
                                pr_number=pr_number,
                                evidence_id=evidence_id,
                                downstream_check_value=downstream_check_value,
                                ci_check_value=ci_check_value,
                                changed_files=changed_files,
                            ),
                            encoding="utf-8",
                        )
                    else:
                        # OMN-14741 F-04: a PRE-EXISTING contract (a prior ticket
                        # already owns contracts/<ticket>.yaml) does NOT declare THIS
                        # PR's base rows. Without them the freshly-written
                        # downstream/CI receipts bind to a dod_evidence item that does
                        # not exist, leaving contract_entry_sha256=PENDING and breaking
                        # eligibility (the OCC#4304 class). Append the two base rows
                        # structurally (robust to a non-dod_evidence-terminal contract).
                        self._ensure_base_dod_evidence(
                            contract_path,
                            repo=repo,
                            pr_number=pr_number,
                            evidence_id=evidence_id,
                            ci_evidence_id=ci_evidence_id,
                            downstream_check_value=downstream_check_value,
                            ci_check_value=ci_check_value,
                        )
                    contract_paths[ticket] = contract_path

                    # Stage 1: downstream receipt stamped with the actual landed
                    # commit — the squash mergeCommit.oid post-merge, else the
                    # reviewed head SHA pre-merge (OMN-14255).
                    #
                    # OMN-16356 NET-NEW-FILE-ONLY GUARD (case 2 of the shared
                    # regression this ticket also covers): unlike the
                    # ticket-shared admissibility-validator id below, these two
                    # ids embed the product PR number and were assumed to be
                    # unique-per-run by construction. That assumption breaks
                    # when a PRIOR companion for this EXACT repo#pr already
                    # merged under an Evidence-Source stamp this PR's body no
                    # longer carries (the OMN-16386 inherited/stale-stamp
                    # class) — the idempotency guard above correctly decides to
                    # mint a fresh companion, but these paths already exist at
                    # the clone base, so unconditionally opening them for write
                    # trips the append-only guard on a genuine live incident
                    # (omnibase_infra#2766, 2026-08-23T18:32:07Z). Never
                    # reopen an already-merged receipt for write — skip,
                    # exactly like the admissibility-validator guard just below.
                    downstream_dir = (
                        clone_dir / "drift" / "dod_receipts" / ticket / evidence_id
                    )
                    downstream_dir.mkdir(parents=True, exist_ok=True)
                    downstream_receipt_path = downstream_dir / "command.yaml"
                    downstream_already_merged = downstream_receipt_path.is_file()
                    downstream_already_merged_by_ticket[ticket] = (
                        downstream_already_merged
                    )
                    if downstream_already_merged:
                        logger.info(
                            "occ_companion_emitter: skipping downstream receipt "
                            "%s for %s#%s — a companion for this exact PR already "
                            "merged it (OMN-16356 net-new-file-only guard, never "
                            "overwrite).",
                            evidence_id,
                            repo,
                            pr_number,
                        )
                    else:
                        downstream_receipt_path.write_text(
                            render_downstream_receipt(
                                ticket_id=ticket,
                                evidence_id=evidence_id,
                                pr_number=pr_number,
                                repo=repo,
                                run_timestamp=run_timestamp,
                                commit_sha=receipt_commit_sha,
                                branch=branch,
                                probe_command=downstream_probe_command,
                                probe_stdout=downstream_stdout,
                                exit_code=downstream_exit,
                                actual_output=downstream_actual_output,
                                runner=self._runner,
                                verifier=self._verifier,
                                check_value=downstream_check_value,
                            ),
                            encoding="utf-8",
                        )

                    # Stage 1b: CI-outcome receipt (OMN-14425) — backs the second,
                    # substantive dod_evidence item declared alongside the existence
                    # probe above. Same OMN-16356 net-new-file-only guard as above.
                    ci_dir = (
                        clone_dir / "drift" / "dod_receipts" / ticket / ci_evidence_id
                    )
                    ci_dir.mkdir(parents=True, exist_ok=True)
                    ci_receipt_path = ci_dir / "command.yaml"
                    ci_already_merged = ci_receipt_path.is_file()
                    ci_already_merged_by_ticket[ticket] = ci_already_merged
                    if ci_already_merged:
                        logger.info(
                            "occ_companion_emitter: skipping CI receipt %s for "
                            "%s#%s — a companion for this exact PR already merged "
                            "it (OMN-16356 net-new-file-only guard, never "
                            "overwrite).",
                            ci_evidence_id,
                            repo,
                            pr_number,
                        )
                    else:
                        ci_receipt_path.write_text(
                            render_ci_check_receipt(
                                ticket_id=ticket,
                                evidence_id=ci_evidence_id,
                                pr_number=pr_number,
                                repo=repo,
                                run_timestamp=run_timestamp,
                                commit_sha=receipt_commit_sha,
                                branch=branch,
                                probe_command=ci_probe_command,
                                probe_stdout=ci_stdout,
                                exit_code=ci_exit,
                                runner=self._runner,
                                verifier=self._verifier,
                                check_value=ci_check_value,
                            ),
                            encoding="utf-8",
                        )
                    # OMN-15247 R21b: receipt backing the minted
                    # admissibility-validator item. REQUIRED on a ticket's
                    # FIRST companion -- validator_occ_merge_eligibility
                    # refuses a companion whose contract declares a
                    # dod_evidence item with no PASS receipt (MISSING_RECEIPT),
                    # so declaring the item without this would trade
                    # born-BLOCKED for born-INELIGIBLE.
                    #
                    # OMN-15785 NET-NEW-FILE-ONLY GUARD: this item's id
                    # (ADMISSIBILITY_VALIDATOR_EVIDENCE_ID) is a fixed,
                    # ticket-scoped constant, unlike evidence_id/ci_evidence_id
                    # which embed the product PR number and so can never
                    # collide across companions. When this ticket ALREADY had
                    # a companion before this run (contract pre-existed), that
                    # prior companion's admissibility receipt is either
                    # already merged (immutable) or already on this run's own
                    # generated tree — either way this run must never write to
                    # it. Mirrors node_occ_companion_compute's `state.exists
                    # and state.merged` guard (OMN-15485), which fixed the
                    # identical defect on the sibling compute-oracle path.
                    #
                    # probe_command / probe_stdout / exit_code record the live
                    # product-PR probe this emitter ACTUALLY ran, exactly as the
                    # downstream receipt above does; check_value names the check
                    # the OCC contract-compliance runner executes at CI time. The
                    # emitter has the PRODUCT repo's checkout, never OCC's, so it
                    # cannot run `uv run pytest tests/test_evidence_admissibility
                    # .py` -- and fabricating an "N passed" probe_stdout is the
                    # false-evidence class this ticket removes.
                    #
                    # OMN-16892: WHICH item occupies that slot now depends on the
                    # PR's diff, so the receipt minted here follows the SAME branch
                    # `render_companion_contract` took above. Exactly ONE receipt is
                    # minted either way -- minting the other would declare a receipt
                    # for an item the contract does not carry, which
                    # `check_receipt_hardening` reports as an orphan.
                    #
                    # The receipt FILENAME is the declared `check_type`, not a fixed
                    # `command.yaml`: eligibility resolves an item's receipt at
                    # `<evidence_id>/<check_type>.yaml`, and the behavior item
                    # declares `test_passes`. Hardcoding `command.yaml` here is
                    # exactly the OMN-16859 defect on the sibling compute producer,
                    # whose behavior receipts have had to be hand-authored.
                    #
                    # OMN-16071: skip on EITHER signal. The receipt-path half
                    # is the one the ticket's AC names ("never open an existing
                    # receipt file for write"); the contract half is retained
                    # deliberately, because minting a slot receipt into a
                    # pre-existing contract that does not declare that item
                    # would trade an append-only violation for an orphan
                    # receipt (``check_receipt_hardening``).
                    if (
                        contract_already_had_companion[ticket]
                        or slot_receipt_already_present[ticket]
                    ):
                        logger.info(
                            "occ_companion_emitter: skipping "
                            "%s receipt for %s — prior companion: %s, receipt "
                            "already at clone base: %s; the item is "
                            "ticket-shared and already-merged/"
                            "already-generated (OMN-15785 net-new-file-only "
                            "guard + OMN-16071 add-only writer, never "
                            "overwrite).",
                            slot_evidence_id,
                            ticket,
                            contract_already_had_companion[ticket],
                            slot_receipt_already_present[ticket],
                        )
                    else:
                        validator_dir = (
                            clone_dir
                            / "drift"
                            / "dod_receipts"
                            / ticket
                            / slot_evidence_id
                        )
                        validator_dir.mkdir(parents=True, exist_ok=True)
                        (validator_dir / f"{slot_check_type}.yaml").write_text(
                            render_downstream_receipt(
                                ticket_id=ticket,
                                evidence_id=slot_evidence_id,
                                pr_number=pr_number,
                                repo=repo,
                                run_timestamp=run_timestamp,
                                commit_sha=receipt_commit_sha,
                                branch=branch,
                                probe_command=downstream_probe_command,
                                probe_stdout=downstream_stdout,
                                exit_code=downstream_exit,
                                # SHORT deliberately -- yamlfmt folds a long plain
                                # scalar at column 100 and restales the hash
                                # (F-03 / OMN-14684).
                                actual_output=slot_actual_output,
                                runner=self._runner,
                                verifier=self._verifier,
                                check_value=slot_check_value,
                                check_type=slot_check_type,
                            ),
                            encoding="utf-8",
                        )
                    # OMN-14741 F-01: rebind ONLY this PR's own receipts, never rglob
                    # every receipt under <ticket>/. The whole-file contract_sha256 of
                    # a PRIOR merged receipt for the same ticket goes stale when the
                    # contract grows, but the eligibility/receipt gates grandfather a
                    # prior merged receipt's whole-file hash — so rewriting it here is
                    # a NON-append-only mutation of an already-merged receipt (the
                    # OCC#4293/4295/4296 class). Scope the rebind to this PR's rows.
                    #
                    # OMN-15785: the admissibility id is included ONLY when this
                    # run itself just minted it (fresh ticket) — when it was
                    # skipped above (pre-existing ticket) it must never be
                    # rebound either, or the "skip the write" guard above would
                    # be defeated by this rebind pass mutating the same merged
                    # file's contract_sha256/contract_entry_sha256 fields.
                    #
                    # OMN-16356: same exclusion for the downstream/CI ids when
                    # THIS run skipped writing them (already-merged-elsewhere,
                    # see the net-new-file-only guard above) — rebinding a
                    # receipt this run never opened is itself the mutation the
                    # guard exists to prevent.
                    rebind_evidence_ids: set[str] = set()
                    if not downstream_already_merged:
                        rebind_evidence_ids.add(evidence_id)
                    if not ci_already_merged:
                        rebind_evidence_ids.add(ci_evidence_id)
                    if not (
                        contract_already_had_companion[ticket]
                        or slot_receipt_already_present[ticket]
                    ):
                        rebind_evidence_ids.add(slot_evidence_id)
                    self._rebind_receipts(
                        clone_dir,
                        ticket,
                        contract_path,
                        rebind_evidence_ids,
                    )

                self._run_git(["git", "add", "contracts", "drift"], cwd=str(clone_dir))
                self._run_git(
                    [
                        "git",
                        "commit",
                        "-m",
                        (
                            f"evidence({', '.join(tickets)}): author OCC companion "
                            f"for {repo}#{pr_number}\n\n"
                            f"OCC companion by node_pr_lifecycle_fix_effect "
                            f"(OMN-13317 F1 / OMN-13990 / OMN-14285). "
                            f"Product PR head {head_sha}."
                        ),
                    ],
                    cwd=str(clone_dir),
                )
                # OMN-14741 F-01: fail CLOSED before pushing if the generated tree
                # touched anything outside this run's contract + receipt set. This is a
                # real diff against the clone base, not the assertion-only comment the
                # force-push previously relied on.
                #
                # OMN-15785: the admissibility path is allowed ONLY for tickets
                # that did not already have a companion — the same tickets the
                # write/rebind guards above actually touched. Widening this to
                # every ticket would silently re-permit the exact overwrite the
                # write guard just refused to perform.
                self._assert_append_only(
                    clone_dir,
                    base_sha,
                    self._allowed_paths(tickets, {evidence_id, ci_evidence_id})
                    | self._allowed_paths(
                        [
                            t
                            for t in tickets
                            if not (
                                contract_already_had_companion[t]
                                or slot_receipt_already_present[t]
                            )
                        ],
                        {slot_evidence_id},
                        filename=f"{slot_check_type}.yaml",
                    ),
                )
                # Force-push: the auto/* bot branch is fully REGENERATED each run
                # (fresh clone off the default + freshly-timestamped receipts), so a
                # `synchronize` re-fire produces history disjoint from the already
                # pushed remote branch — a plain push would be rejected non-fast-
                # forward (OMN-13990 / CodeRabbit). Force-push is safe here (content
                # is deterministic and the branch always presents the companion as
                # all-adds relative to base, keeping the append-only gate green).
                #
                # OMN-15845: verify OCC's default branch has not moved past
                # ``base_sha`` immediately before the push — see
                # :meth:`_assert_base_still_fresh` / :class:`StaleCompanionBaseError`.
                self._assert_base_still_fresh(
                    base_sha=base_sha, token=token, cwd=str(clone_dir), tickets=tickets
                )
                self._run_git(
                    ["git", "push", "--force", "origin", branch], cwd=str(clone_dir)
                )

                # 3. Open or sync the OCC binding PR (one per product PR).
                occ_pr_number = self._open_or_sync_occ_pr(
                    branch=branch, ticket=tickets[0], repo=repo, pr_number=pr_number
                )

                # Genuine OCC-PR probe for the self-bind receipts.
                occ_owner, occ_repo_name = split_repo(self._occ_repo)
                occ_pr_data = rest_json(
                    "GET",
                    f"/repos/{occ_owner}/{occ_repo_name}/pulls/{occ_pr_number}",
                    token=token,
                )
                occ_state = occ_pr_data.get("state") or "open"
                occ_head_sha = self._head_sha(str(clone_dir))
                occ_probe_command = (
                    f"gh api repos/{self._occ_repo}/pulls/{occ_pr_number}/files "
                    "--paginate --jq '.[].sha'"
                )
                occ_stdout, occ_exit = self._observe_pr_probe(
                    probe_command=occ_probe_command,
                    token=token,
                    fallback={"number": occ_pr_number, "state": occ_state},
                )

                # Stage 2: self-binding receipt per ticket with the REAL OCC PR + head.
                self_bind_evidence_id = f"occ-self-bind-pr-{occ_pr_number}"
                for ticket in tickets:
                    self_bind_dir = (
                        clone_dir
                        / "drift"
                        / "dod_receipts"
                        / ticket
                        / self_bind_evidence_id
                    )
                    self_bind_dir.mkdir(parents=True, exist_ok=True)
                    (self_bind_dir / "command.yaml").write_text(
                        render_self_bind_receipt(
                            ticket_id=ticket,
                            evidence_id=self_bind_evidence_id,
                            occ_pr_number=occ_pr_number,
                            occ_repo=self._occ_repo,
                            run_timestamp=run_timestamp,
                            occ_commit_sha=occ_head_sha,
                            branch=branch,
                            probe_command=occ_probe_command,
                            probe_stdout=occ_stdout,
                            exit_code=occ_exit,
                            runner=self._runner,
                            verifier=self._verifier,
                        ),
                        encoding="utf-8",
                    )
                    # OMN-14650: register the self-bind item in the contract's
                    # dod_evidence BEFORE recomputing contract_sha256, so
                    # validator_occ_merge_eligibility actually evaluates the self-bind
                    # receipt — the ONLY receipt bound to the OCC companion PR. Without
                    # this the receipt is written but never inspected and every auto/*
                    # companion fails eligibility with pr_ticket_mismatch. The rebind
                    # below then binds the self-bind receipt's per-entry hash (now that
                    # the entry exists) and the whole-file hash across ALL receipts.
                    self._append_self_bind_evidence(
                        contract_paths[ticket],
                        evidence_id=self_bind_evidence_id,
                        occ_pr_number=occ_pr_number,
                        ticket_id=ticket,
                    )
                    # OMN-14741 F-01: rebind this PR's own three receipts (downstream,
                    # CI, self-bind) against the now-final contract — never the whole
                    # ticket. The per-entry hash is append-invariant; only these fresh
                    # receipts need the current whole-file hash.
                    #
                    # OMN-15785: the admissibility id joins this rebind pass
                    # ONLY when this ticket had no PRIOR companion — i.e. only
                    # when THIS run minted the receipt in pass 1 above. When
                    # the ticket already had a companion the receipt was
                    # skipped in pass 1 and must not be touched here either;
                    # its whole-file contract_sha256 stays whatever the prior,
                    # already-merged companion bound it to (the eligibility
                    # gate grandfathers that value — see the F-01 note two
                    # blocks up).
                    #
                    # OMN-16356: same exclusion for downstream/CI when THIS
                    # run skipped writing them in pass 1 (a companion for this
                    # exact PR already merged them — the net-new-file-only
                    # guard above).
                    pass2_rebind_evidence_ids = {self_bind_evidence_id}
                    if not downstream_already_merged_by_ticket[ticket]:
                        pass2_rebind_evidence_ids.add(evidence_id)
                    if not ci_already_merged_by_ticket[ticket]:
                        pass2_rebind_evidence_ids.add(ci_evidence_id)
                    if not (
                        contract_already_had_companion[ticket]
                        or slot_receipt_already_present[ticket]
                    ):
                        # OMN-15247 R21b: pass 2 re-renders the contract with
                        # the self-bind entry appended, so its whole-file
                        # digest changes and EVERY pass-1 receipt THIS RUN
                        # minted must be rebound -- the validator's included,
                        # or it ships a stale contract_sha256 and fails the
                        # receipt gate.
                        pass2_rebind_evidence_ids.add(slot_evidence_id)
                    self._rebind_receipts(
                        clone_dir,
                        ticket,
                        contract_paths[ticket],
                        pass2_rebind_evidence_ids,
                    )

                self._run_git(["git", "add", "contracts", "drift"], cwd=str(clone_dir))
                self._run_git(
                    [
                        "git",
                        "commit",
                        "-m",
                        (
                            f"evidence({', '.join(tickets)}): self-bind "
                            f"OCC#{occ_pr_number} + rebind contract_sha256"
                        ),
                    ],
                    cwd=str(clone_dir),
                )
                # OMN-14741 F-01: re-assert append-only over the FINAL tree (both
                # commits) before the deterministic all-adds force-push.
                #
                # OMN-15785: same admissibility-path narrowing as the pass-1
                # assertion above — allowed only for tickets this run actually
                # minted/rebound the receipt for.
                self._assert_append_only(
                    clone_dir,
                    base_sha,
                    self._allowed_paths(
                        tickets,
                        {evidence_id, ci_evidence_id, self_bind_evidence_id},
                    )
                    | self._allowed_paths(
                        [
                            t
                            for t in tickets
                            if not (
                                contract_already_had_companion[t]
                                or slot_receipt_already_present[t]
                            )
                        ],
                        {slot_evidence_id},
                        filename=f"{slot_check_type}.yaml",
                    ),
                )
                # Force-push (see rationale above): deterministic all-adds regeneration.
                #
                # OMN-15845: re-verify freshness immediately before THIS push too —
                # a sibling companion can land between the first and second push of
                # the same run (two GitHub round-trips — open-or-sync PR + self-bind
                # probe — separate them).
                self._assert_base_still_fresh(
                    base_sha=base_sha, token=token, cwd=str(clone_dir), tickets=tickets
                )
                self._run_git(
                    ["git", "push", "--force", "origin", branch], cwd=str(clone_dir)
                )

                # OMN-16403 falsifiable mint-verify: re-parse each ticket's
                # just-pushed contract and confirm the self-bind dod_evidence
                # entry actually landed before this method goes on to PATCH
                # Evidence-Source onto the product PR and declare success.
                #
                # ``_append_self_bind_evidence``'s own idempotency guard (an
                # id-presence check, by design so a `synchronize` re-fire does
                # not double-append) returns silently whenever it believes the
                # item is already declared. That silent-return path is
                # indistinguishable, from the caller's side, between a
                # genuine re-fire and a bug/race that made the guard true
                # without the item ever having been written — and nothing
                # downstream previously re-checked the WRITTEN file before
                # patching the product PR. That is the exact half-companion
                # state the OMN-16403 incident produced on
                # onex_change_control#6636: pass 1 landed, Evidence-Source
                # was correctly patched onto the product PR, but the
                # ``occ-self-bind-pr-6636`` dod_evidence entry never made it
                # into ``contracts/OMN-16145.yaml`` — silently, with no error
                # and no retry, stranding the companion at
                # ``validator_occ_merge_eligibility`` for 4 days. Fail loudly
                # here instead: never patch the product PR (and never report
                # this run as having authored a companion) while any ticket's
                # contract is missing its own self-bind entry.
                self._assert_self_bind_landed(
                    contract_paths=contract_paths,
                    tickets=tickets,
                    self_bind_evidence_id=self_bind_evidence_id,
                    occ_pr_number=occ_pr_number,
                )

            # 5. PATCH Evidence-Source: OCC#<n> back onto the product PR via REST.
            self._patch_evidence_source(
                repo=repo,
                pr_number=pr_number,
                occ_pr_number=occ_pr_number,
                tickets=tickets,
                existing_body=body,
            )

            action = (
                f"authored OCC companion Evidence-Source: OCC#{occ_pr_number} for "
                f"{', '.join(tickets)} on {repo}#{pr_number} "
                f"(product head {head_sha}, branch {branch})"
            )
            logger.info("occ_companion_emitter: %s", action)
            return action
        finally:
            # Release on BOTH success and any exception so a crashed/failed
            # mint frees the head immediately (the TTL steal is only the
            # backstop for a hard kill that never reaches this finally).
            # Best-effort — never masks the mint's real return/exception.
            release_occ_companion_lease(
                token=token,
                repo_slug=repo_slug,
                pr_number=pr_number,
                head_sha=head_sha,
                occ_repo=self._occ_repo,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tickets(title: str) -> list[str]:
        """Return the PR-TITLE-cited ticket set (OMN-16376).

        A companion must key to the ticket the PR **title** cites, not one
        merely mentioned in the body. The Receipt Gate's own identity check
        (``_verify_ticket_identity`` Axis 1, ``validator_receipt_gate.py``)
        requires the PR title — not the body — to reference whatever ticket
        the Evidence-Ticket line names; the ``pr-title / check-title`` CI job
        enforces the same title-only extraction. A body commonly cites OTHER
        tickets as related context (e.g. a "related: OMN-<n>" line, or a
        closing keyword pointed at a different, unrelated ticket) — keying
        the companion to a body-cited ticket the title never mentions is
        therefore structurally unable to satisfy the gate it exists to serve
        (live incident: omninode_infra PR #958, title cited OMN-16368, body
        cited OMN-15757 as related context; the autobind companion was
        wrongly keyed to OMN-15757 and could never pass Axis 1 for
        OMN-16368's Receipt Gate run).

        Delegates to ``_extract_ticket_ids`` with an EMPTY body so the
        SAME title-token regex (``TICKET_PATTERN``) the gate itself falls
        back to is reused byte-for-byte, never re-derived here.
        """
        return _extract_ticket_ids("", title)

    @staticmethod
    def _occ_branch_name(*, repo: str, pr_number: int) -> str:
        """The deterministic OCC companion branch name for one product PR.

        Single definition shared by the fresh-mint path (``branch`` at the top
        of :meth:`_emit_companion_sync`) and the OMN-16386 binding-identity
        check below — the two must never diverge on this format.
        """
        repo_slug = repo.replace("/", "-")
        return f"auto/{repo_slug.lower()}-pr-{pr_number}-occ-autobind"

    def _occ_binding_matches_this_pr(
        self, *, occ_pr_number: int, repo: str, pr_number: int, token: str
    ) -> bool:
        """True when OCC#``occ_pr_number`` was actually minted for THIS product PR.

        OMN-16386: a resolvable ``Evidence-Source: OCC#<n>`` line is not proof
        of a genuine binding — it may be inherited verbatim from a template PR
        body (the release-cascade dependency-bump class), naming a companion
        that was minted for a *different* product PR. That companion's
        ``occ-self-bind-pr-<n>`` receipt binds only the template PR's head SHA
        and its branch encodes the template PR's own (repo, pr_number), never
        this one's — so a presence-only check silently strands the cascade PR
        at the Receipt Gate (live: onex_change_control#6850, #6823, #6636).

        The OCC companion branch is deterministic
        (:meth:`_occ_branch_name`) and embeds the exact (repo, pr_number) the
        companion was minted for. Comparing OCC#``occ_pr_number``'s actual
        head branch against the branch THIS run would use for its own
        (repo, pr_number) is a genuine identity check, not a presence check.

        An unresolvable OCC PR (404, deleted, network error) fails OPEN toward
        minting a fresh companion — a redundant mint is self-healing (the
        idempotency guard on the fresh companion's own branch handles it),
        while silently trusting an unverifiable citation is exactly the
        defect this check exists to close.
        """
        expected_branch = self._occ_branch_name(repo=repo, pr_number=pr_number)
        occ_owner, occ_repo_name = split_repo(self._occ_repo)
        try:
            occ_pr_data = rest_json(
                "GET",
                f"/repos/{occ_owner}/{occ_repo_name}/pulls/{occ_pr_number}",
                token=token,
            )
        except GitHubApiError as exc:
            logger.warning(
                "occ_companion_emitter: could not resolve OCC#%s to verify "
                "the Evidence-Source binding for %s#%s (%s); treating as "
                "unbound and proceeding to mint (OMN-16386 fail-open)",
                occ_pr_number,
                repo,
                pr_number,
                exc,
            )
            return False
        head = occ_pr_data.get("head") if isinstance(occ_pr_data, dict) else None
        actual_branch = head.get("ref") if isinstance(head, dict) else None
        return actual_branch == expected_branch

    @staticmethod
    def _is_private_repo(pr_data: dict[str, object]) -> bool:
        """True when the product PR's repo is private (OMN-14766 F-16).

        Read from the PR REST payload's ``base.repo.private`` (the base is always
        the product repo, even for a fork PR whose ``head.repo`` is the fork). No
        extra API call — the emitter already GETs the PR. A private repo cannot be
        re-probed by the hosted OCC contract-compliance runner (its token has no
        scope there), so its declared check_values are rendered receipt-local. Fails
        SAFE toward *public* only when the field is genuinely absent; the effect is
        a hosted `gh pr view` check that fails loudly in OCC CI rather than a silent
        skip, so an unexpected shape is surfaced, not masked.
        """
        base = pr_data.get("base")
        if not isinstance(base, dict):
            return False
        repo_obj = base.get("repo")
        if not isinstance(repo_obj, dict):
            return False
        return bool(repo_obj.get("private"))

    @staticmethod
    def _suppression_reason(pr_data: dict[str, object]) -> str | None:
        """Return a reason code if this product PR must NOT get a companion (F-17).

        Suppression fires when the PR is closed, a draft, or carries a
        do-not-merge marker in its TITLE or a matching label name. Returns one of
        ``PR_CLOSED`` / ``PR_DRAFT`` / ``PR_DO_NOT_MERGE``, or ``None`` when the PR
        is a normal mergeable product PR. Pure — reads only the REST snapshot.

        ``state`` is checked before ``draft`` because a closed draft should report
        the more actionable ``PR_CLOSED``; both are terminal for emission.
        """
        state = pr_data.get("state")
        if isinstance(state, str) and state.lower() == "closed":
            return "PR_CLOSED"
        if bool(pr_data.get("draft")):
            return "PR_DRAFT"
        title = pr_data.get("title")
        if isinstance(title, str) and HOLD_MARKER_RE.search(title):
            return "PR_DO_NOT_MERGE"
        labels = pr_data.get("labels")
        if isinstance(labels, list):
            for label in labels:
                name = label.get("name") if isinstance(label, dict) else None
                if isinstance(name, str) and HOLD_MARKER_RE.search(name):
                    return "PR_DO_NOT_MERGE"
        return None

    def _observe_pr_probe(
        self, *, probe_command: str, token: str, fallback: dict[str, object]
    ) -> tuple[str, int]:
        """Execute the declared probe and return ``(probe_stdout, exit_code)``.

        Runs the real ``gh pr view --json`` probe so the receipt carries
        machine-observed output rather than a fabricated template (OMN-13990
        item 4 / OMN-14055). ``gh`` authenticates from ``GH_TOKEN``. When ``gh``
        is unavailable or errors, the probe falls back to the fields already
        observed from the GitHub REST API — still genuine GitHub facts, never a
        hardcoded template — and reports exit_code 0 because the PR was in fact
        observed. Output is re-serialised as a single compact JSON line so the
        receipt's YAML block scalar stays well-formed.
        """
        fallback_json = json.dumps(fallback, separators=(",", ":"), sort_keys=True)
        try:
            env = os.environ.copy()
            env["GH_TOKEN"] = token
            result = subprocess.run(
                shlex.split(probe_command),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return fallback_json, 0
        if result.returncode != 0 or not result.stdout.strip():
            return fallback_json, 0
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip().replace("\n", " "), 0
        return json.dumps(parsed, separators=(",", ":"), sort_keys=True), 0

    # ------------------------------------------------------------------
    # OMN-15247 — contention detection (deliverable A)
    # ------------------------------------------------------------------

    def _find_contending_companions(
        self, *, tickets: Sequence[str], own_branch: str, token: str
    ) -> tuple[ContentionFinding, ...]:
        """Index open OCC companions that already carry evidence for ``tickets``.

        Runs unconditionally on every mint attempt — there is no mode that
        skips it. The three I/O halves are bound here and the decision logic
        stays pure in :func:`omnimarket.occ_contention.find_open_companions`.
        """
        occ_owner, occ_repo_name = split_repo(self._occ_repo)

        def _search(path: str) -> dict[str, object]:
            return rest_json("GET", path, token=token)

        def _get_pull(number: int) -> dict[str, object]:
            return rest_json(
                "GET", f"/repos/{occ_owner}/{occ_repo_name}/pulls/{number}", token=token
            )

        def _files(number: int) -> list[dict[str, object]]:
            return self._paginated_pr_files(occ_owner, occ_repo_name, number, token)

        return find_open_companions(
            tickets=tickets,
            occ_repo=self._occ_repo,
            own_branch=own_branch,
            search_issues=_search,
            get_pull=_get_pull,
            list_pr_files=_files,
        )

    def _comment_deferred(
        self,
        *,
        repo: str,
        pr_number: int,
        findings: Sequence[ContentionFinding],
        token: str,
    ) -> None:
        """Idempotently note the defer on each CONTENDING OCC PR (best-effort).

        Guarded by a marker comment so a ``synchronize`` storm cannot spam: the
        existing comments are fetched and the marker checked BEFORE posting. The
        comment lands on the contending OCC PR, not the product PR, so the human
        who authored the stronger companion sees that the machine stood down.
        Best-effort and swallowed, exactly like ``_apply_machine_minted_label``:
        the load-bearing half of the defer is the mutation suppression, and a
        comment API hiccup must never turn a clean defer into a failure.
        """
        occ_owner, occ_repo_name = split_repo(self._occ_repo)
        for finding in findings:
            if finding.occ_pr_number <= 0:
                continue
            marker = f"<!-- occ-autobind-deferred:{repo}#{pr_number} -->"
            try:
                existing = rest_json_array(
                    "GET",
                    f"/repos/{occ_owner}/{occ_repo_name}/issues/"
                    f"{finding.occ_pr_number}/comments?per_page=100",
                    token=token,
                )
                if any(marker in str(c.get("body") or "") for c in existing):
                    continue
                rest_json(
                    "POST",
                    f"/repos/{occ_owner}/{occ_repo_name}/issues/"
                    f"{finding.occ_pr_number}/comments",
                    token=token,
                    body={
                        "body": (
                            f"{marker}\nOCC autobind stood down for "
                            f"`{repo}#{pr_number}` ({finding.ticket_id}): this PR "
                            f"({finding.provenance.value}) already carries evidence "
                            "for that ticket, so no competing companion was minted "
                            "(OMN-15247 defer-on-contention)."
                        )
                    },
                )
            except (
                GitHubApiError,
                OSError,
            ) as exc:  # fallback-ok: comment is courtesy, the suppression is the gate
                logger.warning(
                    "occ_companion_emitter: could not post defer note on OCC#%s: %s",
                    finding.occ_pr_number,
                    exc,
                )

    def _comment_no_red_derivable(
        self, *, repo: str, pr_number: int, token: str
    ) -> None:
        """Idempotently tell the PRODUCT PR that hand-authored evidence is needed."""
        owner, repo_name = split_repo(repo)
        marker = f"<!-- occ-autobind-no-red-derivable:{pr_number} -->"
        try:
            existing = rest_json_array(
                "GET",
                f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments?per_page=100",
                token=token,
            )
            if any(marker in str(c.get("body") or "") for c in existing):
                return
            rest_json(
                "POST",
                f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments",
                token=token,
                body={
                    "body": (
                        f"{marker}\nOCC autobind did not mint a companion for this "
                        "PR: no changed-file candidate could be proven RED against "
                        "the merge base, and emitting a PR-existence probe instead "
                        "would be non-falsifiable evidence (OMN-15247). "
                        "Hand-authored evidence is required."
                    )
                },
            )
        except (GitHubApiError, OSError) as exc:  # fallback-ok: courtesy comment
            logger.warning(
                "occ_companion_emitter: could not post no-red-derivable note on "
                "%s#%s: %s",
                repo,
                pr_number,
                exc,
            )

    def _post_mint_status_check_run(
        self,
        *,
        repo: str,
        pr_number: int,
        head_sha: str,
        token: str,
        reason: str,
        summary: str,
    ) -> None:
        """Report a mint decision on the check surface, not just a PR comment.

        OMN-16339: from the checks rollup / workflow-run status / OCC PR list,
        a policy decline is indistinguishable from a stalled or broken
        pipeline — the ambiguity that produced two independent false-stall
        diagnoses against a correctly-functioning OMN-15247 gate in one
        night. ``conclusion`` is always ``"neutral"``, never ``"failure"``:
        this check-run can never newly block a merge, and no OMN-15247 gate
        behavior changes — this is purely additive observability. Re-posts
        (not deduplicated) on every decline, including a manual replay, so a
        second silent decline is also visible rather than only the first.
        """
        owner, repo_name = split_repo(repo)
        try:
            rest_json(
                "POST",
                f"/repos/{owner}/{repo_name}/check-runs",
                token=token,
                body={
                    "name": _MINT_STATUS_CHECK_NAME,
                    "head_sha": head_sha,
                    "status": "completed",
                    "conclusion": "neutral",
                    "output": {
                        "title": f"declined: {reason}",
                        "summary": summary,
                    },
                },
            )
        except (GitHubApiError, OSError) as exc:  # fallback-ok: courtesy check-run
            logger.warning(
                "occ_companion_emitter: could not post mint-status check-run on "
                "%s#%s: %s",
                repo,
                pr_number,
                exc,
            )

    # ------------------------------------------------------------------
    # OMN-15247 — content-bound check derivation (deliverable B)
    # ------------------------------------------------------------------

    @staticmethod
    def _paginated_pr_files(
        owner: str, repo_name: str, pr_number: int, token: str
    ) -> list[dict[str, object]]:
        """Return every ``/pulls/{n}/files`` entry (mirrors HandlerOccStateEffect)."""
        files: list[dict[str, object]] = []
        page = 1
        while True:
            batch = rest_json_array(
                "GET",
                f"/repos/{owner}/{repo_name}/pulls/{pr_number}/files"
                f"?per_page=100&page={page}",
                token=token,
            )
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def _content_at_ref(
        self, owner: str, repo_name: str, path: str, ref: str, token: str
    ) -> str | None:
        """Fetch decoded file content at ``ref``, or None if absent/undecodable."""
        encoded_path = urllib.parse.quote(path, safe="/")
        try:
            data = rest_json(
                "GET",
                f"/repos/{owner}/{repo_name}/contents/{encoded_path}?ref={ref}",
                token=token,
            )
        except GitHubApiError:
            return None
        if data.get("encoding") != "base64":
            return None
        try:
            raw = base64.b64decode(str(data.get("content", "")), validate=False)
        except (binascii.Error, ValueError):
            return None
        return raw.decode("utf-8", errors="replace")

    def _resolve_red_ref_live(
        self, owner: str, repo_name: str, pr_data: dict[str, object], token: str
    ) -> str | None:
        """Resolve the MERGE-BASE ref a generated check must go RED against.

        Live-I/O wrapper over the pure :func:`resolve_red_ref` (OMN-15247 B2).
        Every API failure degrades to ``None``, which means *no content-bound
        check is emitted* — fail-closed, never a fallback to ``pr.base.sha``.
        """

        def _commit(sha: str) -> dict[str, object]:
            return rest_json(
                "GET", f"/repos/{owner}/{repo_name}/commits/{sha}", token=token
            )

        def _compare(base_ref: str, head_ref_sha: str) -> dict[str, object]:
            return rest_json(
                "GET",
                f"/repos/{owner}/{repo_name}/compare/{base_ref}...{head_ref_sha}",
                token=token,
            )

        try:
            return resolve_red_ref(pr_data=pr_data, compare=_compare, commit=_commit)
        except (GitHubApiError, OSError) as exc:  # fallback-ok: fail CLOSED to None
            logger.warning(
                "occ_companion_emitter: could not resolve merge-base RED ref for "
                "%s/%s#%s (%s); no content-bound check will be derived",
                owner,
                repo_name,
                pr_data.get("number"),
                exc,
            )
            return None

    def _derive_content_bound_check(
        self,
        *,
        repo: str,
        owner: str,
        repo_name: str,
        pr_number: int,
        pr_data: dict[str, object],
        evidence_ref: str,
        token: str,
    ) -> tuple[str | None, str | None, int | None]:
        """Derive a RED-PROVEN content-bound check, or ``(None, red_ref, None)``.

        Returns ``(check_value, red_ref, red_exit_code)``. The derivation ALWAYS
        runs (both binding modes) and logs its outcome — under ``pr_existence``
        the result is observed and discarded, changing zero committed bytes; that
        is what makes the OFF state observable rather than absent
        (``feedback_optional_input_means_the_check_does_not_exist``).

        The mint-time acceptance bar (OMN-15247 §5 layer 1) is enforced here and
        is fail-closed: the selected probe MUST exit 0 at ``evidence_ref`` and
        NON-zero at the merge base before it is allowed to be written. Any missing
        leg yields ``None``.
        """
        red_ref = self._resolve_red_ref_live(owner, repo_name, pr_data, token)
        if red_ref is None:
            logger.info(
                "occ_companion_emitter content-bound: %s#%s no_red_derivable "
                "(merge base unresolvable) binding=%s",
                repo,
                pr_number,
                self._check_binding.value,
            )
            return None, None, None

        try:
            files = self._paginated_pr_files(owner, repo_name, pr_number, token)
        except (GitHubApiError, OSError) as exc:  # fallback-ok: fail CLOSED
            logger.warning(
                "occ_companion_emitter content-bound: could not list files for "
                "%s#%s (%s); no content-bound check derived",
                repo,
                pr_number,
                exc,
            )
            return None, red_ref, None

        candidates = extract_symbol_candidates(files)

        def _fetch(path: str, ref: str) -> str | None:
            return self._content_at_ref(owner, repo_name, path, ref, token)

        # OMN-16410 — lockfile-line candidates. A pure ``uv.lock`` bump (the
        # OMN-13902 sibling-lock-refresh bot's whole output shape) has zero
        # Python declarations, so ``extract_symbol_candidates`` alone always
        # returns empty for it and this producer used to decline every such
        # PR outright ("no changed-file candidate could be proven RED"),
        # forcing hand-authored evidence for a mechanically-provable fact.
        # Content, not ``patch``, is the input here on purpose — GitHub omits
        # ``patch`` once a file's diff crosses an undocumented per-file size
        # threshold, and a full ``uv.lock`` relock is exactly that shape
        # (MEASURED live against omnibase_infra#2848: 4396 changed lines,
        # `patch` absent from the files listing) — see
        # ``extract_lock_line_candidates``'s docstring. Appended, never
        # replacing the Python candidates, so a PR touching both keeps trying
        # the Python grammar first.
        for f in files:
            path = str(f.get("filename", ""))
            status = f.get("status")
            if status not in ("added", "modified") or not path.endswith(
                LOCK_FILE_SUFFIXES
            ):
                continue
            head_content = _fetch(path, evidence_ref)
            base_content = _fetch(path, red_ref)
            candidates = candidates + extract_lock_line_candidates(
                path=path, head_content=head_content, base_content=base_content
            )

        # OMN-15247 foldproof follow-up: no ``accept=`` filter here anymore.
        # Pre-fix, this candidate was rejected outright whenever its rendered
        # length would fold the CONTRACT's ``check_value:`` line (indent 8) —
        # yamlfmt folds a double-quoted scalar at the first space past column
        # 100, which would restale contract_sha256 (F-03 / OMN-14684) — and
        # every realistic content-bound check crosses that budget, making
        # ``content_bound`` a fail-closed no-op on every real PR. Fold-safety
        # now lives in RENDERING (``occ_evidence_stamp.render_check_value_field``,
        # used for both the contract's check_value and the receipt's
        # check_value/probe_command/actual_output), which picks a fold-proof
        # literal block scalar whenever the quoted form would fold — so any
        # RED-derivable candidate is safe to emit regardless of length.
        check = select_asserted_check(
            candidates,
            repo=repo,
            head_sha=evidence_ref,
            base_sha=red_ref,
            fetch_content=_fetch,
        )
        if check is None:
            logger.info(
                "occ_companion_emitter content-bound: %s#%s no_red_derivable "
                "(0 of %d candidates RED-controlled at %s) binding=%s",
                repo,
                pr_number,
                len(candidates),
                red_ref[:8],
                self._check_binding.value,
            )
            return None, red_ref, None

        # Mint-time RED/GREEN execution — the acceptance bar, enforced before the
        # check is allowed anywhere near a committed byte.
        _green_out, green_exit = self._execute_probe_raw(check, token=token)
        if green_exit != 0:
            logger.warning(
                "occ_companion_emitter content-bound: %s#%s candidate did NOT go "
                "GREEN at %s (exit %s); rejected",
                repo,
                pr_number,
                evidence_ref[:8],
                green_exit,
            )
            return None, red_ref, None

        red_check = check.replace(f"?ref={evidence_ref}", f"?ref={red_ref}")
        _red_out, red_exit = self._execute_probe_raw(red_check, token=token)
        if red_exit == 0:
            logger.warning(
                "occ_companion_emitter content-bound: %s#%s candidate ALSO passes "
                "at merge base %s (exit 0) — non-falsifiable, rejected",
                repo,
                pr_number,
                red_ref[:8],
            )
            return None, red_ref, None

        logger.info(
            "occ_companion_emitter content-bound: %s#%s would_bind=%r "
            "green_at=%s red_at=%s red_exit=%s binding=%s",
            repo,
            pr_number,
            check,
            evidence_ref[:8],
            red_ref[:8],
            red_exit,
            self._check_binding.value,
        )
        return check, red_ref, red_exit

    @staticmethod
    def _execute_probe_raw(probe_command: str, *, token: str) -> tuple[str, int]:
        """Run a probe and return its TRUE exit code (OMN-15247 §5 / §9 trap).

        ``_observe_pr_probe`` deliberately NORMALIZES every failure to exit 0 —
        correct for provenance capture (the PR *was* observed), but fatal for RED
        derivation: reusing it would report exit 0 at the merge base for a probe
        that genuinely failed, manufacturing a false RED-proof and defeating the
        entire acceptance bar. This executor never normalizes.

        The content-bound probe is a shell PIPELINE (``gh api … | base64 -d |
        grep -c …``), so it runs under ``bash -o pipefail -c`` rather than
        ``shlex.split``: without ``pipefail`` the exit status would be ``grep``'s
        alone, and a failed ``gh api`` (deleted ref, revoked scope) would still
        report ``grep``'s verdict on empty input. A non-zero exit from ANY stage
        is the honest answer. Any launch failure returns a non-zero sentinel, so
        an unrunnable probe is never mistaken for a passing one.
        """
        env = os.environ.copy()
        env["GH_TOKEN"] = token
        try:
            result = subprocess.run(
                ["bash", "-o", "pipefail", "-c", probe_command],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return f"probe launch failed: {exc}", 127
        return result.stdout.strip(), result.returncode

    def _clone_and_branch(
        self, clone_dir: Path, branch: str, tmpdir: str, token: str
    ) -> str:
        """Clone OCC (shallow) + cut the companion branch. Returns the base SHA.

        The base SHA is the clone's default-branch HEAD, captured BEFORE cutting
        the branch, so the OMN-14741 F-01 append-only guard can diff the generated
        tree against exactly the state the companion was branched from.
        """
        # HTTPS x-access-token clone/push (OMN-13990): the effects container has
        # no SSH identity. The token is redacted from any surfaced git error by
        # occ_git_transport.run_git.
        self._run_git(
            [
                "git",
                "clone",
                "--depth=1",
                authenticated_occ_url(token, self._occ_repo),
                str(clone_dir),
            ],
            cwd=tmpdir,
        )
        self._run_git(
            ["git", "config", "user.name", self._git_author_name],
            cwd=str(clone_dir),
        )
        self._run_git(
            ["git", "config", "user.email", self._git_author_email],
            cwd=str(clone_dir),
        )
        base_sha = self._head_sha(str(clone_dir))
        self._run_git(["git", "checkout", "-b", branch], cwd=str(clone_dir))
        return base_sha

    def _rebind_receipts(
        self,
        clone_dir: Path,
        ticket: str,
        contract_path: Path,
        evidence_ids: Iterable[str],
    ) -> None:
        """Rebind THIS PR's receipts' contract hash binding(s) (OMN-14741 F-01).

        Only the receipt directories named in ``evidence_ids`` are touched —
        never an rglob over ``<ticket>/`` that would rewrite a PRIOR merged
        receipt's whole-file ``contract_sha256`` when the contract grows. The
        eligibility/receipt gates grandfather a prior merged receipt's stale
        whole-file hash, so rewriting it here is a non-append-only mutation of an
        already-merged receipt (the OCC#4293/4295/4296 friction). Scoping to this
        PR's own rows is both correct (those are the only receipts that need the
        current whole-file hash) and append-only-safe.

        Legacy whole-file: sets ``contract_sha256`` to sha256(contract) on each
        of this PR's receipts, mirroring the overnight-sweep manual recipe
        (friction #9). Per-entry (OMN-13888 / OMN-14418 residual 3): for a receipt
        that declares ``contract_entry_sha256``, also rebind it to
        ``compute_contract_entry_sha256(contract_data, receipt's own
        evidence_item_id)`` — the SAME canonical per-entry hasher the
        consumer-side gates recompute against, never a local re-implementation. A
        receipt whose id is not a declared dod_evidence item is left with the
        field absent (``ContractEntryNotFoundError`` swallowed for that receipt).

        Rendering + hashing stay in the pure :mod:`occ_evidence_stamp` seam (plus
        the canonical omnibase_core per-entry hasher); this method only does I/O.
        """
        contract_bytes = contract_path.read_bytes()
        whole_file_digest = compute_contract_sha256(contract_bytes)
        contract_data = yaml.safe_load(contract_bytes)
        receipt_root = clone_dir / "drift" / "dod_receipts" / ticket
        for evidence_id in evidence_ids:
            for receipt in sorted((receipt_root / evidence_id).rglob("*.yaml")):
                text = receipt.read_text(encoding="utf-8")
                new_text = rebind_contract_sha256_in_text(text, whole_file_digest)

                evidence_item_id = extract_evidence_item_id(new_text)
                if evidence_item_id is not None:
                    try:
                        entry_digest = compute_contract_entry_sha256(
                            contract_data, evidence_item_id
                        )
                    except ContractEntryNotFoundError:
                        # Not every receipt binds to a declared dod_evidence item
                        # (self-bind receipts, by design) — nothing to rebind.
                        pass
                    else:
                        new_text = rebind_contract_entry_sha256_in_text(
                            new_text, entry_digest
                        )

                if new_text != text:
                    receipt.write_text(new_text, encoding="utf-8")

    def _append_self_bind_evidence(
        self,
        contract_path: Path,
        *,
        evidence_id: str,
        occ_pr_number: int,
        ticket_id: str,
    ) -> None:
        """Append the self-bind item to the contract's dod_evidence (OMN-14650).

        OMN-14741 F-04: a STRUCTURAL insert at the END of the ``dod_evidence``
        block, robust to a contract whose ``dod_evidence`` is NOT the terminal
        top-level key (the naive EOF string-append assumed terminal and produced
        invalid YAML — the list item landed after a sibling top-level key). The
        rendered item block is yamlfmt-clean, so inserting it into a yamlfmt-clean
        contract keeps the file yamlfmt-clean.

        Idempotent: a ``synchronize`` re-fire regenerates the branch from a fresh
        clone; the id-presence guard (parsed, not a substring match) protects
        against a double-append within a single run. Must run BEFORE the
        whole-file/per-entry rebind so the self-bind receipt binds to the final
        contract bytes.
        """
        text = contract_path.read_text(encoding="utf-8")
        if self._declares_dod_evidence_id(text, evidence_id):
            return  # already declared — do not append twice
        block = render_self_bind_dod_evidence_item(
            evidence_id=evidence_id,
            occ_pr_number=occ_pr_number,
            occ_repo=self._occ_repo,
            ticket_id=ticket_id,
        )
        contract_path.write_text(
            self._insert_dod_evidence_items(text, [block]), encoding="utf-8"
        )

    def _ensure_base_dod_evidence(
        self,
        contract_path: Path,
        *,
        repo: str,
        pr_number: int,
        evidence_id: str,
        ci_evidence_id: str,
        downstream_check_value: str | None = None,
        ci_check_value: str | None = None,
    ) -> None:
        """Ensure a PRE-EXISTING contract declares THIS PR's base rows (F-04).

        When ``contracts/<ticket>.yaml`` already exists (a prior ticket/PR authored
        it), it does not declare this PR's downstream/CI dod_evidence items, so the
        freshly-written receipts bind to nothing and eligibility breaks with
        ``contract_entry_sha256=PENDING`` (the OCC#4304 class). Append whichever of
        the two base rows is missing, using the SAME item-block renderers
        :func:`render_companion_contract` composes — so the appended row's parsed
        dod_evidence item (hence its per-entry hash) is byte-identical to the
        fresh-contract row. Structural insert (robust to non-terminal contracts).

        For a private product repo the appended rows carry the hosted-safe
        ``downstream_check_value`` / ``ci_check_value`` (OMN-14766 F-16), so a
        repaired pre-existing contract matches the fresh-contract shape there too.
        """
        text = contract_path.read_text(encoding="utf-8")
        blocks: list[str] = []
        if not self._declares_dod_evidence_id(text, evidence_id):
            blocks.append(
                render_downstream_dod_evidence_item(
                    evidence_id=evidence_id,
                    repo=repo,
                    pr_number=pr_number,
                    check_value=downstream_check_value,
                )
            )
        if not self._declares_dod_evidence_id(text, ci_evidence_id):
            blocks.append(
                render_ci_dod_evidence_item(
                    evidence_id=evidence_id,
                    repo=repo,
                    pr_number=pr_number,
                    check_value=ci_check_value,
                )
            )
        if blocks:
            contract_path.write_text(
                self._insert_dod_evidence_items(text, blocks), encoding="utf-8"
            )

    @staticmethod
    def _declares_dod_evidence_id(contract_text: str, evidence_id: str) -> bool:
        """True when ``evidence_id`` is a declared dod_evidence item (parsed)."""
        data = yaml.safe_load(contract_text)
        if not isinstance(data, dict):
            return False
        for item in data.get("dod_evidence") or []:
            if isinstance(item, dict) and item.get("id") == evidence_id:
                return True
        return False

    def _assert_self_bind_landed(
        self,
        *,
        contract_paths: dict[str, Path],
        tickets: Sequence[str],
        self_bind_evidence_id: str,
        occ_pr_number: int,
    ) -> None:
        """Fail loudly (OMN-16403) unless every ticket's self-bind item landed.

        Re-reads each ticket's just-written contract file off disk — the
        SAME bytes that were just committed and force-pushed to the OCC
        companion branch, not a re-derived assumption — and parses it with
        the canonical :meth:`_declares_dod_evidence_id` check. Raising here,
        before :meth:`_patch_evidence_source` runs, guarantees a caller can
        never observe a product PR whose ``Evidence-Source`` was patched to
        an OCC companion that is missing its own self-bind receipt entry —
        the "half-companion" state that stranded ``onex_change_control#6636``
        for 4 days with no signal.
        """
        missing = [
            ticket
            for ticket in tickets
            if not self._declares_dod_evidence_id(
                contract_paths[ticket].read_text(encoding="utf-8"),
                self_bind_evidence_id,
            )
        ]
        if missing:
            raise RuntimeError(
                f"OCC self-bind mint-verify failed (OMN-16403): "
                f"{self_bind_evidence_id!r} is not a declared dod_evidence "
                f"item for ticket(s) {', '.join(missing)} after the "
                f"self-bind pass for OCC#{occ_pr_number} — refusing to "
                "patch Evidence-Source onto a half-companioned product PR."
            )

    @staticmethod
    def _insert_dod_evidence_items(contract_text: str, blocks: Sequence[str]) -> str:
        """Insert item ``blocks`` at the END of the ``dod_evidence`` list (F-04).

        Text-level, byte-shape-preserving: the existing (yamlfmt-clean) contract
        bytes are untouched except for the inserted, already-yamlfmt-clean,
        2-space-indented item blocks. The insertion point is the boundary of the
        ``dod_evidence`` block — the first subsequent column-0 (non-indented,
        non-blank) line, else EOF — so a contract whose ``dod_evidence`` is NOT the
        terminal top-level key still gets the item appended to the RIGHT list
        rather than dumped after a sibling key.
        """
        if not blocks:
            return contract_text
        lines = contract_text.splitlines(keepends=True)
        key_idx: int | None = None
        for i, line in enumerate(lines):
            if re.match(r"^dod_evidence:[ \t]*$", line):
                key_idx = i
                break
        if key_idx is None:
            raise RuntimeError(
                "cannot append dod_evidence item: contract has no block-style "
                "'dod_evidence:' key (OMN-14741 F-04)"
            )
        end = len(lines)
        for j in range(key_idx + 1, len(lines)):
            stripped = lines[j].rstrip("\n")
            if stripped and not stripped[0].isspace():
                end = j
                break
        # Guarantee the line preceding the insertion ends with a newline.
        if end > 0 and not lines[end - 1].endswith("\n"):
            lines[end - 1] = lines[end - 1] + "\n"
        return "".join(lines[:end]) + "".join(blocks) + "".join(lines[end:])

    @staticmethod
    def _allowed_paths(
        tickets: Iterable[str],
        evidence_ids: Iterable[str],
        *,
        filename: str = "command.yaml",
    ) -> set[str]:
        """Repo-relative paths this run is permitted to add/modify (F-01).

        OMN-16892: ``filename`` exists because a receipt's basename is the
        contract item's ``check_type``, not a constant. The diff-derived
        behavior item declares ``test_passes``, so its receipt lands at
        ``test_passes.yaml``; with this hardcoded to ``command.yaml`` the
        fail-closed guard rejected the producer's own write and aborted the
        entire mint. Kept a keyword with the old default so every existing
        caller — all of which mint ``command`` items — is unchanged.
        """
        eids = list(evidence_ids)
        allowed: set[str] = set()
        for ticket in tickets:
            allowed.add(f"contracts/{ticket}.yaml")
            for eid in eids:
                allowed.add(f"drift/dod_receipts/{ticket}/{eid}/{filename}")
        return allowed

    def _assert_append_only(
        self, clone_dir: Path, base_sha: str, allowed_paths: set[str]
    ) -> None:
        """Fail CLOSED if the generated tree touched anything unexpected (F-01).

        Diffs the committed branch against the clone base and rejects (a) any
        deletion and (b) any add/modify of a path outside this run's contract +
        receipt set. This is a real check against ``git diff``, replacing the
        assertion-only "all-adds" comment the force-push previously trusted — the
        exact gap that let generated companions mutate already-merged receipts
        (OCC#4293/4295/4296).

        OMN-16071: membership in ``allowed_paths`` alone is NOT sufficient —
        it only proves this run's writer *intended* to touch a path, not that
        the path is genuinely new at ``base_sha``. A shared, ticket-scoped
        evidence id whose directory a PRIOR companion already merged is, by
        construction, always present in THIS run's own allowed set too (the
        writer renders the same deterministic path every time), so a status-
        ``M`` change there previously sailed through this guard silently —
        the mutate-in-place defect named in the ticket title. The git status
        letter is now checked directly for RECEIPTS: any status other than
        ``A`` (added) is an unconditional violation, matching the hosted OCC
        Append-Only Gate's own receipt semantics (``omnibase_core.validation.
        validator_occ_append_only.evaluate_append_only``'s ``receipt_diff``
        leg, which flags any M/D/R/C status there and requires corrections to
        be net-new ``.supersede.<NNNN>.yaml`` files).

        OMN-16356: the CONTRACT file (``contracts/<ticket>.yaml``) is a
        DIFFERENT case. It is intentionally, structurally append-only at the
        content level across companions for the same ticket — every writer
        (``_ensure_base_dod_evidence`` F-04, ``_append_self_bind_evidence``)
        only ever splices new ``dod_evidence`` blocks in, never rewrites
        existing bytes — so its own git status is legitimately ``M`` on a
        second-or-later companion. The hosted gate's OWN semantics for the
        contract are per-ENTRY (``evaluate_append_only``'s ``base_contract``/
        ``head_contract`` legs: an existing id must survive, unchanged; a NEW
        id is always allowed), not per-file. A blanket per-file ``A``-only
        rule — introduced by OMN-16071's PR #2086 to close the receipt-mutate
        gap — was stricter than the gate itself for this one path and
        rejected every legitimate second companion for an already-companioned
        ticket (live 2026-08-23: omnimarket#2124/OMN-15800,
        omnibase_infra#2790/OMN-15468, onex_change_control#6926/OMN-16413; a
        parallel report against the sibling ``node_occ_companion_effect``
        writer is OMN-16356's own original filing). A contract-path ``M`` is
        now independently re-verified against the SAME canonical judgment the
        hosted gate makes (:func:`evaluate_append_only`) before being allowed;
        it still fails closed if that judgment finds a removed or altered
        entry.
        """
        diff = self._run_git(
            ["git", "diff", "--name-status", base_sha, "HEAD"], cwd=str(clone_dir)
        )
        violations: list[str] = []
        for raw in diff.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            path = parts[-1]  # rename → dest path is last field
            if status.startswith("D"):
                violations.append(f"deletes {path}")
            elif not status.startswith("A"):
                if (
                    status.startswith("M")
                    and path in allowed_paths
                    and _CONTRACT_YAML_PATH_RE.match(path)
                    and self._is_sanctioned_contract_growth(clone_dir, base_sha, path)
                ):
                    continue
                violations.append(
                    f"{status} {path} (not a net-new add — a receipt or "
                    "contract may never be opened for write once it exists "
                    "at the clone base; express a genuine change as a "
                    "net-new .supersede.<NNNN>.yaml file, OMN-16071)"
                )
            elif path not in allowed_paths:
                violations.append(f"{status} {path}")
        if violations:
            raise RuntimeError(
                "OCC companion append-only violation (OMN-14741 F-01): the "
                "generated tree changed files outside this run's contract + "
                "receipt set: "
                + "; ".join(sorted(violations))
                + ". Allowed: "
                + ", ".join(sorted(allowed_paths))
            )

    def _is_sanctioned_contract_growth(
        self, clone_dir: Path, base_sha: str, path: str
    ) -> bool:
        """True when a status-``M`` ticket contract change is a pure append.

        Re-derives the SAME judgment the hosted OCC Append-Only Gate makes
        (:func:`evaluate_append_only`) directly against the base/head contract
        bytes, rather than trusting the coarser git status letter. Fails
        CLOSED (returns ``False``) on any read/parse error or non-dict YAML,
        or when the gate's own per-entry evaluation finds a removed or
        content-altered ``dod_evidence`` id — a purely additive change (only
        new entries) is the only shape that returns ``True``.
        """
        try:
            base_text = self._run_git(
                ["git", "show", f"{base_sha}:{path}"], cwd=str(clone_dir)
            )
            head_text = self._run_git(
                ["git", "show", f"HEAD:{path}"], cwd=str(clone_dir)
            )
            base_contract = yaml.safe_load(base_text)
            head_contract = yaml.safe_load(head_text)
        except Exception:
            return False
        if not isinstance(base_contract, dict) or not isinstance(head_contract, dict):
            return False
        result = evaluate_append_only(
            base_contract=base_contract, head_contract=head_contract
        )
        return result.ok

    def _run_git(self, argv: list[str], *, cwd: str) -> str:
        # Delegates to the shared transport, which redacts any embedded
        # x-access-token credential from a surfaced git error (OMN-13990).
        return run_git(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)

    def _head_sha(self, cwd: str) -> str:
        return self._run_git(["git", "rev-parse", "HEAD"], cwd=cwd)

    def _assert_base_still_fresh(
        self, *, base_sha: str, token: str, cwd: str, tickets: Sequence[str]
    ) -> None:
        """Fail fast (OMN-15845) ONLY on a same-ticket collision, not any churn.

        Cheap freshness check — ``git ls-remote <url> HEAD`` — run immediately
        before EACH force-push. When the remote's current default-branch HEAD
        SHA still matches the SHA this run's clone was cut from, this returns
        immediately (no fetch, no diff).

        OMN-16116 (round-2 narrowing): a raw SHA mismatch is NOT itself a
        collision — live measurement showed OCC's default branch churns
        roughly every 24 minutes on average (bursts of 5 commits in 20
        minutes observed), almost entirely on OTHER tickets. The original
        ``remote_sha != base_sha`` predicate turned the rare same-ticket race
        this ticket exists to catch into a frequent liveness problem: any
        mint whose clone-to-push window overlapped an unrelated ticket's OCC
        merge would hard-abort. When the remote HAS moved, this now does a
        minimal single-commit fetch (never a full unshallow/merge/rebase —
        that would require recomputing ``contract_already_had_companion`` and
        every downstream receipt, a much larger change than this fail-fast
        fix) and diffs just the two trees, scoped to whether the new commit
        touched THIS run's own ticket(s) — ``contracts/<ticket>.yaml`` or
        anything under ``drift/dod_receipts/<ticket>/``. Only that scoped
        collision raises :class:`StaleCompanionBaseError`; an unrelated-ticket
        move on OCC's default branch is allowed to proceed.
        """
        remote_url = authenticated_occ_url(token, self._occ_repo)
        output = self._run_git(["git", "ls-remote", remote_url, "HEAD"], cwd=cwd)
        fields = output.split()
        remote_sha = fields[0] if fields else ""
        if not remote_sha or not SHA_RE.match(remote_sha):
            raise StaleCompanionBaseError(
                "could not verify OCC base freshness before push: "
                f"unparseable `git ls-remote` output {output!r}"
            )
        if remote_sha == base_sha:
            return  # fast path: remote hasn't moved — no fetch/diff needed.

        # The remote moved. Pull in just the one new commit (against the
        # already-configured ``origin`` remote, which carries the same
        # authenticated URL the initial shallow clone used) and diff trees —
        # `git diff` compares commit trees directly and needs no shared
        # ancestry, so this works even though both sides are independent
        # depth=1 shallow fetches.
        self._run_git(["git", "fetch", "--depth=1", "origin", remote_sha], cwd=cwd)
        diff_output = self._run_git(
            ["git", "diff", "--name-only", base_sha, remote_sha], cwd=cwd
        )
        changed_paths = {p.strip() for p in diff_output.splitlines() if p.strip()}
        scoped_prefixes = self._ticket_scoped_path_prefixes(tickets)
        colliding = sorted(
            path
            for path in changed_paths
            if any(
                path == prefix or path.startswith(prefix) for prefix in scoped_prefixes
            )
        )
        if not colliding:
            logger.info(
                "occ_companion_emitter: OCC default branch moved from %s to "
                "%s but the diff touches none of this run's ticket-scoped "
                "paths %s — proceeding (OMN-16116 narrowing).",
                base_sha,
                remote_sha,
                sorted(scoped_prefixes),
            )
            return
        raise StaleCompanionBaseError(
            f"OCC default branch moved from {base_sha} to {remote_sha} since "
            "this run's clone, and the diff touches this run's own "
            f"ticket-scoped path(s): {', '.join(colliding)} — a sibling "
            "companion for the same ticket likely merged in between; "
            "refusing to force-push a stale-based companion (OMN-15845). "
            "Safe to retry: this producer re-fires on the product PR's next "
            "lifecycle event and will clone a fresh base."
        )

    @staticmethod
    def _ticket_scoped_path_prefixes(tickets: Iterable[str]) -> set[str]:
        """Path prefixes that scope the OMN-15845/OMN-16116 freshness check.

        Mirrors :meth:`_allowed_paths`'s path construction (contract +
        receipt-tree layout) but evidence-id-agnostic: unlike
        ``_allowed_paths`` (which names exact receipt files THIS run writes),
        a same-ticket collision is any change anywhere under a ticket's
        ``drift/dod_receipts/<ticket>/`` tree — including a sibling
        companion's OWN evidence ids, which this run never writes and so
        would never appear in ``_allowed_paths``' output.
        """
        prefixes: set[str] = set()
        for ticket in tickets:
            prefixes.add(f"contracts/{ticket}.yaml")
            prefixes.add(f"drift/dod_receipts/{ticket}/")
        return prefixes

    @staticmethod
    def _receipt_commit_sha(pr_data: dict[str, object], head_sha: str) -> str:
        """Resolve the commit SHA the downstream receipt should cite (OMN-14255).

        Returns the actual squash ``merge_commit_sha`` ONLY when the PR is truly
        merged; otherwise the reviewed ``head_sha``. GitHub's REST ``merge_commit_sha``
        on an OPEN PR is a throwaway *test-merge* SHA that does not exist on any
        branch, so it must never be cited — gate on the ``merged`` / ``merged_at``
        facts before trusting it. Pure function — no I/O.
        """
        merged = bool(pr_data.get("merged")) or bool(pr_data.get("merged_at"))
        merge_commit_sha = pr_data.get("merge_commit_sha")
        if (
            merged
            and isinstance(merge_commit_sha, str)
            and SHA_RE.match(merge_commit_sha)
        ):
            return merge_commit_sha
        return head_sha

    def _open_or_sync_occ_pr(
        self, *, branch: str, ticket: str, repo: str, pr_number: int
    ) -> int:
        """Open the OCC binding PR, or return the existing PR for this branch.

        ``synchronize`` re-fires the emitter for the same product PR; the OCC
        branch already exists and pushing updates it, so a fresh ``create`` 422s.
        Look up the open PR for the branch head first.
        """
        token = _resolve_github_token()
        owner, repo_name = split_repo(self._occ_repo)

        existing_number = self._first_open_pr_number(owner, repo_name, branch, token)
        if existing_number is not None:
            # OMN-14893: OCC#4661 shipped with no distinguishable provenance
            # marker because this emitter never applied one — the ONLY signal
            # was the git commit author, which OMN-14893's own investigation
            # showed is subtle enough to cause a real misattribution. Verify
            # (idempotent add) the marker on every sync too, not only create,
            # so a companion opened before this fix landed gets it retro-
            # actively on its next `synchronize` re-fire.
            self._apply_machine_minted_label(owner, repo_name, existing_number, token)
            return existing_number

        # Human prose is authored here; the Evidence-Ticket line is rendered by
        # the Piece-2 core renderer over the typed stamp (no inline stamp text).
        prose = (
            f"Autobind OCC evidence for `{ticket}`.\n\n"
            f"Triggered by node_pr_lifecycle_fix_effect OCC companion emitter on "
            f"{repo}#{pr_number} (OMN-13317 F1 / OMN-14285).\n"
        )
        body = render_occ_companion_pr_body(prose, tickets=[ticket])
        # OMN-13990: target OCC's DEFAULT branch, not a hardcoded "main". The
        # branch is cut from the shallow clone of the default (OCC default is
        # `dev`); a PR based on "main" surfaces the entire dev<->main delta
        # (thousands of files) with the 3 companion files buried in it — an
        # unmergeable mega-PR. Basing on the default keeps the companion PR a
        # clean net-new-files diff.
        base = self._occ_default_branch(owner, repo_name, token)
        resp = rest_json(
            "POST",
            f"/repos/{owner}/{repo_name}/pulls",
            token=token,
            body={
                "title": (
                    f"evidence({ticket}): OCC Evidence-Source autobind for "
                    f"{repo}#{pr_number}"
                ),
                "head": branch,
                "base": base,
                "body": body,
            },
        )
        number = resp.get("number")
        if not isinstance(number, int):
            raise RuntimeError(
                f"OCC PR creation returned unexpected number field: {number!r}"
            )
        # OMN-14893 provenance marker: see the sync-path comment above.
        self._apply_machine_minted_label(owner, repo_name, number, token)
        return number

    @staticmethod
    def _apply_machine_minted_label(
        owner: str, repo_name: str, occ_pr_number: int, token: str
    ) -> None:
        """Add the machine-minted marker + ``ci:ready`` labels to the OCC PR.

        Two labels land in one POST (:data:`OCC_AUTHOR_TIME_LABELS`): the
        distinguishable marker (OMN-14393 / OMN-14893) that lets the
        report-only window — and any human skimming the PR list — decide
        ``minted_by_node`` without inspecting git commit authorship, and
        ``ci:ready`` (OMN-16071), which decides whether the companion's
        required CI wave runs at all on the OCC repo's label-gated pilot.
        Mirrors ``HandlerOccCompanionEffect``'s ``_apply_machine_minted_label``
        (net-negative-surface: same label constant, same
        ``call_with_retry``-wrapped ``rest_json_array`` helper).

        RETRYABLE + FAIL-CLOSED (OMN-16071 CodeRabbit follow-up): routed
        through :func:`omnimarket.occ_git_transport.call_with_retry`, which
        retries a bounded 3 attempts on a transient transport shape (network /
        5xx) before re-raising. A prior revision logged-and-swallowed every
        failure with the rationale "the label is observability, not a gate" —
        that no longer holds now that ``ci:ready`` is CI-gating: a swallowed
        failure here would report a companion as successfully authored while it
        silently carries only the marker and can never pass CI Summary,
        reproducing the exact OCC#6540-class stall this ticket fixes. This
        emitter is the LIVE producer per
        ``reference_two_occ_producers_canonical_not_wired``, so this path is
        where that failure mode would actually be observed in production.

        OMN-15441: routed through ``rest_json_array``, not ``rest_json``. The
        labels endpoint responds with the issue's full label ARRAY, which
        ``rest_json``'s dict-only contract rejects with "unexpected JSON
        response type". That shape defect is orthogonal to this
        retry/propagate contract and remains fixed: a successful POST still
        decodes cleanly.
        """
        call_with_retry(
            rest_json_array,
            "POST",
            f"/repos/{owner}/{repo_name}/issues/{occ_pr_number}/labels",
            token=token,
            body={"labels": list(OCC_AUTHOR_TIME_LABELS)},
        )

    @staticmethod
    def _occ_default_branch(owner: str, repo_name: str, token: str) -> str:
        """Return the OCC repo's default branch (the correct companion PR base)."""
        info = rest_json("GET", f"/repos/{owner}/{repo_name}", token=token)
        default = info.get("default_branch")
        if not isinstance(default, str) or not default:
            raise RuntimeError(
                f"could not resolve default branch for {owner}/{repo_name}"
            )
        return default

    @staticmethod
    def _first_open_pr_number(
        owner: str, repo_name: str, branch: str, token: str
    ) -> int | None:
        """Return the number of an open PR for ``branch``, or None.

        Uses the GitHub search API (returns an object with ``items``) so the
        dict-returning ``rest_json`` contract holds.
        """
        query = f"repo:{owner}/{repo_name} is:pr is:open head:{branch}"
        from urllib.parse import quote

        resp = rest_json(
            "GET",
            f"/search/issues?q={quote(query)}",
            token=token,
        )
        items = resp.get("items")
        if isinstance(items, list) and items:
            number = items[0].get("number")
            if isinstance(number, int):
                return number
        return None

    def _patch_evidence_source(
        self,
        *,
        repo: str,
        pr_number: int,
        occ_pr_number: int,
        tickets: Sequence[str],
        existing_body: str,
    ) -> None:
        """Rebind the product PR body to ``Evidence-Source: OCC#<n>`` via REST PATCH.

        The new body is produced entirely by the Piece-2 core renderer over the
        typed stamp (human prose preserved verbatim, one canonical Evidence
        block) — no inline f-string authoring. Idempotent: when the rendered body
        equals the existing body there is nothing to write.

        ``gh pr edit`` and GraphQL silently no-op on Projects-classic repos
        (friction #7); REST PATCH of the body is the reliable path.

        OMN-15441: this is the ONLY write this producer makes outside
        ``onex_change_control``, so it resolves the product-repo-scoped
        credential rather than reusing the OCC one. See
        :func:`_resolve_product_token` for why the default ``pat`` mode masks
        the defect today and why ``app`` mode would reproduce the 403 here.
        """
        new_body = render_product_pr_body_with_occ_source(
            existing_body, occ_pr_number=occ_pr_number, tickets=tickets
        )
        if new_body == existing_body:
            return  # already canonical — no-op
        occ_token = _resolve_github_token()
        token, dedicated = _resolve_product_token(occ_token)
        owner, repo_name = split_repo(repo)
        try:
            rest_json(
                "PATCH",
                f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
                token=token,
                body={"body": new_body},
            )
        except GitHubApiError as exc:
            if exc.status_code != 403:
                raise
            # Self-diagnosing scope mismatch, mirroring the effect handler:
            # GitHub's bare "Resource not accessible by integration" cost a
            # full triage pass on OMN-15441.
            source = (
                f"the dedicated {_PRODUCT_TOKEN_ENV_VAR} credential"
                if dedicated
                else (
                    f"the OCC credential (no {_PRODUCT_TOKEN_ENV_VAR} was "
                    f"supplied, so the OCC token was reused — under "
                    f"{_GITHUB_AUTH_MODE_ENV_VAR}=app that is scoped to "
                    f"onex_change_control and can never write to "
                    f"{owner}/{repo_name})"
                )
            )
            raise GitHubApiError(
                f"403 patching {owner}/{repo_name}#{pr_number} body using "
                f"{source}. The Evidence-Source stamp needs "
                f"'pull_requests: write' on {owner}/{repo_name}; supply a "
                f"product-repo-scoped credential via {_PRODUCT_TOKEN_ENV_VAR}. "
                f"Underlying error: {exc}",
                status_code=403,
            ) from exc


__all__ = ["OccCompanionEmitter", "StaleCompanionBaseError"]
