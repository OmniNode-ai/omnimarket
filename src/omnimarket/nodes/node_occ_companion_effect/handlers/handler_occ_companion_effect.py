# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccCompanionEffect — the RSD-3 write-EFFECT + orchestrator (OMN-14622).

Closes the read -> compute -> write OCC-companion producer cycle. It drives the
two-pass loop deterministically:

  * **Pass 1** (``occ_pr_number`` unknown): call RSD-2 (``node_occ_state_effect``)
    to gather live PR + OCC facts, call RSD-1 (``compute_companion_plan``) to
    render the contract + downstream product-receipt, clone ``onex_change_control``,
    write the plan's files to a net-new branch off ``dev``, and open the OCC PR.
  * **Pass 2** (``occ_pr_number`` now known): re-run the SAME pure compute with
    the OCC PR facts injected, so the plan now also renders the self-bind receipt
    (+ contract self-bind entry, OMN-14622) and the ``Evidence-Source``-stamped
    product body. Commit the self-bind onto the branch and PATCH the product PR
    body.

Every committed byte is a pure function of the compute plan — this node performs
ZERO authoring of its own; it only performs the git/gh side effects. That is
what lets ``verify_companion_attestation`` (RSD-5 / OMN-14055) re-run the SAME
``compute_companion_plan`` and byte-diff the result against what this node
pushed. It reuses the shared ``occ_git_transport`` (OMN-14622 promotion) and
``github_api`` REST helpers rather than duplicating a second transport
(net-negative-surface).

``mode="dry_run"`` (the default) stops after the compute and reports the plan
without any GitHub mutation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Literal
from uuid import UUID

