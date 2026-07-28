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


#: Stem of the ``__run`` filename separator after ``_branch_name``'s character
#: substitution. Everything left of it identifies the OBSERVATION (product repo,
#: PR, head sha, policy version); everything right of it identifies the ATTEMPT
#: (workflow run id + attempt).
_RUN_SEGMENT_MARKER = "--run"


def _branch_name(relpath: str) -> str:
    """Deterministic branch name derived 1:1 from the record's own path.

    Two ingestions of the identical raw attempt always resolve the SAME
    branch name (idempotent), and two different attempts never collide.
    """
    stem = relpath.replace("/", "-").replace(".", "-").replace("_", "-").lower()
    return f"auto/occ-observation-{stem}"[:200]


def _identity_branch_prefix(relpath: str) -> str:
    """The branch-name prefix shared by every ATTEMPT of the same observation.

    ``occ_observation_record_relpath`` deliberately encodes the workflow run id
    and attempt so each raw attempt is its own append-only file. That makes the
    branch name attempt-unique too, which is why an exact-branch lookup can
    never see a sibling attempt — the OMN-15300 duplicate-emission hole (three
    PRs for one head sha in 29s). Truncating at the run marker yields the
    identity key those siblings share.

    Derived from the already-built branch name (not re-derived from the path) so
    it is a genuine prefix even when ``_branch_name`` truncates at 200 chars. If
    the marker is absent the FULL branch is returned, degrading to exact-branch
    matching rather than over-matching unrelated PRs.
    """
    branch = _branch_name(relpath)
    marker_at = branch.rfind(_RUN_SEGMENT_MARKER)
    if marker_at == -1:
        return branch
    return branch[: marker_at + len(_RUN_SEGMENT_MARKER)]


def render_occ_observation_pr_title(evidence_ticket: str, relpath: str) -> str:
    """The ONE title shape this producer emits (OMN-15300).

    Carries exactly one ``OMN-`` token — the intended ticket. The rest of the
    title is a file name built from a hex sha, a policy version and digits, so
    no second ticket can ever leak into the title-scan fallback.
    """
    return (
        f"evidence({evidence_ticket}): OCC observation append "
        f"({relpath.rsplit('/', 1)[-1]})"
    )


def render_occ_observation_pr_body(evidence_ticket: str, relpath: str) -> str:
    """The ONE body shape this producer emits (OMN-15300).

    Two lines carry the whole gate contract, and each answers a different half
    of ``validate_occ_merge_eligibility``:

      * ``Implements <ticket>`` satisfies ``CLOSING_KEYWORD_PATTERN``, which
        ``_extract_ticket_ids`` checks in the BODY first and returns
        exclusively. Because the body always matches, the title is never
        scanned — so this producer is structurally outside the title-fallback
        over-demand described by OMN-15194 / OMN-14658. ``Implements`` is chosen
        over ``Closes``/``Fixes``/``Resolves`` deliberately: the extractor
        accepts all four, but only the other three are GitHub/Linear auto-close
        keywords, and an observation append must not close the ticket it
        reports on — these PRs merge many times a day.
      * ``Evidence-Ticket: <ticket>`` satisfies ``EVIDENCE_TICKET_PATTERN``,
        which is the body-side axis of ``_ticket_bound_to_pr``. Extraction alone
        is not enough: a cited ticket that is not also BOUND fails with
        ``pr_ticket_mismatch`` rather than ``missing_ticket``.
    """
    return (
        "Deterministic, append-only OCC observation record authored by "
        "node_occ_observation_effect. Adds exactly one net-new file: "
        f"`{relpath}`.\n"
        "\n"
        f"Implements {evidence_ticket}\n"
        f"Evidence-Ticket: {evidence_ticket}\n"
    )


def render_occ_observation_commit_subject(evidence_ticket: str, relpath: str) -> str:
    """Commit subject for the append (OMN-15300).

    Carries the ticket so ``pr_commit_texts`` is a THIRD independent binding
    axis alongside the title and the ``Evidence-Ticket`` line — the binding
    survives a later hand-edit of either.
    """
    return f"evidence({evidence_ticket}): OCC observation append {relpath}"


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

        # Duplicate-emission guard (OMN-15300) — runs BEFORE the clone so a
        # superseded attempt costs one API call and leaves no orphan branch.
        # The observe workflow fires once per `pull_request` event, so a single
        # head sha can produce several runs seconds apart; each gets its own
        # attempt-scoped path and branch, and the old exact-branch lookup could
        # not see its siblings. Every one of those PRs carries byte-identical
        # observation content and collapses to ONE qualifying observation in the
        # N=10 window, so the extra PRs add no signal and burn a full CI cycle
        # each.
        superseded = self._open_pr_for_identity(
            occ_owner, occ_name, _identity_branch_prefix(relpath), token
        )
        if superseded is not None:
            existing_number, existing_url = superseded
            return ModelOccObservationEffectResult(
                mode="mutate",
                action=(
                    f"no-op: an open observation PR for this identity already "
                    f"exists (OCC#{existing_number}); no second PR opened"
                ),
                relpath=relpath,
                superseded_by_open_pr=True,
                occ_branch=branch,
                occ_pr_number=existing_number,
                occ_pr_url=existing_url,
            )

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
            self._commit_all(
                clone_dir,
                render_occ_observation_commit_subject(request.evidence_ticket, relpath),
            )
            self._assert_append_only(clone_dir, base_sha, {relpath})
            self._push(clone_dir, branch, token, request.occ_repo)

        occ_pr_number, occ_pr_url = self._open_or_sync_occ_pr(
            occ_owner,
            occ_name,
            branch,
            default_branch,
            relpath,
            token,
            request.evidence_ticket,
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
        evidence_ticket: str,
    ) -> tuple[int, str]:
        existing = self._first_open_pr(occ_owner, occ_name, branch, token)
        if existing is not None:
            return existing
        title = render_occ_observation_pr_title(evidence_ticket, relpath)
        body = render_occ_observation_pr_body(evidence_ticket, relpath)
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

    def _open_pr_for_identity(
        self, occ_owner: str, occ_name: str, branch_prefix: str, token: str
    ) -> tuple[int, str] | None:
        """First OPEN observation PR whose branch shares this identity prefix.

        Scans the most recently created page of open PRs rather than paginating
        the whole list. That bound is deliberate and sufficient for the failure
        this closes: duplicate attempts for one head sha arrive seconds apart,
        so a live sibling is always among the newest open PRs. A sibling that
        has already aged out of that page is not suppressed — the guard is
        best-effort de-duplication, never a correctness barrier, and missing one
        costs a redundant PR, not a lost observation.
        """
        prs = rest_json_array(
            "GET",
            f"/repos/{occ_owner}/{occ_name}/pulls"
            "?state=open&sort=created&direction=desc&per_page=100",
            token=token,
        )
        for pr in prs:
            head = pr.get("head")
            ref = head.get("ref") if isinstance(head, dict) else None
            number = pr.get("number")
            if (
                isinstance(ref, str)
                and ref.startswith(branch_prefix)
                and isinstance(number, int)
            ):
                return number, str(pr.get("html_url") or "")
        return None

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


__all__ = [
    "HandlerOccObservationEffect",
    "render_occ_observation_commit_subject",
    "render_occ_observation_pr_body",
    "render_occ_observation_pr_title",
]
