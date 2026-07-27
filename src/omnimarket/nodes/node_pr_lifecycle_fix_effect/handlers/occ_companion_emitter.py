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

# OMN-13990 (D3, validator-parity ticket extraction): the emitter uses the SAME
# ticket-extraction the occ-preflight eligibility validator uses so the two can
# never cite a different ticket set. This is the gate's own private helper —
# importing it (rather than re-deriving the regex) binds the emitter to the gate
# exactly. omnibase-core is a declared omnimarket dependency.
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

from omnimarket.events.occ_autoauthor import OCC_MACHINE_MINTED_LABEL
from omnimarket.github_api import (
    GitHubApiError,
    rest_json,
    rest_json_array,
    split_repo,
)
from omnimarket.github_app_auth import resolve_app_installation_token_from_contract
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    SHA_RE,
    ci_check_evidence_id,
    compute_contract_sha256,
    extract_evidence_item_id,
    rebind_contract_entry_sha256_in_text,
    rebind_contract_sha256_in_text,
    receipt_local_check_value,
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
    extract_symbol_candidates,
    is_yamlfmt_stable_check,
    resolve_red_ref,
    select_asserted_check,
)

# OMN-15247: contention detection + the two default-OFF producer mode vars. Both
# live in shared top-level modules (never a cross-node private import) so the
# born-path emitter and node_occ_state_effect derive from ONE definition each.
from omnimarket.occ_contention import (
    ContentionFinding,
    EnumCheckBinding,
    EnumContentionPolicy,
    decide_contention,
    find_open_companions,
    resolve_occ_producer_policy,
)
from omnimarket.occ_git_transport import (
    OCC_REPO,
    acquire_occ_companion_lease,
    authenticated_occ_url,
    release_occ_companion_lease,
    run_git,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# OMN-14031: bound every git subprocess so a stalled network git call (push /
# fetch under egress saturation) fails fast instead of wedging the fix-effect
# path — the same un-timed-subprocess hang class fixed in the inventory node.
_GIT_TIMEOUT_SECONDS = 120

_OCC_REPO = OCC_REPO

_DEFAULT_RUNNER = "node_pr_lifecycle_fix_effect"
_DEFAULT_VERIFIER = "occ-evidence-source-autobind"

# OMN-14793 (OMN-14783 rec #2): the single-producer lease TTL. Floor is the
# worst-case mint duration (clone + double force-push + PR open) with margin; the
# per-git-op bound is ``_GIT_TIMEOUT_SECONDS`` (120s) x several network git ops,
# so 900s comfortably clears a slow-but-live producer while still self-healing a
# crashed one within 15 min. Too short → a live producer's lease is stolen
# mid-mint (re-introduces the race); too long → a crashed producer wedges a head.
_DEFAULT_LEASE_TTL_SECONDS = 900

# OMN-14741 F-17: emission is suppressed for a product PR that is closed, a draft,
# or explicitly marked do-not-merge. A do-not-merge marker is any of these tokens
# in the PR TITLE (case-insensitive) or a matching label name. The compact regex
# tolerates the common spellings (``DO NOT MERGE`` / ``DO-NOT-MERGE`` /
# ``DONOTMERGE``) plus ``[WIP]`` / ``WORK IN PROGRESS``.
_DO_NOT_MERGE_RE = re.compile(
    r"\bDO[\s_-]?NOT[\s_-]?MERGE\b|\bWORK[\s_-]?IN[\s_-]?PROGRESS\b|\[\s*WIP\s*\]",
    re.IGNORECASE,
)


# OMN-14893: the OCC machine path defaults to the shared operator PAT
# (``pat`` mode, unchanged behavior) until ``ONEXBOT_OCC_APP_ID`` /
# ``ONEXBOT_OCC_PRIVATE_KEY`` are provisioned to this runtime. Flipping
# ``OMNI_OCC_GITHUB_AUTH_MODE=app`` routes through
# ``resolve_app_installation_token_from_contract`` instead, which NEVER reads
# ``GITHUB_TOKEN`` — the fallback that reproduced OMN-14893's original defect
# is not merely avoided by an ``if``, it is mechanically absent from that
# code path (see ``github_app_auth`` module docstring).
_GITHUB_AUTH_MODE_ENV_VAR = "OMNI_OCC_GITHUB_AUTH_MODE"


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
        contention_policy: EnumContentionPolicy | None = None,
        check_binding: EnumCheckBinding | None = None,
    ) -> None:
        # OMN-15247: both new behaviors resolve from the environment here, so the
        # two no-arg construction sites (handler_pr_lifecycle_fix_runtime.py and
        # handler_pr_lifecycle_orchestrator.py) need no change; tests inject
        # directly. Resolution is FAIL-CLOSED — an unrecognized value raises
        # naming the var and the accepted set, never silently defaults.
        # Shipped defaults (observe / pr_existence) reproduce today's bytes.
        resolved_policy = resolve_occ_producer_policy(os.environ)
        self._contention_policy = (
            contention_policy
            if contention_policy is not None
            else resolved_policy.contention_policy
        )
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
        already_bound = product_pr_occ_binding(body)
        if already_bound is not None:
            action = (
                f"no-op: {repo}#{pr_number} already bound to "
                f"OCC#{already_bound} (Evidence-Source already an OCC source)"
            )
            logger.info("occ_companion_emitter: %s", action)
            return action

        # 2. Validator-parity ticket extraction (OMN-13990 D3): the gate owns
        #    tickets via closing-keyword-exclusive-else-all-title-tokens; author a
        #    companion for EVERY cited ticket. Fall back to the caller-supplied
        #    ticket only when the PR cites none.
        tickets = self._extract_tickets(title, body)
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
        branch = f"auto/{repo_slug.lower()}-pr-{pr_number}-occ-autobind"
        evidence_id = f"dod-{repo_slug}-pr-{pr_number}"
        ci_evidence_id = ci_check_evidence_id(evidence_id)

        # OMN-15247 deliverable A — DEFER-ON-CONTENTION. Placed AFTER the
        # already-bound idempotency check and ticket extraction, and BEFORE
        # ``acquire_occ_companion_lease``, so a defer takes the lease-free,
        # zero-side-effect path (no lease, no clone, no branch, no push, no PR,
        # no ``_patch_evidence_source``).
        #
        # The DETECTION runs unconditionally in BOTH modes — only the enforcement
        # is gated (see occ_contention's module docstring on why that is not the
        # silent no-check the always-on lease reasoning rejects).
        findings = self._find_contending_companions(
            tickets=tickets, own_branch=branch, token=token
        )
        should_defer, contention_reason = decide_contention(
            findings, self._contention_policy
        )
        for finding in findings:
            logger.warning(
                "occ_companion_emitter contention: ticket=%s occ_pr=%s "
                "provenance=%s policy=%s would_defer=%s reason=%s",
                finding.ticket_id,
                finding.occ_pr_number,
                finding.provenance.value,
                self._contention_policy.value,
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
        ci_probe_command = f"gh pr view {pr_number} --repo {repo} --json files"
        ci_stdout, ci_exit = self._observe_pr_probe(
            probe_command=ci_probe_command,
            token=token,
            fallback={"number": pr_number, "note": "diff not observed"},
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
            # Precedence: private-repo hosted-safe form (OMN-14766 F-16) wins,
            # since a hosted content read cannot run there at all. Otherwise a
            # content_bound-enabled producer overrides the DOWNSTREAM check with
            # the RED-proven content read; the CI/diff-scope check is untouched.
            if is_private:
                return (
                    receipt_local_check_value(
                        ticket_id=ticket, evidence_id=evidence_id
                    ),
                    receipt_local_check_value(
                        ticket_id=ticket, evidence_id=ci_evidence_id
                    ),
                )
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
                for ticket in tickets:
                    downstream_check_value, ci_check_value = _hosted_safe_check_values(
                        ticket
                    )
                    contract_path = clone_dir / "contracts" / f"{ticket}.yaml"
                    contract_path.parent.mkdir(parents=True, exist_ok=True)
                    if not contract_path.is_file():
                        contract_path.write_text(
                            render_companion_contract(
                                ticket_id=ticket,
                                repo=repo,
                                pr_number=pr_number,
                                evidence_id=evidence_id,
                                downstream_check_value=downstream_check_value,
                                ci_check_value=ci_check_value,
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
                    downstream_dir = (
                        clone_dir / "drift" / "dod_receipts" / ticket / evidence_id
                    )
                    downstream_dir.mkdir(parents=True, exist_ok=True)
                    (downstream_dir / "command.yaml").write_text(
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
                    # probe above.
                    ci_dir = (
                        clone_dir / "drift" / "dod_receipts" / ticket / ci_evidence_id
                    )
                    ci_dir.mkdir(parents=True, exist_ok=True)
                    (ci_dir / "command.yaml").write_text(
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
                    # OMN-14741 F-01: rebind ONLY this PR's own receipts, never rglob
                    # every receipt under <ticket>/. The whole-file contract_sha256 of
                    # a PRIOR merged receipt for the same ticket goes stale when the
                    # contract grows, but the eligibility/receipt gates grandfather a
                    # prior merged receipt's whole-file hash — so rewriting it here is
                    # a NON-append-only mutation of an already-merged receipt (the
                    # OCC#4293/4295/4296 class). Scope the rebind to this PR's rows.
                    self._rebind_receipts(
                        clone_dir, ticket, contract_path, {evidence_id, ci_evidence_id}
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
                self._assert_append_only(
                    clone_dir,
                    base_sha,
                    self._allowed_paths(tickets, {evidence_id, ci_evidence_id}),
                )
                # Force-push: the auto/* bot branch is fully REGENERATED each run
                # (fresh clone off the default + freshly-timestamped receipts), so a
                # `synchronize` re-fire produces history disjoint from the already
                # pushed remote branch — a plain push would be rejected non-fast-
                # forward (OMN-13990 / CodeRabbit). Force-push is safe here (content
                # is deterministic and the branch always presents the companion as
                # all-adds relative to base, keeping the append-only gate green).
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
                    f"gh pr view {occ_pr_number} --repo {self._occ_repo} "
                    "--json number,state"
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
                    self._rebind_receipts(
                        clone_dir,
                        ticket,
                        contract_paths[ticket],
                        {evidence_id, ci_evidence_id, self_bind_evidence_id},
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
                self._assert_append_only(
                    clone_dir,
                    base_sha,
                    self._allowed_paths(
                        tickets, {evidence_id, ci_evidence_id, self_bind_evidence_id}
                    ),
                )
                # Force-push (see rationale above): deterministic all-adds regeneration.
                self._run_git(
                    ["git", "push", "--force", "origin", branch], cwd=str(clone_dir)
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
    def _extract_tickets(title: str, body: str) -> list[str]:
        """Return the gate-identical cited ticket set (OMN-13990 D3).

        Delegates to the occ-preflight eligibility validator's ``_extract_ticket_ids``
        (body closing-keywords exclusive, else all title tokens) so the emitter
        authors a companion for exactly the tickets the gate will check.
        """
        return _extract_ticket_ids(body, title)

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
        if isinstance(title, str) and _DO_NOT_MERGE_RE.search(title):
            return "PR_DO_NOT_MERGE"
        labels = pr_data.get("labels")
        if isinstance(labels, list):
            for label in labels:
                name = label.get("name") if isinstance(label, dict) else None
                if isinstance(name, str) and _DO_NOT_MERGE_RE.search(name):
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

        Runs in BOTH modes — only the enforcement is policy-gated. The three I/O
        halves are bound here and the decision logic stays pure in
        :func:`omnimarket.occ_contention.find_open_companions`.
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

        check = select_asserted_check(
            candidates,
            repo=repo,
            head_sha=evidence_ref,
            base_sha=red_ref,
            fetch_content=_fetch,
            # Destination-specific constraint: this string is written to a
            # CONTRACT's ``check_value:`` line (indent 8). yamlfmt folds it at
            # the first space past column 100, which would restale
            # contract_sha256 (F-03 / OMN-14684). The selector cannot know the
            # destination indent, so the guard is applied here.
            accept=is_yamlfmt_stable_check,
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
    def _allowed_paths(tickets: Iterable[str], evidence_ids: Iterable[str]) -> set[str]:
        """Repo-relative paths this run is permitted to add/modify (F-01)."""
        eids = list(evidence_ids)
        allowed: set[str] = set()
        for ticket in tickets:
            allowed.add(f"contracts/{ticket}.yaml")
            for eid in eids:
                allowed.add(f"drift/dod_receipts/{ticket}/{eid}/command.yaml")
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

    def _run_git(self, argv: list[str], *, cwd: str) -> str:
        # Delegates to the shared transport, which redacts any embedded
        # x-access-token credential from a surfaced git error (OMN-13990).
        return run_git(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)

    def _head_sha(self, cwd: str) -> str:
        return self._run_git(["git", "rev-parse", "HEAD"], cwd=cwd)

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
        """Best-effort: add the machine-minted marker label to the OCC PR.

        The distinguishable marker (OMN-14393 / OMN-14893) that lets the
        report-only window — and any human skimming the PR list — decide
        ``minted_by_node`` without inspecting git commit authorship (the
        signal that caused a real misattribution, per the OMN-14893
        investigation). Mirrors ``HandlerOccCompanionEffect``'s
        ``_apply_machine_minted_label`` byte-for-byte (net-negative-surface:
        same label constant, same best-effort contract). Non-fatal: any
        failure is logged and swallowed so a label API hiccup can never abort
        a successful author.
        """
        try:
            rest_json(
                "POST",
                f"/repos/{owner}/{repo_name}/issues/{occ_pr_number}/labels",
                token=token,
                body={"labels": [OCC_MACHINE_MINTED_LABEL]},
            )
        except (
            GitHubApiError,
            OSError,
        ) as exc:  # fallback-ok: label is observability, not a gate
            logger.warning(
                "occ_companion_emitter: could not apply %r label to OCC#%s: %s",
                OCC_MACHINE_MINTED_LABEL,
                occ_pr_number,
                exc,
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
        """
        new_body = render_product_pr_body_with_occ_source(
            existing_body, occ_pr_number=occ_pr_number, tickets=tickets
        )
        if new_body == existing_body:
            return  # already canonical — no-op
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)
        rest_json(
            "PATCH",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
            token=token,
            body={"body": new_body},
        )


__all__ = ["OccCompanionEmitter"]
