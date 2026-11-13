# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_github_repo_write_effect (OMN-20912).

One typed write command in, one typed completed or failed result out. Every
GitHub call goes through the shared landing transport
(``omnimarket.github_landing``), the send path node_pr_landing_github_effect
uses; no ``gh`` subprocess.

Rules this handler enforces:

- **Mode from the contract.** ``write_config`` names the mode per repository
  and per owner; a repository row wins over its owner's row, and a repository
  named by neither takes the default (dry_run). dry_run returns the
  requests the write would send and sends nothing. A request may force
  dry_run; it can never force enforce.
- **Quota floor.** The landing effect's rule, read from this contract's
  ``quota_floor``: every response's ``x-ratelimit-*`` headers become a reading
  per resource, and a call whose resource's last reading is under the floor,
  inside that reading's window, is refused with ``quota_floor`` before it is
  sent.
- **Expected head.** pr_edit, pr_ready, pr_close and pr_review_post read the PR first and
  refuse with ``head_moved`` when its head is not the expected one; pr_create
  and workflow_dispatch check the branch tip when given an expected head, and
  pr_comment and pr_thread_reply check the PR's head when given one. No
  mutation follows.
- **Idempotency.** A retried command with the same idempotency key returns the
  first result marked ``replayed`` and sends nothing; a key reused with
  different request content is refused with ``idempotency_key_conflict``,
  naming the fields that differ, never their values. Only the call-context
  fields (correlation id, dry_run, the asking lane and run, the expected head,
  the lease, the claim and the claims directory) may differ between retries.
  Across restarts GitHub
  itself is the record: pr_create finds an open PR for the head, pr_comment
  finds a comment carrying the key, pr_thread_reply finds a review comment
  carrying the key, pr_review_post finds a review carrying the key,
  pr_ready and pr_close find the PR already
  in the asked-for state. workflow_dispatch has no GitHub-side record, so only
  the in-process record guards a retry. check_run_create likewise only has
  the in-process record; a restart can create another check run.
- **Allowed labels.** pr_label adds and removes labels on a pull request, pinned to
  the expected head like pr_edit. Every label must be in the ``allowed_labels`` set of
  the node's overlay (``overlay.py``); one that is not, or an overlay that cannot be
  read, is refused ``label_not_allowed`` before any GitHub call, in either mode. A label
  already in the asked-for state is not sent again and the result is ``replayed``.
- **Repository variables.** repo_variable_set and repo_variable_get read and write
  GitHub Actions repository variables, never secrets. The allowed names and the file
  each value is read from are overlay values (``repo_variables`` in ``overlay.py``);
  the value is never on the request. A name the overlay does not allow, a source
  that cannot be read (set only; get reports ``variable_in_sync`` as None), or a
  name or value carrying a refuse marker is refused before any GitHub call, in
  either mode, and only the /actions/variables endpoints are called.
- **Runners.** runner_list pages a self-hosted runner organization's registrations
  and answers with every one of them; runner_remove deletes one offline, idle
  runner by name. The organizations and runner names a lane may act on are only
  the overlay's ``runner_removals`` entries (``overlay.py``); anything else, an
  absent overlay or an absent ``runner_removals`` is refused ``runner_not_allowed``
  before any GitHub call, in either mode, and only the /actions/runners endpoints
  are called. A runner that is online or busy is refused ``runner_online`` with no
  DELETE; a name GitHub does not register answers completed ``replayed`` with
  ``runner_removed`` false. Like a variable, a runner is read back from GitHub on
  every call, so runner operations keep no idempotency record.
- **Claim.** pr_close is refused with ``claim_not_held`` unless the requesting
  lane and run hold a live claim on the PR in node_pr_claim_registry_effect.
  The claim is checked in dry_run too, since it needs no GitHub call.
- **Lease fence (OMN-21425).** A command carrying a lease is refused before any
  GitHub call, in either mode, unless the fence port answers ok: stale is
  lease_fence_stale, expired is lease_expired, unknown, a port error or no port
  is lease_unreadable. A lease whose expires_at is closer than the contract's
  descriptor.timeout_ms is refused lease_expired without asking. A command
  with no lease is not fenced.
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
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from pydantic import SecretStr

from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaHeadersMissingError,
    ModelGithubQuotaReading,
)
from omnimarket.github_api import GitHubApiError
from omnimarket.github_app_auth import (
    GitHubAppCredentialMissingError,
    GitHubAppIdentityError,
    mint_installation_token,
)
from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    convert_to_draft_request,
    create_issue_comment_request,
    create_pull_request_request,
    create_review_comment_reply_request,
    edit_pull_request_request,
    git_ref_request,
    issue_comments_request,
    mark_ready_request,
    next_page_request,
    open_pulls_for_head_request,
    pull_request_request,
    review_comments_request,
    split_repository,
    workflow_dispatch_request,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    GITHUB_GRAPHQL_PATH,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.handlers.github_quota_readings import GithubQuotaReadings
