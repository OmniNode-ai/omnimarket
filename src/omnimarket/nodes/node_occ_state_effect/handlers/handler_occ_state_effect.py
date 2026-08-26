# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccStateEffect — the RSD-2 read-EFFECT for node_occ_companion_compute (OMN-14619).

Gathers all machine-observed PR + OCC facts the pure COMPUTE node
(``node_occ_companion_compute``, RSD-1, OMN-14285) needs to render a companion,
and assembles the exact :class:`ModelOccCompanionRequest` it consumes. This is
the read half of the read -> compute -> write cycle the design doc
(``docs/plans/2026-07-10-occ-autogen-mechanization-design.md``) specifies;
RSD-3 (the write-EFFECT that clones/pushes/opens the companion PR, plus the
trigger/orchestrator) is intentionally NOT built here — OMN-14619 is READ-ONLY
by design so it can be proven against real PRs without wiring anything into
the live process.

Second responsibility (OMN-14619's other half): derive an honest **content-read**
``downstream_check_value`` from the PR diff instead of leaving the COMPUTE node
to fall back to its generic ``gh pr view ... --json number,state`` probe, which
proves only that the PR exists — never that the claimed work landed (it can
never go RED against an EXISTS-but-WRONG PR). :func:`extract_symbol_candidates`
+ :func:`select_asserted_check` are pure functions (given an injected content
fetcher) so the RED-control logic is unit-testable without live network calls;
only :class:`HandlerOccStateEffect` itself performs I/O.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from omnibase_core.validation.validator_receipt_gate import _extract_ticket_ids

from omnimarket.events.occ_companion import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.github_api import GitHubApiError, rest_json, rest_json_array, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)
from omnimarket.occ_content_probe import (
    SymbolCandidate,
    build_content_read_check,
    declaration_count,
    extract_symbol_candidates,
    resolve_red_ref,
    select_asserted_check,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# Matches an added top-level declaration line in a unified diff hunk, e.g.
# "+class HandlerCodegenOutcomeReducer:" or "+    async def handle(self, ...):".
# Deterministic, LLM-free stand-in for "pick a symbol the PR adds" — the exact
# authoring move the hand-authored reference companions (OCC#4135, OCC#4136)
# made manually ("class HandlerCodegenOutcomeReducer defined ... — head 1 / dev 404").
_DECLARATION_RE = re.compile(
    r"^\+\s*(async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

_OCC_DEFAULT_TARGET_BRANCH = "dev"  # OCC companion PRs always target dev (never main).


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an ``object``-typed API field to a dict, or empty on any other shape."""
    return value if isinstance(value, dict) else {}


def _as_int(value: object, default: int = 0) -> int:
    """Narrow an ``object``-typed API field to an int, or ``default`` otherwise."""
    return value if isinstance(value, int) else default


def extract_pr_label_names(pr_payload: dict[str, object]) -> tuple[str, ...]:
    """Pure: extract the PR label names from a GitHub PR REST payload (F-17).

    REST ``labels`` is a list of ``{"name": ...}`` objects; anything else (a
    missing key, a non-list, a label without a name) yields no name. Kept a pure
    function of the payload — like ``extract_symbol_candidates`` — so the F-17
    label-suppression seam is unit-testable without the live network half.
    """
    labels_raw = pr_payload.get("labels")
    if not isinstance(labels_raw, list):
        return ()
    return tuple(
        str(label["name"])
        for label in labels_raw
        if isinstance(label, dict) and label.get("name")
    )


def _resolve_github_token() -> str:
    """Resolve the GitHub token from the contract-declared ref (OMN-12856)."""
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


# OMN-15247: the OMN-14619 pure content-probe functions (``SymbolCandidate``,
# ``extract_symbol_candidates``, ``declaration_count``, ``build_content_read_check``,
# ``select_asserted_check``) MOVED to ``omnimarket.occ_content_probe`` so the
# born-path producer (``OccCompanionEmitter``) can derive the same content-bound
# check without importing this node's private handler package. They are re-exported
# here — unchanged behavior, one definition each (feedback_one_canonical_model_per
# _shape) — so this module's public names and its existing tests are untouched.

__all__ = [
    "HandlerOccStateEffect",
    "SymbolCandidate",
    "build_content_read_check",
    "declaration_count",
    "extract_pr_label_names",
    "extract_symbol_candidates",
    "resolve_red_ref",
    "select_asserted_check",
]


class HandlerOccStateEffect:
    """EFFECT handler: gather live PR + OCC facts into a ModelOccCompanionRequest.

    Read-only — never clones, writes, or pushes. Zero mutation to any GitHub
    surface. Companion (git clone/push/PR-open) is RSD-3, deliberately not
    built here.
    """

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        logger.info(
            "occ_state_effect: repo=%s pr=%s correlation_id=%s",
            request.repo,
            request.pr_number,
            request.correlation_id,
        )
        return await asyncio.to_thread(self._gather_sync, request)

    # -- gather -------------------------------------------------------------

    def _gather_sync(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        token = _resolve_github_token()
        owner, repo_name = split_repo(request.repo)

        pr = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{request.pr_number}", token=token
        )
        head = _as_dict(pr.get("head"))
        base = _as_dict(pr.get("base"))
        head_sha = str(head.get("sha") or "")
        title = str(pr.get("title") or "")
        body = str(pr.get("body") or "")
        pr_state = str(pr.get("state") or "open")
        # OMN-16665: REST returns state=="closed" for a merged PR AND for an
        # abandoned one, so state alone cannot tell a dead target from a
        # permanent evidence hole. ``merged`` is the authoritative discriminator
        # on the pulls payload; ``merged_at`` is its non-null sibling and is read
        # as a fallback so an unexpectedly-absent boolean does not silently
        # downgrade a merged PR to "closed, nothing lost".
        pr_merged = bool(pr.get("merged")) or bool(pr.get("merged_at"))
        pr_is_draft = bool(pr.get("draft"))
        head_ref = str(head.get("ref") or "")
        # F-17 parity (OMN-14785): carry PR label names so the pure COMPUTE can
        # suppress a do-not-merge/WIP-LABELLED PR (the emitter checks labels; the
        # canonical producer must too).
        pr_labels = extract_pr_label_names(pr)
        # OMN-14783 F-16 / OMN-14766: the base repo is always the PRODUCT repo (even
        # for a fork PR whose head.repo is the fork). A private product repo cannot
        # be re-probed by the hosted OCC contract-compliance runner (its token has
        # no scope there), so the COMPUTE must render declared check_values
        # receipt-local (OCC#4307/#4318). Read base.repo.private with no extra API
        # call — the emitter's _is_private_repo does the same. Fails SAFE to public
        # when the field is absent (a hosted gh check then fails loud in OCC CI
        # rather than a silent skip), mirroring the emitter.
        product_repo_private = bool(_as_dict(base.get("repo")).get("private"))

        files = self._list_files(owner, repo_name, request.pr_number, token)
        changed_files = tuple(str(f.get("filename", "")) for f in files)
        diff_total_lines = sum(
            _as_int(f.get("additions")) + _as_int(f.get("deletions")) for f in files
        )

        tickets = tuple(_extract_ticket_ids(body, title))
        occ_contract_states = tuple(
            self._occ_contract_state(request.occ_repo, ticket, token)
            for ticket in tickets
        )

        # OMN-14783 F-16 (D-16.3): the content-read check is a hosted
        # `gh api repos/<repo>/contents/...` re-run, which — like `gh pr view
        # --repo <private>` — fails under the hosted OCC runner's token for a
        # PRIVATE product repo. Suppress it there so the COMPUTE falls back to the
        # hosted-safe receipt-local check_value (driven by product_repo_private)
        # instead of shipping an un-runnable hosted content-read.
        #
        # OMN-15247 (B2): the RED reference is the MERGE BASE, not ``base.sha``.
        # ``base.sha`` is the base branch TIP recorded on the PR object; on a base
        # branch that moved after the PR was cut it may already contain the symbol,
        # wrongly rejecting a genuinely RED-derivable candidate. OMN-15247's
        # acceptance bar names the merge base explicitly. ``resolve_red_ref``
        # returns None when the ref cannot be resolved, and a None ``red_ref``
        # emits NO content-bound check (fail-closed) rather than silently reverting
        # to the old, weaker reference.
        downstream_check_value: str | None = None
        red_ref = self._resolve_red_ref_live(owner, repo_name, pr, token)
        if head_sha and red_ref and not product_repo_private:
            candidates = extract_symbol_candidates(files)

            def _fetch(path: str, ref: str) -> str | None:
                return self._content_at_ref(request.repo, path, ref, token)

            downstream_check_value = select_asserted_check(
                candidates,
                repo=request.repo,
                head_sha=head_sha,
                base_sha=red_ref,
                fetch_content=_fetch,
            )

        product_probe = self._observe_pr_probe(
            pr_number=request.pr_number,
            repo=request.repo,
            token=token,
            fallback={
                "number": request.pr_number,
                "state": pr_state,
                "headRefName": head_ref,
            },
        )

        return ModelOccCompanionRequest(
            repo=request.repo,
            pr_number=request.pr_number,
            pr_head_sha=head_sha,
            pr_title=title,
            pr_body=body,
            pr_state=pr_state,
            pr_merged=pr_merged,
            allow_merged_replay=request.allow_merged_replay,
            pr_is_draft=pr_is_draft,
            pr_head_ref=head_ref,
            pr_labels=pr_labels,
            runner=request.runner,
            verifier=request.verifier,
            run_timestamp=datetime.now(UTC).isoformat(),
            product_probe=product_probe,
            occ_repo=request.occ_repo,
            occ_contract_states=occ_contract_states,
            changed_files=changed_files,
            diff_total_lines=diff_total_lines,
            downstream_check_value=downstream_check_value,
            product_repo_private=product_repo_private,
        )

    def _list_files(
        self, owner: str, repo_name: str, pr_number: int, token: str
    ) -> list[dict[str, object]]:
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

    def _occ_contract_state(
        self, occ_repo: str, ticket_id: str, token: str
    ) -> ModelOccContractState:
        """Read whether contracts/<ticket>.yaml already exists on OCC dev.

        OCC contracts only ever land on ``dev`` (main-target-guard rejects a
        companion PR targeting main), so a contract found at ``ref=dev`` is, by
        construction, already merged.
        """
        path = f"contracts/{ticket_id}.yaml"
        content = self._content_at_ref(
            occ_repo, path, _OCC_DEFAULT_TARGET_BRANCH, token
        )
        if content is None:
            return ModelOccContractState(ticket_id=ticket_id)

        whole_hash = hashlib.sha256(content.encode()).hexdigest()
        entry_ids: tuple[str, ...] = ()
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, dict):
            items = parsed.get("dod_evidence")
            if isinstance(items, list):
                entry_ids = tuple(
                    str(item.get("id"))
                    for item in items
                    if isinstance(item, dict) and item.get("id")
                )
        return ModelOccContractState(
            ticket_id=ticket_id,
            exists=True,
            merged=True,
            existing_entry_ids=entry_ids,
            whole_file_sha256=whole_hash,
            raw_contract_text=content,
        )

    def _resolve_red_ref_live(
        self, owner: str, repo_name: str, pr: dict[str, object], token: str
    ) -> str | None:
        """Resolve the merge-base RED ref for this PR, or None (OMN-15247 B2).

        Thin live-I/O wrapper over the pure :func:`resolve_red_ref`: it supplies
        the two GitHub reads (``/commits/{sha}`` for a merged PR's squash parent,
        ``/compare/{base}...{head}`` for an open PR) and degrades every API
        failure to ``None``. ``None`` means *no content-bound check is emitted*
        — fail-closed, never a fallback to the weaker ``base.sha``.
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
            return resolve_red_ref(pr_data=pr, compare=_compare, commit=_commit)
        except (GitHubApiError, OSError) as exc:  # fallback-ok: fail CLOSED to None
            logger.warning(
                "occ_state_effect: could not resolve merge-base RED ref for "
                "%s/%s (%s); no content-bound check will be derived",
                owner,
                repo_name,
                exc,
            )
            return None

    def _content_at_ref(self, repo: str, path: str, ref: str, token: str) -> str | None:
        """Fetch decoded file content at ``ref``, or None if absent/undecodable."""
        owner, repo_name = split_repo(repo)
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
        content_b64 = str(data.get("content", ""))
        try:
            raw = base64.b64decode(content_b64, validate=False)
        except (binascii.Error, ValueError):
            return None
        return raw.decode("utf-8", errors="replace")

    def _observe_pr_probe(
        self,
        *,
        pr_number: int,
        repo: str,
        token: str,
        fallback: dict[str, object],
    ) -> ModelObservedProbe:
        """Run the real ``gh pr view`` probe so the receipt carries a genuine
        machine observation (OMN-13990 item 4 / OMN-14055), mirroring
        ``OccCompanionEmitter._observe_pr_probe`` exactly.
        """
        command = (
            f"gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
        )
        fallback_json = json.dumps(fallback, separators=(",", ":"), sort_keys=True)
        try:
            env = os.environ.copy()
            env["GH_TOKEN"] = token
            result = subprocess.run(
                shlex.split(command),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return ModelObservedProbe(
                command=command, stdout=fallback_json, exit_code=0
            )
        if result.returncode != 0 or not result.stdout.strip():
            return ModelObservedProbe(
                command=command, stdout=fallback_json, exit_code=0
            )
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ModelObservedProbe(
                command=command,
                stdout=result.stdout.strip().replace("\n", " "),
                exit_code=0,
            )
        return ModelObservedProbe(
            command=command,
            stdout=json.dumps(parsed, separators=(",", ":"), sort_keys=True),
            exit_code=0,
        )


__all__ = [
    "HandlerOccStateEffect",
    "SymbolCandidate",
    "build_content_read_check",
    "declaration_count",
    "extract_pr_label_names",
    "extract_symbol_candidates",
    "select_asserted_check",
]
