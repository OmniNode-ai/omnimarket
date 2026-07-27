# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccObservationEffect — durable append-only OCC observation write (OMN-14888).

Appends ONE :class:`~omnimarket.events.occ_observation_record.ModelOccObservationRecord`
as a net-new file into ``onex_change_control`` at its deterministic path
(:func:`occ_observation_record_relpath`), reusing the SAME git transport
(``occ_git_transport``) as ``node_occ_companion_effect`` — no second cross-repo
write surface (net-negative-surface). The append-only guard is enforced two
ways, matching the F-01 pattern already proven for OCC companions:

  1. **Idempotent no-op**: if the exact deterministic path already exists on
     the OCC default branch (this attempt was already ingested), the write is
     skipped entirely — a re-ingestion of the identical raw attempt is a no-op,
     never a duplicate row or an error.
  2. **Fail-closed diff assertion**: the committed tree is diffed against the
     clone base and REJECTED if it touches anything other than exactly the one
     net-new path this run intends to add (ported from
     ``HandlerOccCompanionEffect._assert_append_only``, OMN-14741 F-01).

``mode="dry_run"`` (the default) renders the path + content and reports them
without ANY GitHub mutation — same shadow-wired convention as
``node_occ_companion_effect`` before it goes live. See the OMN-14888 ticket for
the two documented activation routes (bus + sibling adapter vs. a new
write-scoped GHA secret) — neither is wired here.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Literal

from omnimarket.events.occ_observation_store import (
    occ_observation_record_relpath,
    render_occ_observation_record,
)
from omnimarket.github_api import GitHubApiError, rest_json, rest_json_array, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_result import (
    ModelOccObservationEffectResult,
)
from omnimarket.occ_git_transport import authenticated_occ_url, run_git

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_GIT_TIMEOUT_SECONDS = 120.0
_GIT_AUTHOR_NAME = "node-occ-observation-effect"
_GIT_AUTHOR_EMAIL = "occ-observation-effect@omninode.ai"


def _resolve_github_token() -> str:
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


def _branch_name(relpath: str) -> str:
    """Deterministic branch name derived 1:1 from the record's own path.

    Two ingestions of the identical raw attempt always resolve the SAME
    branch name (idempotent), and two different attempts never collide.
    """
    stem = relpath.replace("/", "-").replace(".", "-").replace("_", "-").lower()
    return f"auto/occ-observation-{stem}"[:200]


