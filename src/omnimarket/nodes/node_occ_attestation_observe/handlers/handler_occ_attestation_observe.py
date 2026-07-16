# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccAttestationObserve — the OMN-14393 report-only attestation gate (read-only).

Observes the OCC companion on a real product PR and emits ONE
:class:`ModelOccAutoauthorObservation`:

  * ``occ_pr_number``  — resolved from the product PR's ``Evidence-Source`` stamp.
  * ``minted_by_node`` — True iff the OCC PR carries ``occ:machine-minted`` (the
    marker seam; branch prefix alone cannot distinguish node from emitter).
  * ``attestation_match`` — True iff the on-PR companion files are byte-reproducible
    from ``compute_companion_plan`` (via ``verify_companion_attestation``): the
    expected pass-2 plan is recomputed from live PR + OCC facts, and each expected
    companion file's ACTUAL content on the OCC PR is byte-diffed against it
    (observed facts — timestamps/probes — projected out by the oracle).
  * ``occ_preflight_eligible`` — True iff the product PR's ``occ-preflight /
    eligibility`` check-run concluded ``success``.

This is DISTINCT from the observe workflow (``node_occ_companion_effect`` dry_run),
which reports "what the producer WOULD author". This reports "does the companion
actually on this PR match + pass occ-preflight".

REPORT-ONLY / DEFAULT-OFF (design §3): read-only. It never opens, stamps, or
blocks anything, and it is fail-soft — any resolution error yields a not-clean
observation with a reason, never a raised exception, so the non-blocking CI check
always produces a typed record.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnibase_compat.contracts.pr_occ_stamp import (
    EnumPrEvidenceSourceKind,
    parse_pr_occ_metadata_stamp,
    render_pr_occ_metadata_stamp,
)

