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

SELF-BIND (OMN-15323). A well-shaped PR is not a mergeable one: ``occ-preflight
/ eligibility`` also demands a PASS receipt that is DECLARED in the cited
ticket's contract and BOUND to this PR. This handler therefore writes THREE
files, not one, in two commits on the same branch:

  1. commit A — ``drift/occ_observations/**`` : the record (unchanged).
  2. commit B — ``contracts/<ticket>.yaml``   : append one dod_evidence item.
                ``drift/dod_receipts/<ticket>/<item>/command.yaml`` : the PASS
                receipt, bound to commit A's sha.

Both commits are pushed BEFORE the PR is opened, so eligibility sees complete
evidence on its first run. The append-only guard is re-asserted before each
push against an EXACT allowlist of those three paths, so the OCC write token
still cannot be steered anywhere else (``grants/**``, ``allowlists/**`` and
every other path stay unreachable — proven by
``test_append_only_guard_omn_14888`` / ``test_self_bind_eligibility_omn_15323``).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.events.occ_observation_store import (
    declares_dod_evidence_id,
    insert_dod_evidence_item,
    occ_observation_contract_relpath,
    occ_observation_evidence_item_id,
    occ_observation_receipt_relpath,
    occ_observation_record_relpath,
    occ_observation_self_bind_check_value,
    render_occ_observation_dod_evidence_item,
    render_occ_observation_record,
    render_occ_observation_self_bind_receipt,
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

#: Every branch this producer ever pushes starts with this literal prefix
#: (see ``_branch_name``). OMN-15777 widens the reuse check from "same
#: identity" to "ANY branch with this prefix" so at most one observation PR is
#: ever open at a time — there is nothing left for a sibling to conflict with.
_OBSERVATION_BRANCH_PREFIX = "auto/occ-observation-"


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


def render_occ_observation_self_bind_commit_subject(
    evidence_ticket: str, evidence_item_id: str
) -> str:
    """Commit subject for the self-bind commit (OMN-15323).

    Carries the ticket for the same reason the record commit does: every commit
    text on the branch is a binding axis, so a squash or a dropped commit cannot
    silently unbind the PR.
    """
    return f"evidence({evidence_ticket}): OCC observation self-bind {evidence_item_id}"


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

        # Cross-identity conflict-factory guard (OMN-15777): every observation
        # PR's self-bind commit appends to the SAME shared
        # contracts/OMN-14888.yaml tail, so any two simultaneously-open
        # observation PRs are structural conflicts with each other, even
        # though each is individually well-formed. If ANY observation PR is
        # already open — regardless of identity — this run's commits land as
        # two MORE commits on THAT branch instead of a fresh branch/PR, so at
        # most one observation PR is ever open at a time. The evidence-item id
        # this run declares is keyed on its own workflow-run-id/attempt
        # (occ_observation_evidence_item_id), so two different identities
        # appending to the same branch never collide on an id, and each
        # append starts from the branch's CURRENT tip (a fresh clone of it),
        # so a later append's insertion point is always past an earlier one's
        # — no merge, no conflict, even across many sequential appends.
        reuse_target = self._find_reusable_observation_pr(occ_owner, occ_name, token)

        with tempfile.TemporaryDirectory(prefix="occ-observation-effect-") as tmp:
            clone_dir = str(Path(tmp) / "onex_change_control")
            default_branch = ""
            if reuse_target is not None:
                target_branch, _reuse_number, _reuse_url = reuse_target
                self._clone_branch(clone_dir, token, request.occ_repo, target_branch)
            else:
                target_branch = branch
                default_branch = self._clone_default(clone_dir, token, request.occ_repo)

            if (Path(clone_dir) / relpath).exists():
                # Idempotent no-op: this exact raw attempt is already durable.
                return ModelOccObservationEffectResult(
                    mode="mutate",
                    action=f"no-op: {relpath} already present on {target_branch}",
                    relpath=relpath,
                    already_present=True,
                    occ_branch=target_branch,
                )

            base_sha = self._head_sha(clone_dir)
            if reuse_target is None:
                run_git(["git", "checkout", "-B", target_branch], cwd=clone_dir)
            self._write_file(clone_dir, relpath, content)
            self._commit_paths(
                clone_dir,
                [relpath],
                render_occ_observation_commit_subject(request.evidence_ticket, relpath),
            )
            record_commit_sha = self._head_sha(clone_dir)
            self._assert_append_only(clone_dir, base_sha, {relpath})
            self._push(clone_dir, target_branch, token, request.occ_repo)

            # OMN-15323: the record alone can never merge — eligibility needs a
            # DECLARED, PR-BOUND PASS receipt. Authored as a second commit on
            # the same branch so it can cite the record commit's sha, and
            # pushed BEFORE the PR is opened so occ-preflight's first run
            # already sees complete evidence.
            self_bind_paths = self._author_self_bind(
                clone_dir=clone_dir,
                request=request,
                relpath=relpath,
                record_commit_sha=record_commit_sha,
                branch=target_branch,
                token=token,
                occ_owner=occ_owner,
                occ_name=occ_name,
            )
            self._assert_append_only(clone_dir, base_sha, {relpath, *self_bind_paths})
            self._push(clone_dir, target_branch, token, request.occ_repo)

        if reuse_target is not None:
            _, reuse_number, reuse_url = reuse_target
            return ModelOccObservationEffectResult(
                mode="mutate",
                action=(
                    f"appended {relpath} onto existing observation PR "
                    f"(OCC#{reuse_number}); no second PR opened"
                ),
                relpath=relpath,
                appended_to_existing_pr=True,
                occ_branch=target_branch,
                occ_pr_number=reuse_number,
                occ_pr_url=reuse_url,
            )

        occ_pr_number, occ_pr_url = self._open_or_sync_occ_pr(
            occ_owner,
            occ_name,
            target_branch,
            default_branch,
            relpath,
            token,
            request.evidence_ticket,
        )
        return ModelOccObservationEffectResult(
            mode="mutate",
            action=f"appended {relpath} on OCC#{occ_pr_number}",
            relpath=relpath,
            occ_branch=target_branch,
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

    def _clone_branch(
        self, clone_dir: str, token: str, occ_repo: str, branch: str
    ) -> None:
        """Clone directly onto an EXISTING branch (OMN-15777 append-reuse path).

        Single-branch, depth=1 — same cost profile as ``_clone_default`` — but
        checks out ``branch`` instead of the repo's default branch, so this
        run's commits start from whatever that branch's tip already carries
        (including any prior observation's append). That is what makes
        sequential appends onto an already-advanced branch conflict-free: each
        append clones the LIVE tip and inserts past it, never a stale base.
        """
        url = authenticated_occ_url(token, occ_repo)
        run_git(
            [
                "git",
                "clone",
                "--depth=1",
                "--single-branch",
                "--branch",
                branch,
                url,
                clone_dir,
            ],
            cwd=str(Path(clone_dir).parent),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        run_git(["git", "config", "user.name", _GIT_AUTHOR_NAME], cwd=clone_dir)
        run_git(["git", "config", "user.email", _GIT_AUTHOR_EMAIL], cwd=clone_dir)

    def _write_file(self, clone_dir: str, relpath: str, content: str) -> None:
        path = Path(clone_dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _commit_paths(self, clone_dir: str, relpaths: list[str], message: str) -> None:
        """Stage EXACTLY ``relpaths`` and commit them.

        Path-scoped rather than ``git add <dir>``: this run now writes into two
        top-level trees (``drift/`` and ``contracts/``), and a directory-wide add
        would stage anything else that happened to be dirty in the clone. The
        append-only assertion still backstops the committed tree.
        """
        run_git(["git", "add", "--", *relpaths], cwd=clone_dir)
        run_git(["git", "commit", "-m", message], cwd=clone_dir)

    # -- self-bind evidence (OMN-15323) -------------------------------------

    def _author_self_bind(
        self,
        *,
        clone_dir: str,
        request: ModelOccObservationEffectRequest,
        relpath: str,
        record_commit_sha: str,
        branch: str,
        token: str,
        occ_owner: str,
        occ_name: str,
    ) -> tuple[str, str]:
        """Author + commit the contract entry and the PASS receipt.

        Returns the two repo-relative paths touched, for the append-only
        allowlist. Raises (fail LOUD) on any inconsistency: a PR carrying the
        record without its binding evidence is precisely the state OMN-15323
        exists to remove, so a partial write must never be reported as success.
        """
        ticket = request.evidence_ticket
        evidence_item_id = occ_observation_evidence_item_id(request.record)
        contract_relpath = occ_observation_contract_relpath(ticket)
        receipt_relpath = occ_observation_receipt_relpath(ticket, evidence_item_id)

        contract_path = Path(clone_dir) / contract_relpath
        if not contract_path.is_file():
            raise RuntimeError(
                f"OCC observation self-bind cannot proceed: {contract_relpath} is "
                f"absent from {request.occ_repo}. The evidence ticket must be one "
                "whose contract already exists on the OCC default branch — that is "
                "why this producer binds to the observation-store ticket and not "
                "to the triggering product PR's ticket (OMN-15323)."
            )

        # The probe is REAL: confirm with the OCC remote that the record commit
        # this receipt binds to actually landed. `gh api ... --jq .sha` is the
        # replayable form of this exact REST read; the node has no gh binary.
        check_value = occ_observation_self_bind_check_value(
            occ_repo=request.occ_repo, record_commit_sha=record_commit_sha
        )
        probe_stdout = self._probe_commit_sha(
            occ_owner, occ_name, record_commit_sha, token
        )

        contract_text = contract_path.read_text(encoding="utf-8")
        if not declares_dod_evidence_id(contract_text, evidence_item_id):
            contract_text = insert_dod_evidence_item(
                contract_text,
                render_occ_observation_dod_evidence_item(
                    record=request.record,
                    evidence_item_id=evidence_item_id,
                    check_value=check_value,
                ),
            )
            contract_path.write_text(contract_text, encoding="utf-8")

        # Hash the entry AFTER the append, from the parsed contract — the same
        # canonical hasher occ-preflight and OCC's honesty gate recompute.
        contract_entry_sha256 = compute_contract_entry_sha256(
            yaml.safe_load(contract_text), evidence_item_id
        )
        self._write_file(
            clone_dir,
            receipt_relpath,
            render_occ_observation_self_bind_receipt(
                evidence_ticket=ticket,
                evidence_item_id=evidence_item_id,
                check_value=check_value,
                contract_entry_sha256=contract_entry_sha256,
                run_timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                record_commit_sha=record_commit_sha,
                probe_stdout=probe_stdout,
                branch=branch,
                occ_repo=request.occ_repo,
                runner=request.runner,
                verifier=request.verifier,
            ),
        )
        self._commit_paths(
            clone_dir,
            [contract_relpath, receipt_relpath],
            render_occ_observation_self_bind_commit_subject(ticket, evidence_item_id),
        )
        logger.info(
            "occ_observation_effect: self-bind authored ticket=%s item=%s "
            "record_commit=%s relpath=%s",
            ticket,
            evidence_item_id,
            record_commit_sha,
            relpath,
        )
        return contract_relpath, receipt_relpath

    def _probe_commit_sha(
        self, occ_owner: str, occ_name: str, commit_sha: str, token: str
    ) -> str:
        """Read back the pushed record commit from the OCC remote (fail-closed).

        The receipt's ``probe_stdout`` must be the captured output of a probe
        that really ran — an executable check_type with empty stdout is
        indistinguishable from "never ran" and ModelDodReceipt rejects it.
        """
        payload = rest_json(
            "GET",
            f"/repos/{occ_owner}/{occ_name}/commits/{commit_sha}",
            token=token,
        )
        observed = payload.get("sha")
        if not isinstance(observed, str) or observed.lower() != commit_sha.lower():
            raise GitHubApiError(
                "OCC observation self-bind probe failed: "
                f"/repos/{occ_owner}/{occ_name}/commits/{commit_sha} returned "
                f"sha={observed!r}; the record commit is not readable on the remote, "
                "so no honest receipt can bind to it."
            )
        return observed

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
        (OMN-14741 F-01) — same diff-based guard.

        OMN-15323 widened the allowlist from one path to three (record, the
        cited contract, that contract's receipt) but NOT the mechanism: the
        allowlist is still an exact set computed from this run's own inputs, so
        every other path in ``onex_change_control`` — ``grants/**``,
        ``allowlists/**``, another ticket's contract or receipts — remains
        unreachable, and any deletion is still rejected outright. It is called
        once per commit, before each push, so nothing unverified is ever pushed.
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

    #: Hard cap on pages fetched by ``_iter_open_prs`` (100/page -> 1000 PRs).
    #: A bound, not an unlimited loop: real observation-PR volume never
    #: approaches this, and a misbehaving API (e.g. never returning a
    #: short/empty page) must still terminate rather than loop forever.
    _MAX_OPEN_PR_PAGES = 10

    def _iter_open_prs(
        self, occ_owner: str, occ_name: str, token: str, direction: str
    ) -> Generator[dict[str, Any], None, None]:
        """Yield every OPEN pull request, paginating past the first 100 (OMN-15777).

        A single ``per_page=100`` call missed a matching PR whenever more than
        100 unrelated PRs were open — the selector would then either suppress
        nothing it should have, or (for the reuse selector) open a SECOND
        conflicting PR instead of finding the one already open. Pages are
        fetched until a short page (fewer than 100 results) ends the list, or
        ``_MAX_OPEN_PR_PAGES`` is hit.
        """
        for page in range(1, self._MAX_OPEN_PR_PAGES + 1):
            prs = rest_json_array(
                "GET",
                f"/repos/{occ_owner}/{occ_name}/pulls"
                f"?state=open&sort=created&direction={direction}"
                f"&per_page=100&page={page}",
                token=token,
            )
            yield from prs
            if len(prs) < 100:
                return

    @staticmethod
    def _matching_observation_pr(
        pr: dict[str, Any], occ_owner: str, occ_name: str, branch_prefix: str
    ) -> tuple[str, int, str] | None:
        """A PR matches only if its head branch is IN the OCC repo itself.

        ``head.ref`` is scoped to whatever repository opened the PR — a fork
        can name its branch ``auto/occ-observation-*`` too. Without this check
        a selector could return a fork branch name, which ``_clone_branch``
        would then try to clone FROM the OCC repo (not the fork) and fail,
        turning an unrelated fork PR into a denial-of-service on every
        subsequent observation write. Requiring ``head.repo.full_name`` to be
        this run's own ``{occ_owner}/{occ_name}`` closes that.
        """
        head = pr.get("head")
        if not isinstance(head, dict):
            return None
        ref = head.get("ref")
        repo = head.get("repo")
        full_name = repo.get("full_name") if isinstance(repo, dict) else None
        number = pr.get("number")
        if (
            full_name == f"{occ_owner}/{occ_name}"
            and isinstance(ref, str)
            and ref.startswith(branch_prefix)
            and isinstance(number, int)
        ):
            return ref, number, str(pr.get("html_url") or "")
        return None

    def _open_pr_for_identity(
        self, occ_owner: str, occ_name: str, branch_prefix: str, token: str
    ) -> tuple[int, str] | None:
        """First OPEN observation PR whose branch shares this identity prefix.

        Scanned most-recently-created first: duplicate attempts for one head
        sha arrive seconds apart, so a live sibling is always among the
        newest open PRs, and this ordering finds it fastest. Still a
        best-effort de-duplication, never a correctness barrier — missing one
        (e.g. past ``_MAX_OPEN_PR_PAGES``) costs a redundant PR, not a lost
        observation.
        """
        for pr in self._iter_open_prs(occ_owner, occ_name, token, "desc"):
            match = self._matching_observation_pr(
                pr, occ_owner, occ_name, branch_prefix
            )
            if match is not None:
                _ref, number, url = match
                return number, url
        return None

    def _find_reusable_observation_pr(
        self, occ_owner: str, occ_name: str, token: str
    ) -> tuple[str, int, str] | None:
        """First OPEN observation PR, of ANY identity (OMN-15777).

        Widened sibling of ``_open_pr_for_identity``: that method only ever
        matches the SAME identity (same product repo/PR/head_sha/policy
        prefix), which suppresses a duplicate PR for a re-fired event but does
        nothing for two genuinely different observations. Every observation
        PR's self-bind commit appends to the identical
        ``contracts/OMN-14888.yaml`` tail, so any two simultaneously open
        observation PRs are structural conflicts with each other regardless of
        identity. Returning the first (oldest) open match gives every
        concurrent caller the SAME reuse target, converging on one branch
        instead of racing to create several.

        Callers must have already ruled out a same-identity match — this
        method does not distinguish identity at all, by design.
        """
        for pr in self._iter_open_prs(occ_owner, occ_name, token, "asc"):
            match = self._matching_observation_pr(
                pr, occ_owner, occ_name, _OBSERVATION_BRANCH_PREFIX
            )
            if match is not None:
                return match
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
    "render_occ_observation_self_bind_commit_subject",
]