from omnimarket.handlers.handler_lane_write_identity import (
    ModelLaneWriteIdentity,
    assert_github_token_is_actor,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    pinned_overlay_root,
)
from omnimarket.inference.secret_store_resolver import (
    SecretStoreConfigurationError,
    resolve_api_key_async,
)
from omnimarket.models.model_github_quota_floor import ModelGithubQuotaFloor
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_dod_verify.services.contract_prerequisite import (
    M4_REPOSITORIES,
    cited_tickets,
    contract_prerequisite,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_pr_claim_lookup import (
    HandlerPrClaimLookup,
    carried_claim_refusal,
    registry_claim_refusal,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    MAX_VARIABLE_CHARS,
    EnumRepoWriteFenceStatus,
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
    ModelRepoWriteRunner,
)
from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_config import (
    ModelRepoWriteCredentialChoice,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    NODE_NAME,
    ModelRepoWriteOverlay,
    load_overlay,
)
from omnimarket.nodes.node_github_repo_write_effect.protocols import (
    ProtocolGithubRepoWriteTransport,
    ProtocolLeaseFence,
    ProtocolPrClaimLookup,
)
from omnimarket.nodes.node_github_repo_write_effect.services.pr_label import (
    add_labels_request,
    remove_label_request,
)
from omnimarket.nodes.node_github_repo_write_effect.services.pr_review_post import (
    create_pull_request_review_request,
    pull_request_reviews_request,
)
from omnimarket.nodes.node_github_repo_write_effect.services.pr_thread_resolve import (
    IDEMPOTENCY_MARKER_PREFIX,
    THREAD_SCAN_PAGES,
    dict_at,
    has_fix_reply,
    resolve_review_thread_request,
    thread_nodes,
    threads_page_request,
)
from omnimarket.nodes.node_github_repo_write_effect.services.repo_variable import (
    create_variable_request,
    get_variable_request,
    read_variable_source_value,
    update_variable_request,
    variable_digest,
)
from omnimarket.nodes.node_github_repo_write_effect.services.runner_registration import (
    delete_runner_request,
    list_runners_request,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"
_CLAIMS_DIR_ENV = "ONEX_PR_CLAIMS_DIR"
# How many completed writes the in-process idempotency record keeps.
_IDEMPOTENCY_RECORD_SIZE = 4096
# The request fields that say who is asking and under which guard-rails, not
# what is written: a retry may differ in them and still be the same write, so
# the idempotency record's per-field digests leave them out.
_CALL_CONTEXT_FIELDS: tuple[str, ...] = (
    "correlation_id",
    "dry_run",
    "requested_by_run",
    "requested_by_lane",
    "expected_head_sha",
    "lease",
    "claim",
    "claims_dir",
)
# Comment pages scanned for an idempotency marker (300 comments), for the
# conversation comments, review comments and reviews of the comment/review writes.
_MARKER_SCAN_PAGES = 3
# The per_page the runner list requests name; a shorter page ends the paging.
_RUNNERS_PAGE_SIZE = 100
# A planned node id in dry_run: the real one is read from the PR first.
_PLANNED_NODE_ID = "<node id read from the pull request>"

Result = ModelRepoWriteCompleted | ModelRepoWriteFailed


async def _credential_from_store(ref: str) -> SecretStr | None:
    """Absence permits the next choice; a broken resolver refuses safely."""
    try:
        return await resolve_api_key_async(ref, env_var_fallback=ref, required=False)
    except SecretStoreConfigurationError as exc:
        # This error names only variables and logical names, never a value
        # (OMN-16984), so its message is what tells the lane which bootstrap
        # variable its host lacks. Folding it into the generic text below left
        # a lane on the in-memory fallback with no way to see the host defect.
        raise GitHubAppCredentialMissingError(
            f"GitHub write: the secret store for declared ref {ref!r} cannot be "
            f"built on this host: {exc}"
        ) from None
    except Exception:
        # A store/SDK error may carry a secret. Do not expose its message or cause.
        raise GitHubAppCredentialMissingError(
            f"GitHub write: credential lookup failed for declared ref {ref!r}; "
            "check the secret resolver configuration and read permission"
        ) from None


def idempotency_marker(key: str) -> str:
    """The hidden line a comment or review carries so a retry can find it."""
    return f"{IDEMPOTENCY_MARKER_PREFIX}{key} -->"


def _request_digests(request: ModelRepoWriteRequest) -> dict[str, str]:
    """One digest per request field, naming the write, not how it was asked.

    The call-context fields are left out (see ``_CALL_CONTEXT_FIELDS``); a
    digest, never the value, is kept, because a body can be private.
    """
    fields = request.model_dump(mode="json", exclude=set(_CALL_CONTEXT_FIELDS))
    return {
        name: hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for name, value in fields.items()
    }


def _resource_of(request: ModelGithubHttpRequest) -> str:
    return "graphql" if request.path == GITHUB_GRAPHQL_PATH else "core"


def _str_at(body: dict[str, object] | None, *path: str) -> str | None:
    node: object = body
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) else None


def _int_at(body: dict[str, object] | None, key: str) -> int | None:
    value = (body or {}).get(key)
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class _VariablePlan:
    """The value a repo_variable write acts on, from the overlay's declared source.

    ``source_value`` is None when that file could not be read: repo_variable_get
    still reads GitHub and reports ``variable_in_sync`` as None, while
    repo_variable_set was refused before any call.
    """

    value: str
    source_value: str | None


class _RefusedError(Exception):
    """Internal: carries the typed failure out of a multi-request operation."""

    def __init__(self, failed: ModelRepoWriteFailed) -> None:
        super().__init__(failed.detail)
        self.failed = failed


class _Run:
    """One command's exchange: the requests it sent, their statuses, its quota."""

    def __init__(
        self,
        handler: HandlerGithubRepoWriteEffect,
        command: ModelRepoWriteRequest,
        transport: ProtocolGithubRepoWriteTransport,
    ) -> None:
        self._handler = handler
        self.command = command
        self._transport = transport
        self.sent: list[ModelGithubHttpRequest] = []
        self.statuses: list[int] = []
        self.quota: ModelGithubQuotaReading | None = None
        self.variable_plan: _VariablePlan | None = None

    def refused(
        self,
        reason: EnumRepoWriteRefusal,
        detail: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        quota: ModelGithubQuotaReading | None = None,
    ) -> _RefusedError:
        c = self.command
        return _RefusedError(
            ModelRepoWriteFailed(
                correlation_id=c.correlation_id,
                operation=c.operation,
                repo=c.repo,
                mode=EnumRepoWriteMode.ENFORCE,
                idempotency_key=c.idempotency_key,
                requested_by_lane=c.requested_by_lane,
                pr_number=c.pr_number,
                requests=tuple(self.sent),
                http_statuses=tuple(self.statuses),
                quota=quota if quota is not None else self.quota,
                reason=reason,
                detail=detail or reason.value,
                http_status=http_status,
                retry_after_seconds=retry_after_seconds,
            )
        )

    def completed(
        self,
        *,
        pr_number: int | None = None,
        replayed: bool = False,
        html_url: str | None = None,
        head_sha: str | None = None,
        comment_id: int | None = None,
        review_id: int | None = None,
        check_run_id: int | None = None,
        variable_name: str | None = None,
        variable_value: str | None = None,
        variable_digest: str | None = None,
        variable_in_sync: bool | None = None,
        runners: tuple[ModelRepoWriteRunner, ...] | None = None,
        runner_name: str | None = None,
        runner_removed: bool | None = None,
    ) -> ModelRepoWriteCompleted:
        c = self.command
        return ModelRepoWriteCompleted(
            correlation_id=c.correlation_id,
            operation=c.operation,
            repo=c.repo,
            mode=EnumRepoWriteMode.ENFORCE,
            idempotency_key=c.idempotency_key,
            requested_by_lane=c.requested_by_lane,
            pr_number=pr_number if pr_number is not None else c.pr_number,
            requests=tuple(self.sent),
            http_statuses=tuple(self.statuses),
            quota=self.quota,
            replayed=replayed,
            html_url=html_url,
            head_sha=head_sha,
            comment_id=comment_id,
            review_id=review_id,
            check_run_id=check_run_id,
            variable_name=variable_name,
            variable_value=variable_value,
            variable_digest=variable_digest,
            variable_in_sync=variable_in_sync,
            runners=runners,
            runner_name=runner_name,
            runner_removed=runner_removed,
        )

    async def send(
        self, request: ModelGithubHttpRequest, *, allow_not_found: bool = False
    ) -> ModelGithubHttpResponse:
        """Send one request; raise :class:`_RefusedError` with the typed failure."""
        resource = _resource_of(request)
        floor_reading = self._handler.floor_refusal(resource)
        if floor_reading is not None:
            raise self.refused(
                EnumRepoWriteRefusal.QUOTA_FLOOR,
                (
                    f"{resource} remaining {floor_reading.remaining} is under the "
                    f"contract floor until {floor_reading.reset}; no call made"
                ),
                quota=floor_reading,
            )
        try:
            response = await self._transport.send(request)
        except (GithubLandingTransportError, OSError) as exc:
            raise self.refused(EnumRepoWriteRefusal.TRANSPORT_ERROR, str(exc)) from None
        self.sent.append(request)
        self.statuses.append(response.status)
        try:
            reading = ModelGithubQuotaReading.from_response_headers(
                response.headers, identity=self._handler.identity
            )
        except ModelGithubQuotaHeadersMissingError:
            pass  # a response without quota headers keeps the last reading
        else:
            self.quota = reading
            self._handler.quota_readings.record(reading)
        errors = (response.body or {}).get("errors")
        if allow_not_found and response.status == 404:
            return response
        if 200 <= response.status < 300 and not errors:
            return response
        reason = (
            EnumRepoWriteRefusal.NOT_FOUND
            if response.status == 404
            else EnumRepoWriteRefusal.GITHUB_REFUSED
        )
        raise self.refused(
            reason,
            f"{request.method} {request.path}: {response.message()}",
            http_status=response.status,
            retry_after_seconds=response.retry_after_seconds(),
        )

    async def expect_pr_head(self) -> dict[str, object]:
        """Read the PR and refuse ``head_moved`` when its head is not the expected one."""
        c = self.command
        number = c.pr_number
        if number is None:  # defensive; the request validator enforces this
            raise ValueError("this operation requires pr_number")
        pr = (await self.send(pull_request_request(c.repo, number))).body or {}
        head = _str_at(pr, "head", "sha")
        if c.expected_head_sha is not None and head != c.expected_head_sha:
            raise self.refused(
                EnumRepoWriteRefusal.HEAD_MOVED,
                f"{c.repo}#{number} head is {head}, expected {c.expected_head_sha}; "
                "no mutation sent",
            )
        return pr

    async def expect_branch_tip(self, branch: str) -> None:
        """Refuse ``head_moved`` when ``branch``'s tip is not the expected head."""
        c = self.command
        if c.expected_head_sha is None:
            return
        ref = (await self.send(git_ref_request(c.repo, branch))).body or {}
        tip = _str_at(ref, "object", "sha")
        if tip != c.expected_head_sha:
            raise self.refused(
                EnumRepoWriteRefusal.HEAD_MOVED,
                f"{c.repo} {branch} tip is {tip}, expected {c.expected_head_sha}; "
                "no mutation sent",
            )


class HandlerGithubRepoWriteEffect:
    """EFFECT: run one PR authoring write against GitHub and report it typed.

    ``transport``, ``claim_lookup`` and ``fence_port`` are injected in tests
    (a recorded fake and stubs). Without a transport the handler builds the
    live urllib transport per call from the contract-declared ``GITHUB_TOKEN`` ref.
    """

    def __init__(
        self,
        transport: ProtocolGithubRepoWriteTransport | None = None,
        *,
        claim_lookup: ProtocolPrClaimLookup | None = None,
        fence_port: ProtocolLeaseFence | None = None,
        deadline_seconds: float | None = None,
        runtime_claims_dir: str | None = None,
        quota_floor: ModelGithubQuotaFloor | None = None,
        config: ModelRepoWriteConfig | None = None,
        clock: Callable[[], float] = time.time,
        quota_readings: GithubQuotaReadings | None = None,
        overlay: ModelRepoWriteOverlay | None = None,
        variable_source_root: Path | None = None,
    ) -> None:
        self._write_overlay = overlay
        self._variable_source_root = variable_source_root
        self._transport = transport
        self._fence = fence_port
        if deadline_seconds is None:
            contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
            deadline_seconds = contract["descriptor"]["timeout_ms"] / 1000
        self._deadline = deadline_seconds
        self._claims = claim_lookup or HandlerPrClaimLookup()
        # The registry this runtime keeps, which the effect can read. The empty
        # string reads none; None takes $ONEX_PR_CLAIMS_DIR, as the CLI names it.
        self._runtime_claims_dir = (
            runtime_claims_dir
            if runtime_claims_dir is not None
            else os.environ.get(_CLAIMS_DIR_ENV)
        )
        self._floor = quota_floor or ModelGithubQuotaFloor.from_contract(_CONTRACT_PATH)
        self._config = config or ModelRepoWriteConfig.from_contract(_CONTRACT_PATH)
        self._clock = clock
        self.identity = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
        self.quota_readings = (
            GithubQuotaReadings() if quota_readings is None else quota_readings
        )
        self._done: OrderedDict[
            tuple[str, str, str], tuple[dict[str, str], ModelRepoWriteCompleted]
        ] = OrderedDict()

    def _variable_plan(
        self, request: ModelRepoWriteRequest
    ) -> ModelRepoWriteFailed | _VariablePlan | None:
        """Plan a repo_variable_set/get write, refusing before any call when the
        overlay does not allow it.
        """
        if request.operation not in (
            EnumRepoWriteOperation.REPO_VARIABLE_SET,
            EnumRepoWriteOperation.REPO_VARIABLE_GET,
        ):
            return None
        is_set = request.operation is EnumRepoWriteOperation.REPO_VARIABLE_SET
        name = request.variable_name
        if name is None:  # defensive; the request validator enforces this
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED,
                "repo_variable_set/get requires variable_name; no call made",
            )
        try:
            if self._write_overlay is None:
                self._write_overlay = load_overlay()
        except NodeOverlayError as exc:
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED,
                f"no repo_variables overlay is configured: {exc}",
            )
        overlay = self._write_overlay.repo_variables
        if overlay is None:
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED,
                f"variable {name} on {request.repo} is not in the node's "
                "repo_variables overlay; no call made",
            )
        lowered_name = name.lower()
        for marker in overlay.refuse_name_markers:
            if marker.lower() in lowered_name:
                return self._preflight_refusal(
                    request,
                    EnumRepoWriteRefusal.SECRET_REFUSED,
                    f"variable name {name} carries a refuse marker; no call made",
                )
        entry = next(
            (
                e
                for e in overlay.entries
                if e.repository.lower() == request.repo.lower() and e.name == name
            ),
            None,
        )
        if entry is None:
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED,
                f"variable {name} on {request.repo} is not in the node's "
                "repo_variables overlay; no call made",
            )
        try:
            root = self._variable_source_root or pinned_overlay_root(NODE_NAME)
            source_value = read_variable_source_value(
                root,
                entry.source,
                fmt=entry.format,
                select=entry.select,
                name=entry.name,
                repository=entry.repository,
                max_chars=MAX_VARIABLE_CHARS,
            )
        except ValueError:
            # The declared source cannot be read: a set refuses before any
            # call, a get still reads GitHub and reports in_sync as None.
            if is_set:
                return self._preflight_refusal(
                    request,
                    EnumRepoWriteRefusal.VARIABLE_SOURCE_UNREADABLE,
                    f"the source for variable {name} on {request.repo} is "
                    "unreadable; no call made",
                )
            return _VariablePlan(value="", source_value=None)
        if not is_set:
            return _VariablePlan(value="", source_value=source_value)
        lowered_value = source_value.lower()
        for marker in overlay.refuse_value_markers:
            if marker.lower() in lowered_value:
                return self._preflight_refusal(
                    request,
                    EnumRepoWriteRefusal.SECRET_REFUSED,
                    f"the source value for variable {name} on {request.repo} "
                    "carries a refuse marker; no call made",
                )
        return _VariablePlan(value=source_value, source_value=source_value)

    def _runner_preflight(
        self, request: ModelRepoWriteRequest
    ) -> ModelRepoWriteFailed | None:
        """Refuse a runner write the overlay's ``runner_removals`` does not
        allow, before any call.

        The overlay is read the first time a runner operation needs it, so no
        other operation depends on it. The organization is the owner part of
        the request's repository: a runner_list is served when at least one
        entry names that organization (case-insensitively), a runner_remove
        only when (organization, runner_name) is an entry.
        """
        if request.operation not in (
            EnumRepoWriteOperation.RUNNER_LIST,
            EnumRepoWriteOperation.RUNNER_REMOVE,
        ):
            return None
        try:
            if self._write_overlay is None:
                self._write_overlay = load_overlay()
        except NodeOverlayError as exc:
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED,
                f"no runner overlay is configured: {exc}",
            )
        overlay = self._write_overlay.runner_removals
        if overlay is None:
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED,
                "the node's overlay declares no runner_removals; no call made",
            )
        organization, _ = split_repository(request.repo)
        lowered_organization = organization.lower()
        if request.operation is EnumRepoWriteOperation.RUNNER_LIST:
            if any(
                entry.organization.lower() == lowered_organization
                for entry in overlay.entries
            ):
                return None
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED,
                f"no runner_removals entry names organization {organization}; "
                "no call made",
            )
        name = request.runner_name
        if name is None:  # defensive; the request validator enforces this
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED,
                "runner_remove requires runner_name; no call made",
            )
        if any(
            entry.organization.lower() == lowered_organization and entry.name == name
            for entry in overlay.entries
        ):
            return None
        return self._preflight_refusal(
            request,
            EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED,
            f"runner {name} on organization {organization} is not in the "
            "node's runner_removals overlay; no call made",
        )

    def _label_refusal(
        self, request: ModelRepoWriteRequest
    ) -> ModelRepoWriteFailed | None:
        """Refuse a pr_label naming a label the overlay does not allow, before any call.

        The overlay is read the first time a pr_label needs it, so no other
        operation depends on it; one that cannot be read refuses the label
        write, never falls back to a value here.
        """
        if request.operation is not EnumRepoWriteOperation.PR_LABEL:
            return None
        try:
            if self._write_overlay is None:
                self._write_overlay = load_overlay()
        except NodeOverlayError as exc:
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.LABEL_NOT_ALLOWED,
                f"no allowed-label overlay is configured for pr_label: {exc}",
            )
        allowed = set(self._write_overlay.pr_label.allowed_labels)
        refused = sorted(
            {*(request.add_labels or ()), *(request.remove_labels or ())} - allowed
        )
        if not refused:
            return None
        return self._preflight_refusal(
            request,
            EnumRepoWriteRefusal.LABEL_NOT_ALLOWED,
            f"label(s) {', '.join(refused)} are not in the node's allowed pr_label "
            "set; no call made",
        )

    # --- quota ----------------------------------------------------------------

    def floor_refusal(self, resource: str) -> ModelGithubQuotaReading | None:
        """The last reading for ``resource`` when it is under the floor, in its window."""
        reading = self.quota_readings.last(resource)
        if reading is None or self._clock() >= reading.reset:
            return None
        if self._floor.refusal(reading) is None:
            return None
        return reading

    # --- entry point ----------------------------------------------------------

    async def handle(self, request: ModelRepoWriteRequest) -> Result:
        """Run one write; dry_run plans its requests and sends nothing."""
        key = (request.repo.lower(), request.operation.value, request.idempotency_key)
        digests = _request_digests(request)
        record = self._done.get(key)
        if record is not None:
            done_digests, done = record
            differing = sorted(
                name
                for name in done_digests.keys() | digests.keys()
                if done_digests.get(name) != digests.get(name)
            )
            if differing:
                return self._preflight_refusal(
                    request,
                    EnumRepoWriteRefusal.IDEMPOTENCY_KEY_CONFLICT,
                    f"idempotency key {request.idempotency_key} was already used "
                    f"for a {request.operation.value} on {request.repo} that "
                    f"differs in {', '.join(differing)}; the recorded write's "
                    "result is not replayed and no call is made",
                )
            return done.model_copy(
                update={"correlation_id": request.correlation_id, "replayed": True}
            )
        refused = await self._lease_refusal(request)
        if refused is not None:
            return refused
        refused = self._claim_refusal(request)
        if refused is not None:
            return refused
        refused = self._label_refusal(request)
        if refused is not None:
            return refused
        variable_plan = self._variable_plan(request)
        if isinstance(variable_plan, ModelRepoWriteFailed):
            return variable_plan
        runner_refusal = self._runner_preflight(request)
        if runner_refusal is not None:
            return runner_refusal
        mode = (
            EnumRepoWriteMode.DRY_RUN
            if request.dry_run
            else self._config.mode_for(request.repo)
        )
        if mode is EnumRepoWriteMode.DRY_RUN:
            return ModelRepoWriteCompleted(
                correlation_id=request.correlation_id,
                operation=request.operation,
                repo=request.repo,
                mode=mode,
                idempotency_key=request.idempotency_key,
                requested_by_lane=request.requested_by_lane,
                pr_number=request.pr_number,
                requests=planned_requests(request),
            )
        try:
            transport = self._transport or await self._live_transport(request.repo)
        except (GitHubAppCredentialMissingError, GitHubAppIdentityError) as exc:
            return self._preflight_refusal(
                request, EnumRepoWriteRefusal.CREDENTIAL_UNAVAILABLE, str(exc)
            )
        run = _Run(self, request, transport)
        run.variable_plan = variable_plan
        try:
            result: Result = await self._run(run)
        except _RefusedError as exc:
            result = exc.failed
        # A repository variable, and a runner, is read back from GitHub on
        # every call, so a repeat finds the answer already there without a
        # record: a record would hide a value or a registration that changed
        # since the first call.
        if isinstance(result, ModelRepoWriteCompleted) and request.operation not in (
            EnumRepoWriteOperation.REPO_VARIABLE_SET,
            EnumRepoWriteOperation.REPO_VARIABLE_GET,
            EnumRepoWriteOperation.RUNNER_LIST,
            EnumRepoWriteOperation.RUNNER_REMOVE,
        ):
            self._done[key] = (digests, result)
            while len(self._done) > _IDEMPOTENCY_RECORD_SIZE:
                self._done.popitem(last=False)
        _log.info(
            "repo-write %s %s lane=%s -> %s",
            request.operation.value,
            request.repo,
            request.requested_by_lane,
            getattr(result, "reason", result.outcome),
        )
        return result

    async def _lease_refusal(
        self, request: ModelRepoWriteRequest
    ) -> ModelRepoWriteFailed | None:
        lease = request.lease
        if lease is None:
            return None
        left = lease.expires_at.timestamp() - self._clock()
        if left < self._deadline:
            expires_at = lease.expires_at.astimezone(UTC)
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.LEASE_EXPIRED,
                f"lease {lease.kind} {lease.resource} fence {lease.fence} expires "
                f"at {expires_at:%Y-%m-%dT%H:%M:%SZ} (UTC), {left:.0f}s away, "
                f"inside the effect's {self._deadline:.0f}s deadline; no call made",
            )
        if self._fence is None:
            return self._preflight_refusal(
                request,
                EnumRepoWriteRefusal.LEASE_UNREADABLE,
                "no fence port is wired in this runtime; a leased write is "
                "refused, no call made",
            )
        try:
            answer = await self._fence.check_fence(
                kind=lease.kind, resource=lease.resource, fence=lease.fence
            )
        except Exception:
            _log.warning("repo-write fence port check failed; leased write refused")
            status = EnumRepoWriteFenceStatus.UNKNOWN
        else:
            try:
                status = EnumRepoWriteFenceStatus(str(answer))
            except ValueError:
                status = EnumRepoWriteFenceStatus.UNKNOWN
        if status is EnumRepoWriteFenceStatus.OK:
            return None
        if status is EnumRepoWriteFenceStatus.STALE:
            reason = EnumRepoWriteRefusal.LEASE_FENCE_STALE
            detail = (
                f"fence {lease.fence} on {lease.kind} {lease.resource} is stale: "
                "a newer grant holds the resource; no call made"
            )
        elif status is EnumRepoWriteFenceStatus.EXPIRED:
            reason = EnumRepoWriteRefusal.LEASE_EXPIRED
            detail = (
                f"no live grant holds {lease.kind} {lease.resource} "
                f"fence {lease.fence}; no call made"
            )
        else:
            reason = EnumRepoWriteRefusal.LEASE_UNREADABLE
            detail = (
                f"the fence port could not read {lease.kind} {lease.resource} "
                f"fence {lease.fence}; no call made"
            )
        return self._preflight_refusal(request, reason, detail)

    def _claim_refusal(
        self, request: ModelRepoWriteRequest
    ) -> ModelRepoWriteFailed | None:
        if request.operation is not EnumRepoWriteOperation.PR_CLOSE:
            return None
        now = datetime.fromtimestamp(self._clock(), tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        detail = self._claim_detail(request, now)
        if detail is None:
            return None
        return self._preflight_refusal(
            request, EnumRepoWriteRefusal.CLAIM_NOT_HELD, detail
        )

    def _preflight_refusal(
        self,
        request: ModelRepoWriteRequest,
        reason: EnumRepoWriteRefusal,
        detail: str,
    ) -> ModelRepoWriteFailed:
        return ModelRepoWriteFailed(
            correlation_id=request.correlation_id,
            operation=request.operation,
            repo=request.repo,
            mode=EnumRepoWriteMode.DRY_RUN
            if request.dry_run
            else self._config.mode_for(request.repo),
            idempotency_key=request.idempotency_key,
            requested_by_lane=request.requested_by_lane,
            pr_number=request.pr_number,
            reason=reason,
            detail=detail,
        )

    def _claim_detail(self, request: ModelRepoWriteRequest, now: str) -> str | None:
        """Why the requester does not hold the PR; None when it does.

        A live claim in the registry the effect reads decides: it is the
        requester's or a peer's. With none there, the claim the caller carried
        is checked; with neither, the close is refused.
        """
        claims_dir = request.claims_dir or self._runtime_claims_dir
        held = (
            self._claims.active_claim(
                claims_dir=claims_dir, pr_key=request.pr_key, now=now
            )
            if claims_dir
            else None
        )
        if held is not None:
            return registry_claim_refusal(held, request)
        if request.claim is None:
            return f"no live claim on {request.pr_key}; pr_close needs one"
        return carried_claim_refusal(request.claim, request, now)

    async def _live_transport(
        self, repo: str | None = None
    ) -> UrllibGithubLandingTransport:
        choice = ModelRepoWriteCredentialChoice.from_contract(_CONTRACT_PATH)
        ref = contract_secret_ref(_CONTRACT_PATH, choice.token_secret)
        secret = await _credential_from_store(ref)
        if secret is None:
            secret = await self._app_token(choice, repo)
        # A resolved but refused token never changes identities by taking the App path.
        assert_github_token_is_actor(
            ModelLaneWriteIdentity.from_env(os.environ),
            token=secret.get_secret_value(),
        )
        return UrllibGithubLandingTransport(secret)

    async def _app_token(
        self, choice: ModelRepoWriteCredentialChoice, repo: str | None
    ) -> SecretStr:
        app = choice.fallback_app
        resolved: list[SecretStr | None] = []
        for name in (
            app.app_id_secret,
            app.private_key_secret,
            app.installation_id_secret,
        ):
            ref = contract_secret_ref(_CONTRACT_PATH, name)
            value = await _credential_from_store(ref)
            if value is None and name != app.installation_id_secret:
                raise GitHubAppCredentialMissingError(
                    f"GitHub write: {choice.token_secret} is absent and App ref "
                    f"{ref!r} is unreadable or missing from the secret store/environment"
                )
            resolved.append(value)
        app_id, private_key, installation = resolved
        assert app_id is not None
        assert private_key is not None
        installation_id = None
        if installation is not None:
            try:
                installation_id = int(installation.get_secret_value())
                if installation_id < 1:
                    raise ValueError
            except ValueError:
                raise GitHubAppIdentityError(
                    f"GitHub write: {app.installation_id_secret} must be a positive integer"
                ) from None
        owner, _, name = (repo or "OmniNode-ai").partition("/")
        try:
            # The existing synchronous minter must not block the runtime event loop.
            token = await asyncio.to_thread(
                mint_installation_token,
                app_id=app_id.get_secret_value(),
                private_key_pem=private_key.get_secret_value(),
                private_key_ref=app.private_key_secret,
                installation_id=installation_id,
                org=owner,
                repositories=[name] if name else None,
            )
        except Exception as exc:
            # SDK/HTTP exceptions can contain response bodies or credential values.
            # Never forward their text or chained traceback to runtime logging.
            status = exc.status_code if isinstance(exc, GitHubApiError) else None
            detail = f" (HTTP {status})" if status is not None else ""
            raise GitHubAppIdentityError(
                f"GitHub write App token mint refused{detail}; check permissions and "
                f"declared refs {app.app_id_secret}, {app.private_key_secret}, "
                f"{app.installation_id_secret}"
            ) from None
        return SecretStr(token)

    async def _run(self, run: _Run) -> ModelRepoWriteCompleted:
        op = run.command.operation
        if op is EnumRepoWriteOperation.PR_CREATE:
            return await _pr_create(run)
        if op is EnumRepoWriteOperation.PR_EDIT:
            return await _pr_edit(run)
        if op is EnumRepoWriteOperation.PR_READY:
            return await _pr_ready(run)
        if op is EnumRepoWriteOperation.PR_CLOSE:
            return await _pr_close(run)
        if op is EnumRepoWriteOperation.PR_COMMENT:
            return await _pr_comment(run)
        if op is EnumRepoWriteOperation.PR_THREAD_REPLY:
            return await _pr_thread_reply(run)
        if op is EnumRepoWriteOperation.PR_THREAD_RESOLVE:
            return await _pr_thread_resolve(run)
        if op is EnumRepoWriteOperation.PR_LABEL:
            return await _pr_label(run)
        if op is EnumRepoWriteOperation.REPO_VARIABLE_SET:
            return await _repo_variable_set(run)
        if op is EnumRepoWriteOperation.REPO_VARIABLE_GET:
            return await _repo_variable_get(run)
        if op is EnumRepoWriteOperation.RUNNER_LIST:
            return await _runner_list(run)
        if op is EnumRepoWriteOperation.RUNNER_REMOVE:
            return await _runner_remove(run)
        if op is EnumRepoWriteOperation.PR_REVIEW_POST:
            return await _pr_review_post(run)
        if op is EnumRepoWriteOperation.CHECK_RUN_CREATE:
            return await _check_run_create(run)
        return await _workflow_dispatch(run)


# --- operations ----------------------------------------------------------------


async def _pr_create(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    head, base, title = c.head or "", c.base or "", c.title or ""
    await run.expect_branch_tip(head)
    existing = (await run.send(open_pulls_for_head_request(c.repo, head))).body or {}
    prs = existing.get("value")
    if isinstance(prs, list) and prs and isinstance(prs[0], dict):
        pr = prs[0]
        return run.completed(
            pr_number=_int_at(pr, "number"),
            replayed=True,
            html_url=_str_at(pr, "html_url"),
            head_sha=_str_at(pr, "head", "sha"),
        )
    if c.repo.rsplit("/", 1)[-1].lower() in M4_REPOSITORIES:
        await _pr_create_contract_precondition(run)
    created = (
        await run.send(
            create_pull_request_request(
                c.repo,
                title=title,
                head=head,
                base=base,
                body=c.body or "",
                draft=bool(c.draft),
            )
        )
    ).body
    return run.completed(
        pr_number=_int_at(created, "number"),
        html_url=_str_at(created, "html_url"),
        head_sha=_str_at(created, "head", "sha"),
    )


async def _pr_create_contract_precondition(run: _Run) -> None:
    """Read each cited contract at one immutable head before sending POST."""
    c = run.command
    tickets = cited_tickets(c.title or "")
    if not tickets:
        raise run.refused(
            EnumRepoWriteRefusal.CONTRACT_REQUIRED,
            "pull request title must cite at least one OMN-[0-9]+ ticket",
        )
    head = c.head or ""
    ref = (await run.send(git_ref_request(c.repo, head))).body or {}
    sha = _str_at(ref, "object", "sha")
    if (
        sha is None
        or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None
        or (c.expected_head_sha and sha != c.expected_head_sha)
    ):
        raise run.refused(
            EnumRepoWriteRefusal.HEAD_MOVED, "PR head is unreadable or moved"
        )
    for ticket in tickets:
        response = await run.send(
            ModelGithubHttpRequest(
                method="GET",
                path=f"/repos/{c.repo}/contents/contracts/{ticket}.yaml?ref={quote(sha, safe='')}",
            ),
            allow_not_found=True,
        )
        text = None
        if response.status != 404:
            body = response.body or {}
            content = _str_at(body, "content")
            if (
                body.get("type") != "file"
                or body.get("encoding") != "base64"
                or content is None
            ):
                raise run.refused(
                    EnumRepoWriteRefusal.CONTRACT_REQUIRED,
                    f"contracts/{ticket}.yaml: contract content is unreadable",
                )
            try:
                text = base64.b64decode("".join(content.split()), validate=True).decode(
                    "utf-8"
                )
            except (binascii.Error, UnicodeDecodeError, ValueError):
                raise run.refused(
                    EnumRepoWriteRefusal.CONTRACT_REQUIRED,
                    f"contracts/{ticket}.yaml: contract content is unreadable",
                ) from None
        failure = contract_prerequisite(ticket, text)
        if failure:
            raise run.refused(EnumRepoWriteRefusal.CONTRACT_REQUIRED, failure)
    # A branch can move while the content reads run. Refuse before creation.
    latest = (await run.send(git_ref_request(c.repo, head))).body or {}
    if _str_at(latest, "object", "sha") != sha:
        raise run.refused(
            EnumRepoWriteRefusal.HEAD_MOVED, "PR head moved during contract preflight"
        )


async def _pr_edit(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    pr = await run.expect_pr_head()
    fields: dict[str, object] = {}
    if c.title is not None and c.title != pr.get("title"):
        fields["title"] = c.title
    if c.body is not None and c.body != (pr.get("body") or ""):
        fields["body"] = c.body
    if c.base is not None and c.base != _str_at(pr, "base", "ref"):
        fields["base"] = c.base
    changed = bool(fields)
    if fields:
        await run.send(edit_pull_request_request(c.repo, number, fields))
    node_id = _str_at(pr, "node_id") or ""
    is_draft = bool(pr.get("draft", False))
    if c.draft is True and not is_draft:
        await run.send(convert_to_draft_request(node_id))
        changed = True
    elif c.draft is False and is_draft:
        await run.send(mark_ready_request(node_id))
        changed = True
    return run.completed(
        replayed=not changed,
        html_url=_str_at(pr, "html_url"),
        head_sha=_str_at(pr, "head", "sha"),
    )


async def _pr_ready(run: _Run) -> ModelRepoWriteCompleted:
    pr = await run.expect_pr_head()
    already = not bool(pr.get("draft", False))
    if not already:
        await run.send(mark_ready_request(_str_at(pr, "node_id") or ""))
    return run.completed(
        replayed=already,
        html_url=_str_at(pr, "html_url"),
        head_sha=_str_at(pr, "head", "sha"),
    )


async def _pr_close(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    pr = await run.expect_pr_head()
    already = pr.get("state") == "closed"
    if not already:
        await run.send(
            edit_pull_request_request(c.repo, c.pr_number or 0, {"state": "closed"})
        )
    return run.completed(
        replayed=already,
        html_url=_str_at(pr, "html_url"),
        head_sha=_str_at(pr, "head", "sha"),
    )


async def _pr_comment(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    head_sha: str | None = None
    if c.expected_head_sha is not None:
        head_sha = _str_at(await run.expect_pr_head(), "head", "sha")
    marker = idempotency_marker(c.idempotency_key)
    request: ModelGithubHttpRequest | None = issue_comments_request(
        c.repo, number, per_page=LIST_PAGE_SIZE
    )
    for _page in range(_MARKER_SCAN_PAGES):
        if request is None:
            break
        response = await run.send(request)
        page = (response.body or {}).get("value")
        for comment in page if isinstance(page, list) else []:
            if isinstance(comment, dict) and marker in str(comment.get("body", "")):
                return run.completed(
                    replayed=True,
                    comment_id=_int_at(comment, "id"),
                    html_url=_str_at(comment, "html_url"),
                    head_sha=head_sha,
                )
        request = next_page_request(response)
    created = (
        await run.send(
            create_issue_comment_request(c.repo, number, f"{c.body}\n\n{marker}")
        )
    ).body
    return run.completed(
        comment_id=_int_at(created, "id"),
        html_url=_str_at(created, "html_url"),
        head_sha=head_sha,
    )


async def _pr_thread_reply(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    head_sha: str | None = None
    if c.expected_head_sha is not None:
        head_sha = _str_at(await run.expect_pr_head(), "head", "sha")
    marker = idempotency_marker(c.idempotency_key)
    request: ModelGithubHttpRequest | None = review_comments_request(
        c.repo, number, per_page=LIST_PAGE_SIZE
    )
    for _page in range(_MARKER_SCAN_PAGES):
        if request is None:
            break
        response = await run.send(request)
        page = (response.body or {}).get("value")
        for comment in page if isinstance(page, list) else []:
            if isinstance(comment, dict) and marker in str(comment.get("body", "")):
                return run.completed(
                    replayed=True,
                    comment_id=_int_at(comment, "id"),
                    html_url=_str_at(comment, "html_url"),
                    head_sha=head_sha,
                )
        request = next_page_request(response)
    created = (
        await run.send(
            create_review_comment_reply_request(
                c.repo, number, c.thread_comment_id or 0, f"{c.body}\n\n{marker}"
            )
        )
    ).body
    return run.completed(
        comment_id=_int_at(created, "id"),
        html_url=_str_at(created, "html_url"),
        head_sha=head_sha,
    )


async def _pr_review_post(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    head_sha = _str_at(await run.expect_pr_head(), "head", "sha")
    marker = idempotency_marker(c.idempotency_key)
    request: ModelGithubHttpRequest | None = pull_request_reviews_request(
        c.repo, number, per_page=LIST_PAGE_SIZE
    )
    for _page in range(_MARKER_SCAN_PAGES):
        if request is None:
            break
        response = await run.send(request)
        page = (response.body or {}).get("value")
        for review in page if isinstance(page, list) else []:
            if isinstance(review, dict) and marker in str(review.get("body", "")):
                return run.completed(
                    replayed=True,
                    review_id=_int_at(review, "id"),
                    html_url=_str_at(review, "html_url"),
                    head_sha=head_sha,
                )
        request = next_page_request(response)
    created = (
        await run.send(
            create_pull_request_review_request(
                c.repo, number, c.expected_head_sha or "", f"{c.body}\n\n{marker}"
            )
        )
    ).body
    return run.completed(
        review_id=_int_at(created, "id"),
        html_url=_str_at(created, "html_url"),
        head_sha=head_sha,
    )


async def _pr_thread_resolve(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    thread_id = c.review_thread_id or ""
    head_sha: str | None = None
    if c.expected_head_sha is not None:
        head_sha = _str_at(await run.expect_pr_head(), "head", "sha")
    after: str | None = None
    found: dict[str, object] | None = None
    for _page in range(THREAD_SCAN_PAGES):
        nodes, after = thread_nodes(
            await run.send(threads_page_request(c.repo, number, after))
        )
        found = next(
            (n for n in nodes if isinstance(n, dict) and n.get("id") == thread_id),
            None,
        )
        if found is not None or after is None:
            break
    if found is None:
        raise run.refused(
            EnumRepoWriteRefusal.NOT_FOUND,
            f"review thread {thread_id} is not on {c.repo}#{number}; no mutation sent",
        )
    comments = dict_at(found, "comments").get("nodes")
    comment_list = comments if isinstance(comments, list) else []
    url = _str_at(comment_list[0] if comment_list else None, "url")
    if found.get("isResolved") is True:
        return run.completed(replayed=True, html_url=url, head_sha=head_sha)
    if not has_fix_reply(comment_list):
        raise run.refused(
            EnumRepoWriteRefusal.THREAD_NOT_REPLIED,
            f"review thread {thread_id} on {c.repo}#{number} has no reply from the "
            "fixing lane; reply with pr-thread-reply first; no mutation sent",
        )
    await run.send(resolve_review_thread_request(thread_id))
    return run.completed(html_url=url, head_sha=head_sha)


async def _repo_variable_set(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    name = c.variable_name or ""
    plan = run.variable_plan
    value = plan.value if plan is not None else ""
    response = await run.send(get_variable_request(c.repo, name), allow_not_found=True)
    if response.status == 404:
        await run.send(create_variable_request(c.repo, name, value))
        return run.completed(variable_name=name, variable_digest=variable_digest(value))
    body = response.body or {}
    if body.get("value") == value:
        return run.completed(
            replayed=True,
            variable_name=name,
            variable_digest=variable_digest(value),
        )
    await run.send(update_variable_request(c.repo, name, value))
    return run.completed(variable_name=name, variable_digest=variable_digest(value))


async def _repo_variable_get(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    name = c.variable_name or ""
    plan = run.variable_plan
    response = await run.send(get_variable_request(c.repo, name))
    body = response.body or {}
    value = str(body.get("value", ""))
    in_sync: bool | None = None
    if plan is not None and plan.source_value is not None:
        in_sync = value == plan.source_value
    return run.completed(
        variable_name=name,
        variable_value=value,
        variable_digest=variable_digest(value),
        variable_in_sync=in_sync,
    )


async def _runners_pages(run: _Run, organization: str) -> list[ModelRepoWriteRunner]:
    """Every runner the organization has registered, page after page.

    ``list_runners_request`` asks 100 runners per page; a page holding fewer
    than that ends the paging.
    """
    runners: list[ModelRepoWriteRunner] = []
    page = 1
    while True:
        body = (await run.send(list_runners_request(organization, page))).body or {}
        registered = body.get("runners")
        entries = registered if isinstance(registered, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            runner_id = _int_at(entry, "id")
            name = entry.get("name")
            if runner_id is None or not isinstance(name, str) or not name:
                continue
            runners.append(
                ModelRepoWriteRunner(
                    id=runner_id,
                    name=name,
                    status=str(entry.get("status", "")),
                    busy=entry.get("busy") is True,
                )
            )
        if len(entries) < _RUNNERS_PAGE_SIZE:
            return runners
        page += 1


async def _runner_list(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    organization, _ = split_repository(c.repo)
    return run.completed(runners=tuple(await _runners_pages(run, organization)))


async def _runner_remove(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    name = c.runner_name or ""
    organization, _ = split_repository(c.repo)
    found = next(
        (
            runner
            for runner in await _runners_pages(run, organization)
            if runner.name == name
        ),
        None,
    )
    if found is None:
        # No registration of that name: the asked-for state already holds, so
        # the answer is replayed and no DELETE is sent.
        return run.completed(replayed=True, runner_name=name, runner_removed=False)
    if found.status == "online" or found.busy:
        raise run.refused(
            EnumRepoWriteRefusal.RUNNER_ONLINE,
            f"runner {name} on {organization} is "
            f"{'busy' if found.busy else found.status}; only an offline, idle "
            "runner may be removed; no DELETE sent",
        )
    await run.send(delete_runner_request(organization, found.id))
    return run.completed(runner_name=name, runner_removed=True)


async def _pr_label(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    pr = await run.expect_pr_head()
    labels = pr.get("labels")
    current = {
        name
        for label in (labels if isinstance(labels, list) else [])
        if isinstance(label, dict) and isinstance(name := label.get("name"), str)
    }
    to_add = [label for label in (c.add_labels or ()) if label not in current]
    to_remove = [label for label in (c.remove_labels or ()) if label in current]
    if to_add:
        await run.send(add_labels_request(c.repo, number, to_add))
    for label in to_remove:
        await run.send(remove_label_request(c.repo, number, label))
    return run.completed(
        replayed=not (to_add or to_remove),
        html_url=_str_at(pr, "html_url"),
        head_sha=_str_at(pr, "head", "sha"),
    )


def _check_run_create_request(c: ModelRepoWriteRequest) -> ModelGithubHttpRequest:
    body: dict[str, object] = {
        "name": c.name,
        "head_sha": c.head_sha,
        "status": c.status,
    }
    if c.conclusion is not None:
        body["conclusion"] = c.conclusion
    if c.output is not None:
        body["output"] = c.output.model_dump(mode="json")
    return ModelGithubHttpRequest(
        method="POST", path=f"/repos/{c.repo}/check-runs", body=body
    )


async def _check_run_create(run: _Run) -> ModelRepoWriteCompleted:
    created = (await run.send(_check_run_create_request(run.command))).body
    return run.completed(
        check_run_id=_int_at(created, "id"),
        html_url=_str_at(created, "html_url"),
        head_sha=_str_at(created, "head_sha"),
    )


async def _workflow_dispatch(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    ref = c.ref or ""
    await run.expect_branch_tip(ref)
    await run.send(workflow_dispatch_request(c.repo, c.workflow or "", ref, c.inputs))
    return run.completed(head_sha=c.expected_head_sha)


def planned_requests(
    request: ModelRepoWriteRequest,
) -> tuple[ModelGithubHttpRequest, ...]:
    """The requests an enforce run would send, before any answer is known.

    A request that depends on an answer (the PR's node id for a GraphQL
    mutation) carries a placeholder; a read that would find the write already
    done is listed, and the write after it is listed too.
    """
    c = request
    n = c.pr_number or 0
    op = c.operation
    tip = (
        [
            git_ref_request(
                c.repo,
                (c.head if op is EnumRepoWriteOperation.PR_CREATE else c.ref) or "",
            )
        ]
        if c.expected_head_sha is not None
        and op
        in (EnumRepoWriteOperation.PR_CREATE, EnumRepoWriteOperation.WORKFLOW_DISPATCH)
        else []
    )
    if op is EnumRepoWriteOperation.REPO_VARIABLE_SET:
        return (get_variable_request(c.repo, c.variable_name or ""),)
    if op is EnumRepoWriteOperation.REPO_VARIABLE_GET:
        return (get_variable_request(c.repo, c.variable_name or ""),)
    if op in (
        EnumRepoWriteOperation.RUNNER_LIST,
        EnumRepoWriteOperation.RUNNER_REMOVE,
    ):
        # A remove reads the registrations first, to find the id and check the
        # runner is offline and idle; the DELETE it may send is only known
        # after that read answers.
        return (list_runners_request(split_repository(c.repo)[0], 1),)
    if op is EnumRepoWriteOperation.PR_LABEL:
        return (
            pull_request_request(c.repo, n),
            *(
                (add_labels_request(c.repo, n, list(c.add_labels)),)
                if c.add_labels
                else ()
            ),
            *(
                remove_label_request(c.repo, n, label)
                for label in c.remove_labels or ()
            ),
        )
    if op is EnumRepoWriteOperation.PR_CREATE:
        return (
            *tip,
            open_pulls_for_head_request(c.repo, c.head or ""),
            create_pull_request_request(
                c.repo,
                title=c.title or "",
                head=c.head or "",
                base=c.base or "",
                body=c.body or "",
                draft=bool(c.draft),
            ),
        )
    if op is EnumRepoWriteOperation.CHECK_RUN_CREATE:
        return (_check_run_create_request(c),)
    if op is EnumRepoWriteOperation.WORKFLOW_DISPATCH:
        return (
            *tip,
            workflow_dispatch_request(c.repo, c.workflow or "", c.ref or "", c.inputs),
        )
    if op is EnumRepoWriteOperation.PR_COMMENT:
        read = [pull_request_request(c.repo, n)] if c.expected_head_sha else []
        return (
            *read,
            issue_comments_request(c.repo, n, per_page=LIST_PAGE_SIZE),
            create_issue_comment_request(
                c.repo, n, f"{c.body}\n\n{idempotency_marker(c.idempotency_key)}"
            ),
        )
    if op is EnumRepoWriteOperation.PR_THREAD_REPLY:
        read = [pull_request_request(c.repo, n)] if c.expected_head_sha else []
        return (
            *read,
            review_comments_request(c.repo, n, per_page=LIST_PAGE_SIZE),
            create_review_comment_reply_request(
                c.repo,
                n,
                c.thread_comment_id or 0,
                f"{c.body}\n\n{idempotency_marker(c.idempotency_key)}",
            ),
        )
    if op is EnumRepoWriteOperation.PR_REVIEW_POST:
        return (
            pull_request_request(c.repo, n),
            pull_request_reviews_request(c.repo, n, per_page=LIST_PAGE_SIZE),
            create_pull_request_review_request(
                c.repo,
                n,
                c.expected_head_sha or "",
                f"{c.body}\n\n{idempotency_marker(c.idempotency_key)}",
            ),
        )
    if op is EnumRepoWriteOperation.PR_THREAD_RESOLVE:
        read = [pull_request_request(c.repo, n)] if c.expected_head_sha else []
        return (
            *read,
            threads_page_request(c.repo, n, None),
            resolve_review_thread_request(c.review_thread_id or ""),
        )
    read_pr = pull_request_request(c.repo, n)
    if op is EnumRepoWriteOperation.PR_READY:
        return (read_pr, mark_ready_request(_PLANNED_NODE_ID))
    if op is EnumRepoWriteOperation.PR_CLOSE:
        return (read_pr, edit_pull_request_request(c.repo, n, {"state": "closed"}))
    fields = {
        k: v
        for k, v in (("title", c.title), ("body", c.body), ("base", c.base))
        if v is not None
    }
    planned: list[ModelGithubHttpRequest] = [read_pr]
    if fields:
        planned.append(edit_pull_request_request(c.repo, n, dict(fields)))
    if c.draft is True:
        planned.append(convert_to_draft_request(_PLANNED_NODE_ID))
    elif c.draft is False:
        planned.append(mark_ready_request(_PLANNED_NODE_ID))
    return tuple(planned)


__all__: list[str] = [
    "HandlerGithubRepoWriteEffect",
    "idempotency_marker",
    "planned_requests",
]