from omnimarket.events.occ_autoauthor import (
    ModelOccAutoauthorObservation,
    is_machine_minted,
)
from omnimarket.events.occ_companion import (
    ModelCompanionFile,
    ModelObservedProbe,
    ModelOccCompanionPlan,
    ModelOccCompanionRequest,
    ModelOccStateRequest,
)
from omnimarket.github_api import (
    GitHubApiError,
    rest_json,
    rest_json_array,
    split_repo,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_occ_attestation_observe.models.model_occ_attestation_observe_request import (
    ModelOccAttestationObserveRequest,
)
from omnimarket.occ_autoauthor_attestation import (
    HandlerOccStateEffect,
    compute_companion_plan,
    verify_companion_attestation,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_OCC_PREFLIGHT_CHECK_NAME = "occ-preflight / eligibility"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without any network)                            #
# --------------------------------------------------------------------------- #


def resolve_evidence_source_occ_pr(pr_body: str) -> int | None:
    """Pure: the OCC PR number stamped as ``Evidence-Source: OCC#<n>``, or None."""
    source = parse_pr_occ_metadata_stamp(pr_body).evidence_source
    if source is not None and source.kind is EnumPrEvidenceSourceKind.OCC_PR:
        return source.occ_pr_number
    return None


def strip_evidence_source_stamp(pr_body: str) -> str:
    """Pure: remove the ``Evidence-Source`` stamp from a product PR body.

    The canonical plan no-ops when the product body is already bound to an OCC
    source (``_already_bound_occ``). To recompute the companion the producer
    ORIGINALLY authored (from the pre-stamp body), the attestation strips the
    Evidence-Source before recompute. The cited ticket text is untouched, so
    ticket extraction and the rendered companion are unchanged.
    """
    parsed = parse_pr_occ_metadata_stamp(pr_body)
    if parsed.evidence_source is None:
        return pr_body
    return render_pr_occ_metadata_stamp(
        parsed.model_copy(update={"evidence_source": None})
    )


def build_observed_files(
    expected_files: tuple[ModelCompanionFile, ...],
    actual_content_by_path: dict[str, str],
) -> tuple[ModelCompanionFile, ...]:
    """Pure: pair each expected companion file's metadata with its ACTUAL content.

    For each file the canonical plan expects, look up the bytes actually present
    on the OCC PR at that path. A file absent from ``actual_content_by_path`` is
    dropped, which shrinks the observed set and therefore FAILS the fingerprint —
    a missing companion file is, correctly, not reproducible. The metadata
    (kind/ticket_id/hashes) is taken from the expected plan so the oracle diffs
    the CONTENT; a divergent file's real hash also diverges in its rendered text,
    so a content swap cannot hide behind copied metadata.
    """
    observed: list[ModelCompanionFile] = []
    for expected in expected_files:
        actual = actual_content_by_path.get(expected.path)
        if actual is None:
            continue
        observed.append(expected.model_copy(update={"content": actual}))
    return tuple(observed)


def extract_check_conclusion(
    check_runs: list[dict[str, object]], name: str
) -> str | None:
    """Pure: the ``conclusion`` of the newest check-run named ``name``, or None.

    GitHub returns multiple runs per name across re-runs; the last one in the
    (started_at-ordered) list is the current state. Returns None when the check
    has never reported — which the caller treats as NOT eligible (fail-safe).
    """
    conclusion: str | None = None
    for run in check_runs:
        if run.get("name") != name:
            continue
        raw = run.get("conclusion")
        conclusion = raw if isinstance(raw, str) else None
    return conclusion


# --------------------------------------------------------------------------- #
# Handler                                                                     #
# --------------------------------------------------------------------------- #


class HandlerOccAttestationObserve:
    """Read-only EFFECT: observe + attest the OCC companion on a product PR."""

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
        request: ModelOccAttestationObserveRequest,
    ) -> ModelOccAutoauthorObservation:
        observed_at = datetime.now(UTC).isoformat()
        logger.info(
            "occ_attestation_observe: repo=%s pr=%s correlation_id=%s",
            request.repo,
            request.pr_number,
            request.correlation_id,
        )
        try:
            return await self._observe(request, observed_at)
        except Exception as exc:  # fallback-ok: report-only gate must not raise
            logger.exception("occ_attestation_observe: observation failed")
            return ModelOccAutoauthorObservation(
                product_repo=request.repo,
                product_pr_number=request.pr_number,
                observed_at=observed_at,
                reason=f"observation error (report-only, non-blocking): {type(exc).__name__}",
            )

    async def _observe(
        self,
        request: ModelOccAttestationObserveRequest,
        observed_at: str,
    ) -> ModelOccAutoauthorObservation:
        token = self._resolve_github_token()

        # READ the live product-PR + OCC facts (reuses RSD-2, read-only).
        expected_request = await self._state_handler.handle(
            ModelOccStateRequest(
                repo=request.repo,
                pr_number=request.pr_number,
                occ_repo=request.occ_repo,
                runner=request.runner,
                verifier=request.verifier,
            )
        )

        occ_preflight_eligible = await asyncio.to_thread(
            self._read_occ_preflight_eligible,
            request.repo,
            expected_request.pr_head_sha,
            token,
        )

        occ_pr_number = resolve_evidence_source_occ_pr(expected_request.pr_body)
        if occ_pr_number is None:
            return ModelOccAutoauthorObservation(
                product_repo=request.repo,
                product_pr_number=request.pr_number,
                occ_pr_number=None,
                minted_by_node=False,
                attestation_match=False,
                occ_preflight_eligible=occ_preflight_eligible,
                observed_at=observed_at,
                reason="no Evidence-Source OCC PR stamped on the product PR body",
            )

        occ_head_sha, minted_by_node = await asyncio.to_thread(
            self._read_occ_pr_head_and_marker, request.occ_repo, occ_pr_number, token
        )

        attestation_match, reason = await asyncio.to_thread(
            self._attest_companion,
            expected_request,
            request.occ_repo,
            occ_pr_number,
            occ_head_sha,
            token,
        )

        return ModelOccAutoauthorObservation(
            product_repo=request.repo,
            product_pr_number=request.pr_number,
            occ_pr_number=occ_pr_number,
            minted_by_node=minted_by_node,
            attestation_match=attestation_match,
            occ_preflight_eligible=occ_preflight_eligible,
            observed_at=observed_at,
            reason=reason,
        )

    # -- attestation --------------------------------------------------------

    def _attest_companion(
        self,
        expected_request: ModelOccCompanionRequest,
        occ_repo: str,
        occ_pr_number: int,
        occ_head_sha: str,
        token: str,
    ) -> tuple[bool, str]:
        """Recompute the pass-2 plan and byte-diff it against the on-PR companion."""
        occ_probe = ModelObservedProbe(
            command=f"gh pr view {occ_pr_number} --repo {occ_repo} --json number,state",
            stdout=json.dumps(
                {"number": occ_pr_number, "state": "OPEN"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            exit_code=0,
        )
        companion_request_v2 = expected_request.model_copy(
            update={
                "pr_body": strip_evidence_source_stamp(expected_request.pr_body),
                "occ_pr_number": occ_pr_number,
                "occ_head_sha": occ_head_sha or None,
                "occ_probe": occ_probe,
            }
        )
        expected_plan: ModelOccCompanionPlan = compute_companion_plan(
            companion_request_v2
        )
        if not expected_plan.companion_files:
            return False, (
                "canonical plan renders no companion files for this PR "
                f"(no_op={expected_plan.no_op}, fast_path={expected_plan.fast_path})"
            )

        ref = occ_head_sha or expected_plan.branch
        actual_content_by_path = {
            f.path: content
            for f in expected_plan.companion_files
            if (content := self._content_at_ref(occ_repo, f.path, ref, token))
            is not None
        }
        observed_files = build_observed_files(
            expected_plan.companion_files, actual_content_by_path
        )
        result = verify_companion_attestation(observed_files, companion_request_v2)
        return result.accepted, result.reason

    # -- I/O boundary -------------------------------------------------------

    def _resolve_github_token(self) -> str:
        ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        secret = resolve_api_key(ref, env_var_fallback=ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {ref!r} resolved to None — ensure GITHUB_TOKEN is set."
            )
        return secret.get_secret_value()

    def _read_occ_preflight_eligible(
        self, repo: str, head_sha: str, token: str
    ) -> bool:
        if not head_sha:
            return False
        owner, name = split_repo(repo)
        try:
            runs: list[dict[str, object]] = []
            page = 1
            while True:
                batch = rest_json_array(
                    "GET",
                    f"/repos/{owner}/{name}/commits/{head_sha}/check-runs"
                    f"?per_page=100&page={page}",
                    token=token,
                )
                runs.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        except GitHubApiError:
            return False
        return extract_check_conclusion(runs, _OCC_PREFLIGHT_CHECK_NAME) == "success"

    def _read_occ_pr_head_and_marker(
        self, occ_repo: str, occ_pr_number: int, token: str
    ) -> tuple[str, bool]:
        owner, name = split_repo(occ_repo)
        pr = rest_json(
            "GET", f"/repos/{owner}/{name}/pulls/{occ_pr_number}", token=token
        )
        head = pr.get("head")
        head_sha = str(head.get("sha") or "") if isinstance(head, dict) else ""
        raw_labels = pr.get("labels")
        label_names: list[str] = []
        if isinstance(raw_labels, list):
            for label in raw_labels:
                if isinstance(label, dict) and isinstance(label.get("name"), str):
                    label_names.append(str(label["name"]))
        return head_sha, is_machine_minted(label_names)

    def _content_at_ref(self, repo: str, path: str, ref: str, token: str) -> str | None:
        import base64
        import binascii
        import urllib.parse

        owner, name = split_repo(repo)
        encoded_path = urllib.parse.quote(path, safe="/")
        try:
            data = rest_json(
                "GET",
                f"/repos/{owner}/{name}/contents/{encoded_path}?ref={ref}",
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


__all__ = [
    "HandlerOccAttestationObserve",
    "build_observed_files",
    "extract_check_conclusion",
    "resolve_evidence_source_occ_pr",
    "strip_evidence_source_stamp",
]