class HandlerOccObservationEffect:
    """EFFECT handler: append one durable OCC observation record."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self,
        request: ModelOccObservationEffectRequest,
    ) -> ModelOccObservationEffectResult:
        relpath = occ_observation_record_relpath(request.record)
        logger.info(
            "occ_observation_effect: relpath=%s mode=%s correlation_id=%s",
            relpath,
            request.mode,
            request.correlation_id,
        )
        branch = _branch_name(relpath)

        if request.mode == "dry_run":
            return ModelOccObservationEffectResult(
                mode="dry_run",
                action=f"dry_run: would append {relpath} (no GitHub mutation)",
                relpath=relpath,
                occ_branch=branch,
            )

        return await asyncio.to_thread(self._write_sync, request, relpath, branch)

    # -- write (mutate) -----------------------------------------------------

    def _write_sync(
        self,
        request: ModelOccObservationEffectRequest,
        relpath: str,
        branch: str,
    ) -> ModelOccObservationEffectResult:
        token = _resolve_github_token()
        occ_owner, occ_name = split_repo(request.occ_repo)
        content = render_occ_observation_record(request.record)

        with tempfile.TemporaryDirectory(prefix="occ-observation-effect-") as tmp:
            clone_dir = str(Path(tmp) / "onex_change_control")
            default_branch = self._clone_default(clone_dir, token, request.occ_repo)

            if (Path(clone_dir) / relpath).exists():
                # Idempotent no-op: this exact raw attempt is already durable.
                return ModelOccObservationEffectResult(
                    mode="mutate",
                    action=f"no-op: {relpath} already present on {default_branch}",
                    relpath=relpath,
                    already_present=True,
                    occ_branch=branch,
                )

            base_sha = self._head_sha(clone_dir)
            run_git(["git", "checkout", "-B", branch], cwd=clone_dir)
            self._write_file(clone_dir, relpath, content)
            self._commit_all(clone_dir, f"evidence: OCC observation append {relpath}")
            self._assert_append_only(clone_dir, base_sha, {relpath})
            self._push(clone_dir, branch, token, request.occ_repo)

        occ_pr_number, occ_pr_url = self._open_or_sync_occ_pr(
            occ_owner, occ_name, branch, default_branch, relpath, token
        )
        return ModelOccObservationEffectResult(
            mode="mutate",
            action=f"appended {relpath} on OCC#{occ_pr_number}",
            relpath=relpath,
            occ_branch=branch,
            occ_pr_number=occ_pr_number,
            occ_pr_url=occ_pr_url,
        )

    # -- git helpers (reuse shared occ_git_transport) -----------------------

    def _clone_default(self, clone_dir: str, token: str, occ_repo: str) -> str:
        url = authenticated_occ_url(token, occ_repo)
        run_git(
            ["git", "clone", "--depth=1", url, clone_dir],
            cwd=str(Path(clone_dir).parent),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        run_git(["git", "config", "user.name", _GIT_AUTHOR_NAME], cwd=clone_dir)
        run_git(["git", "config", "user.email", _GIT_AUTHOR_EMAIL], cwd=clone_dir)
        return run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone_dir)

    def _write_file(self, clone_dir: str, relpath: str, content: str) -> None:
        path = Path(clone_dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _commit_all(self, clone_dir: str, message: str) -> None:
        run_git(["git", "add", "drift"], cwd=clone_dir)
        run_git(["git", "commit", "-m", message], cwd=clone_dir)

    def _push(self, clone_dir: str, branch: str, token: str, occ_repo: str) -> None:
        url = authenticated_occ_url(token, occ_repo)
        run_git(
            ["git", "push", url, f"HEAD:refs/heads/{branch}"],
            cwd=clone_dir,
            timeout=_GIT_TIMEOUT_SECONDS,
        )

    def _head_sha(self, clone_dir: str) -> str:
        return run_git(["git", "rev-parse", "HEAD"], cwd=clone_dir)

    def _assert_append_only(
        self, clone_dir: str, base_sha: str, allowed_paths: set[str]
    ) -> None:
        """Fail CLOSED if the committed tree touched anything unexpected (F-01).

        Ported from ``HandlerOccCompanionEffect._assert_append_only``
        (OMN-14741 F-01) — same diff-based guard, applied to this node's
        single-file write instead of a multi-file companion plan.
        """
        diff = run_git(
            ["git", "diff", "--no-renames", "--name-status", base_sha, "HEAD"],
            cwd=clone_dir,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        violations: list[str] = []
        for raw in diff.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            path = parts[-1]
            if status.startswith("D"):
                violations.append(f"deletes {path}")
            elif path not in allowed_paths:
                violations.append(f"{status} {path}")
        if violations:
            raise RuntimeError(
                "OCC observation append-only violation: the generated tree "
                "changed files outside this run's net-new path: "
                + "; ".join(sorted(violations))
                + ". Allowed: "
                + ", ".join(sorted(allowed_paths))
            )

    # -- github REST helpers (reuse shared github_api) -----------------------

    def _open_or_sync_occ_pr(
        self,
        occ_owner: str,
        occ_name: str,
        branch: str,
        base: str,
        relpath: str,
        token: str,
    ) -> tuple[int, str]:
        existing = self._first_open_pr(occ_owner, occ_name, branch, token)
        if existing is not None:
            return existing
        title = f"evidence: OCC observation append ({relpath.rsplit('/', 1)[-1]})"
        body = (
            f"Deterministic, append-only OCC observation record authored by "
            f"node_occ_observation_effect (OMN-14888). Adds exactly one net-new "
            f"file: `{relpath}`."
        )
        created = rest_json(
            "POST",
            f"/repos/{occ_owner}/{occ_name}/pulls",
            token=token,
            body={"title": title, "head": branch, "base": base, "body": body},
        )
        number = created.get("number")
        if not isinstance(number, int):
            raise GitHubApiError(
                f"OCC observation PR create returned no number: {created}"
            )
        return number, str(created.get("html_url") or "")

    def _first_open_pr(
        self, occ_owner: str, occ_name: str, branch: str, token: str
    ) -> tuple[int, str] | None:
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


__all__ = ["HandlerOccObservationEffect"]