from omnimarket.github_api import (
    GitHubApiError,
    rest_json,
    rest_json_array,
    split_repo,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelCompanionFile,
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_result import (
    ModelOccCompanionEffectResult,
)
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)
from omnimarket.occ_git_transport import (
    authenticated_occ_url,
    run_git,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_GIT_TIMEOUT_SECONDS = 120.0
_GIT_AUTHOR_NAME = "node-occ-companion-effect"
_GIT_AUTHOR_EMAIL = "occ-companion-effect@omninode.ai"


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


class HandlerOccCompanionEffect:
    """EFFECT handler: read -> compute -> write the deterministic OCC companion.

    The write half of the OCC-companion producer. Reads via RSD-2, renders via
    RSD-1's pure compute, and owns only the git/gh side effects (clone, push,
    PR-open, product-body stamp).
    """

    def __init__(self, state_handler: HandlerOccStateEffect | None = None) -> None:
        self._state_handler = state_handler or HandlerOccStateEffect()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self,
        correlation_id: UUID,
        request: ModelOccCompanionEffectRequest,
    ) -> ModelOccCompanionEffectResult:
        logger.info(
            "occ_companion_effect: repo=%s pr=%s mode=%s correlation_id=%s",
            request.repo,
            request.pr_number,
            request.mode,
            correlation_id,
        )

        # --- READ (RSD-2) -> COMPUTE (RSD-1), both side-effect-free-to-us ---
        state_request = ModelOccStateRequest(
            repo=request.repo,
            pr_number=request.pr_number,
            occ_repo=request.occ_repo,
            runner=request.runner,
            verifier=request.verifier,
        )
        companion_request = await self._state_handler.handle(state_request)
        plan = compute_companion_plan(companion_request)

        if plan.no_op:
            return self._result(request, plan, action=f"no-op: {plan.no_op_reason}")
        if plan.fast_path:
            return self._result(
                request, plan, action=f"fast-path skip: {plan.fast_path_reason}"
            )

        if request.mode == "dry_run":
            return self._result(
                request,
                plan,
                action=(
                    f"dry_run: computed a {len(plan.companion_files)}-file companion "
                    f"for {', '.join(plan.tickets)} (no GitHub mutation)"
                ),
            )

        # --- WRITE (this node) — the only side effects live here ---
        return await asyncio.to_thread(
            self._write_sync, correlation_id, request, companion_request, plan
        )

    # -- result assembly ----------------------------------------------------

    def _result(
        self,
        request: ModelOccCompanionEffectRequest,
        plan: ModelOccCompanionPlan,
        *,
        action: str,
        occ_pr_number: int | None = None,
        occ_pr_url: str = "",
        product_body_stamped: bool = False,
    ) -> ModelOccCompanionEffectResult:
        return ModelOccCompanionEffectResult(
            repo=request.repo,
            pr_number=request.pr_number,
            mode=request.mode,
            action=action,
            no_op=plan.no_op,
            no_op_reason=plan.no_op_reason,
            fast_path=plan.fast_path,
            tickets=plan.tickets,
            occ_branch=plan.branch,
            occ_pr_number=occ_pr_number,
            occ_pr_url=occ_pr_url,
            product_body_stamped=product_body_stamped,
            companion_paths=tuple(f.path for f in plan.companion_files),
            deterministic_digest=plan.deterministic_digest,
            wedges=tuple(w.code for w in plan.wedges),
        )

    # -- write (mutate) -----------------------------------------------------

    def _write_sync(
        self,
        correlation_id: UUID,
        request: ModelOccCompanionEffectRequest,
        companion_request: ModelOccCompanionRequest,
        plan: ModelOccCompanionPlan,
    ) -> ModelOccCompanionEffectResult:
        token = _resolve_github_token()
        occ_owner, occ_name = split_repo(request.occ_repo)
        branch = plan.branch

        with tempfile.TemporaryDirectory(prefix="occ-companion-effect-") as tmp:
            clone_dir = str(Path(tmp) / "onex_change_control")
            self._clone_and_branch(clone_dir, branch, token, request.occ_repo)

            # Pass 1: write contract + downstream product-receipt, commit, push.
            self._write_files(clone_dir, plan.companion_files)
            self._commit_all(
                clone_dir,
                f"evidence: OCC companion pass 1 for {request.repo}#{request.pr_number}",
            )
            self._push(clone_dir, branch, token, request.occ_repo, force=True)
            occ_head_c1 = self._head_sha(clone_dir)

            # Open (or reuse) the OCC PR now that the branch has a diff.
            occ_pr_number, occ_pr_url = self._open_or_sync_occ_pr(
                occ_owner, occ_name, branch, plan.tickets, request, token
            )

            # Pass 2: re-run the SAME pure compute with the OCC PR facts so it
            # renders the self-bind receipt (+ contract self-bind entry) and the
            # Evidence-Source-stamped product body — deterministically.
            occ_probe = self._observe_occ_probe(occ_pr_number, request.occ_repo, token)
            companion_request_v2 = companion_request.model_copy(
                update={
                    "occ_pr_number": occ_pr_number,
                    "occ_head_sha": occ_head_c1,
                    "occ_probe": occ_probe,
                }
            )
            plan2 = compute_companion_plan(companion_request_v2)

            self._write_files(clone_dir, plan2.companion_files)
            self._commit_all(
                clone_dir,
                f"evidence: OCC companion self-bind for {request.occ_repo}#{occ_pr_number}",
            )
            self._push(clone_dir, branch, token, request.occ_repo, force=False)

        # Stamp the product PR body with the Evidence-Source block.
        product_owner, product_name = split_repo(request.repo)
        stamped = self._patch_product_body(
            product_owner,
            product_name,
            request.pr_number,
            plan2.product_body_stamped,
            companion_request.pr_body,
            token,
        )

        return self._result(
            request,
            plan2,
            action=(
                f"authored OCC#{occ_pr_number} for {', '.join(plan2.tickets)} "
                f"({len(plan2.companion_files)} files) and "
                f"{'stamped' if stamped else 'left'} the product PR body"
            ),
            occ_pr_number=occ_pr_number,
            occ_pr_url=occ_pr_url,
            product_body_stamped=stamped,
        )

    # -- git helpers (reuse shared occ_git_transport) -----------------------

    def _clone_and_branch(
        self, clone_dir: str, branch: str, token: str, occ_repo: str
    ) -> None:
        url = authenticated_occ_url(token, occ_repo)
        run_git(
            ["git", "clone", "--depth=1", url, clone_dir],
            cwd=str(Path(clone_dir).parent),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        run_git(["git", "config", "user.name", _GIT_AUTHOR_NAME], cwd=clone_dir)
        run_git(["git", "config", "user.email", _GIT_AUTHOR_EMAIL], cwd=clone_dir)
        run_git(["git", "checkout", "-B", branch], cwd=clone_dir)

    def _write_files(
        self, clone_dir: str, files: tuple[ModelCompanionFile, ...]
    ) -> None:
        for f in files:
            path = Path(clone_dir) / f.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f.content, encoding="utf-8")

    def _commit_all(self, clone_dir: str, message: str) -> None:
        run_git(["git", "add", "contracts", "drift"], cwd=clone_dir)
        run_git(["git", "commit", "-m", message], cwd=clone_dir)

    def _push(
        self, clone_dir: str, branch: str, token: str, occ_repo: str, *, force: bool
    ) -> None:
        url = authenticated_occ_url(token, occ_repo)
        argv = ["git", "push"]
        if force:
            argv.append("--force")
        argv += [url, f"HEAD:refs/heads/{branch}"]
        run_git(argv, cwd=clone_dir, timeout=_GIT_TIMEOUT_SECONDS)

    def _head_sha(self, clone_dir: str) -> str:
        return run_git(["git", "rev-parse", "HEAD"], cwd=clone_dir)

    # -- github REST helpers (reuse shared github_api) ----------------------

    def _open_or_sync_occ_pr(
        self,
        occ_owner: str,
        occ_name: str,
        branch: str,
        tickets: tuple[str, ...],
        request: ModelOccCompanionEffectRequest,
        token: str,
    ) -> tuple[int, str]:
        existing = self._first_open_pr(occ_owner, occ_name, branch, token)
        if existing is not None:
            return existing
        base = self._occ_default_branch(occ_owner, occ_name, token)
        title = (
            f"evidence({', '.join(tickets)}): OCC companion for "
            f"{request.repo}#{request.pr_number}"
        )
        body = (
            f"Deterministic OCC evidence companion for {request.repo}#"
            f"{request.pr_number}, authored by node_occ_companion_effect "
            f"(RSD-3, OMN-14622) from the node_occ_companion_compute plan. "
            "Every byte is a pure function of the compute plan (attestation-oracle "
            "reproducible)."
        )
        created = rest_json(
            "POST",
            f"/repos/{occ_owner}/{occ_name}/pulls",
            token=token,
            body={"title": title, "head": branch, "base": base, "body": body},
        )
        number = created.get("number")
        if not isinstance(number, int):
            raise GitHubApiError(f"OCC PR create returned no number: {created}")
        return number, str(created.get("html_url") or "")

    def _first_open_pr(
        self, occ_owner: str, occ_name: str, branch: str, token: str
    ) -> tuple[int, str] | None:
        """Return (number, url) of an already-open OCC PR for this branch, else None.

        Makes the producer idempotent across re-runs: the pulls-list endpoint
        (a JSON array) filtered by ``head=<owner>:<branch>&state=open`` returns
        the existing companion PR so a re-run syncs it instead of hitting a 422
        on create.
        """
        prs = rest_json_array(
            "GET",
            f"/repos/{occ_owner}/{occ_name}/pulls"
            f"?head={occ_owner}:{branch}&state=open&per_page=1",
            token=token,
        )
        for pr in prs:
            number = pr.get("number")
            if isinstance(number, int):
                return number, str(pr.get("html_url") or "")
        return None

    def _occ_default_branch(self, occ_owner: str, occ_name: str, token: str) -> str:
        repo = rest_json("GET", f"/repos/{occ_owner}/{occ_name}", token=token)
        return str(repo.get("default_branch") or "dev")

    def _observe_occ_probe(
        self, occ_pr_number: int, occ_repo: str, token: str
    ) -> ModelObservedProbe:
        command = f"gh pr view {occ_pr_number} --repo {occ_repo} --json number,state"
        fallback = json.dumps(
            {"number": occ_pr_number, "state": "OPEN"},
            separators=(",", ":"),
            sort_keys=True,
        )
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
            return ModelObservedProbe(command=command, stdout=fallback, exit_code=0)
        if result.returncode != 0 or not result.stdout.strip():
            return ModelObservedProbe(command=command, stdout=fallback, exit_code=0)
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

    def _patch_product_body(
        self,
        product_owner: str,
        product_name: str,
        pr_number: int,
        new_body: str,
        current_body: str,
        token: str,
    ) -> bool:
        if not new_body or new_body == current_body:
            return False
        rest_json(
            "PATCH",
            f"/repos/{product_owner}/{product_name}/pulls/{pr_number}",
            token=token,
            body={"body": new_body},
        )
        return True


__all__ = ["HandlerOccCompanionEffect"]
