# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReceiptGatePinAdvance - plan the receipt-gate caller pin advances (OMN-20134).

Every repository that runs the Repo Evidence Gate calls omnibase_core's
receipt-gate reusable at a pinned dev-merge tag. Nothing moved those pins when
the reusable changed: the fix-parent control fallback landed in core and no
caller carried it. This is the pure half of the mechanism. The callers' pin
sites are contract data (``receipt_gate_pin_policy`` in this node's
``contract.yaml``, read by the effect step and passed in the request, because a
compute node reads no file), the dev tags of the source repository come in with the git
blob id of the gate file at each, and the result is one plan per caller:

* the caller's pin resolves to a known tag whose gate blob equals the newest
  tag's: ``current``, no pull request;
* its pin resolves to a tag whose gate blob differs from the newest tag's:
  ``stale``, the newest tag is the target;
* its pin is not a dev-merge tag where the site declares one (a full commit
  sha, a branch): ``untagged``, the newest tag is the target;
* its pin names a tag or commit the tag list does not hold: ``unresolved``,
  the newest tag is the target;
* the pin is current but a second site (a required-checks ref, a test
  constant) holds another ref: ``sites_disagree``, the edits realign it.

A plan with edits carries each file's full new text, so the effect step makes
one commit with the pin line and its test constant together. A site that does
not match exactly its declared number of times, or a file missing from the
snapshot, is a ``pin_site_unmatched`` / ``snapshot_missing`` plan with no edits:
the declared data and the repository disagree, and no pull request is guessed.
"""

from __future__ import annotations

import re
from typing import Literal

from omnimarket.models.model_receipt_gate_pin_advance import (
    DEV_TAG_PATTERN,
    EnumCallerVerdict,
    EnumPinForm,
    ModelCallerPlan,
    ModelCallerSnapshot,
    ModelCoreDevTag,
    ModelFileEdit,
    ModelPinCaller,
    ModelPinSite,
    ModelReceiptGatePinAdvanceRequest,
    ModelReceiptGatePinAdvanceResult,
    ModelReceiptGatePinPolicy,
)

_DEV_TAG = re.compile(DEV_TAG_PATTERN)
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _stamp(tag: ModelCoreDevTag) -> str:
    match = _DEV_TAG.match(tag.tag)
    assert match is not None  # the model pattern guarantees the shape
    return match.group("stamp")


def _matches(text: str, site: ModelPinSite) -> list[re.Match[str]]:
    return list(re.finditer(site.pattern, text, re.MULTILINE))


def _rewrite(text: str, site: ModelPinSite, *, tag: str, sha: str) -> str:
    """Replace the 'ref' group (and the 'tag' group when named) of every match."""
    ref_value = tag if site.ref_form is EnumPinForm.TAG else sha
    spans: list[tuple[int, int, str]] = []
    for match in _matches(text, site):
        spans.append((*match.span("ref"), ref_value))
        if "tag" in match.re.groupindex and match.group("tag") is not None:
            spans.append((*match.span("tag"), tag))
    for start, end, value in sorted(spans, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def _sites(caller: ModelPinCaller) -> tuple[ModelPinSite, ...]:
    return (*caller.pin_sites, *caller.test_constants)


class HandlerReceiptGatePinAdvance:
    """COMPUTE handler: one pin-advance plan per declared caller."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelReceiptGatePinAdvanceRequest
    ) -> ModelReceiptGatePinAdvanceResult:
        newest = max(request.core_tags, key=_stamp)
        by_tag = {t.tag: t for t in request.core_tags}
        by_sha = {t.commit_sha: t for t in request.core_tags}
        snapshots = {s.repository: s for s in request.callers}
        policy = request.policy
        plans = tuple(
            self._plan(
                policy,
                caller,
                snapshots.get(caller.repository),
                newest,
                by_tag,
                by_sha,
            )
            for caller in policy.callers
        )
        return ModelReceiptGatePinAdvanceResult(
            newest_tag=newest.tag,
            newest_receipt_gate_blob=newest.receipt_gate_blob,
            plans=plans,
            excluded=policy.excluded,
        )

    def _plan(
        self,
        policy: ModelReceiptGatePinPolicy,
        caller: ModelPinCaller,
        snapshot: ModelCallerSnapshot | None,
        newest: ModelCoreDevTag,
        by_tag: dict[str, ModelCoreDevTag],
        by_sha: dict[str, ModelCoreDevTag],
    ) -> ModelCallerPlan:
        def refuse(verdict: EnumCallerVerdict, detail: str) -> ModelCallerPlan:
            return ModelCallerPlan(
                repository=caller.repository,
                verdict=verdict,
                pin_kind=caller.pin_sites[0].ref_form,
                detail=detail,
                base_branch=caller.base_branch,
                base_head_sha="" if snapshot is None else snapshot.base_head_sha,
            )

        if snapshot is None:
            return refuse(
                EnumCallerVerdict.SNAPSHOT_MISSING,
                f"no files were read for {caller.repository}",
            )
        for site in _sites(caller):
            text = snapshot.files.get(site.path)
            if text is None:
                return refuse(
                    EnumCallerVerdict.PIN_SITE_UNMATCHED,
                    f"{site.path} is not in the snapshot of {caller.repository}",
                )
            found = len(_matches(text, site))
            if found != site.expected_matches:
                return refuse(
                    EnumCallerVerdict.PIN_SITE_UNMATCHED,
                    f"{site.path}: expected {site.expected_matches} match(es) of "
                    f"the declared pin pattern, found {found}",
                )

        reference = caller.pin_sites[0]
        current_ref = _matches(snapshot.files[reference.path], reference)[0].group(
            "ref"
        )
        pinned, judged, judged_detail = self._judge(
            current_ref, reference.ref_form, newest, by_tag, by_sha
        )
        target = pinned if pinned is not None and judged is None else newest
        edits = self._edits(caller, snapshot, target)
        if judged is not None:
            verdict, detail = judged, judged_detail
        elif edits:
            verdict = EnumCallerVerdict.SITES_DISAGREE
            detail = "another pin site holds a different ref than the reference site"
        else:
            verdict = EnumCallerVerdict.CURRENT
            detail = f"pinned at {target.tag}, whose gate matches the newest tag"
        if verdict is EnumCallerVerdict.CURRENT:
            return ModelCallerPlan(
                repository=caller.repository,
                verdict=verdict,
                pin_kind=caller.pin_sites[0].ref_form,
                detail=detail,
                base_branch=caller.base_branch,
                base_head_sha=snapshot.base_head_sha,
                current_ref=current_ref,
                current_tag=target.tag,
                target_tag=target.tag,
                target_commit_sha=target.commit_sha,
            )
        return self._with_pull_request(
            policy,
            caller,
            snapshot,
            verdict,
            detail,
            current_ref,
            pinned,
            target,
            edits,
        )

    @staticmethod
    def _judge(
        ref: str,
        form: EnumPinForm,
        newest: ModelCoreDevTag,
        by_tag: dict[str, ModelCoreDevTag],
        by_sha: dict[str, ModelCoreDevTag],
    ) -> tuple[ModelCoreDevTag | None, EnumCallerVerdict | None, str]:
        """The tag the ref names, and a verdict unless the pin is up to date."""
        if form is EnumPinForm.TAG:
            if _SHA40.match(ref) or not _DEV_TAG.match(ref):
                return (
                    by_sha.get(ref),
                    EnumCallerVerdict.UNTAGGED,
                    f"{ref!r} is not a dev-merge tag; pins are dev-merge tags only",
                )
            pinned = by_tag.get(ref)
        else:
            if not _SHA40.match(ref):
                return (
                    None,
                    EnumCallerVerdict.UNTAGGED,
                    f"{ref!r} is not a full commit sha",
                )
            pinned = by_sha.get(ref)
        if pinned is None:
            return (
                None,
                EnumCallerVerdict.UNRESOLVED,
                f"{ref} names no dev-merge tag of the source repository",
            )
        if pinned.receipt_gate_blob != newest.receipt_gate_blob:
            return (
                pinned,
                EnumCallerVerdict.STALE,
                f"{pinned.tag} holds gate blob {pinned.receipt_gate_blob[:12]}; "
                f"{newest.tag} holds {newest.receipt_gate_blob[:12]}",
            )
        return pinned, None, ""

    @staticmethod
    def _edits(
        caller: ModelPinCaller, snapshot: ModelCallerSnapshot, target: ModelCoreDevTag
    ) -> tuple[ModelFileEdit, ...]:
        rewritten = dict(snapshot.files)
        for site in _sites(caller):
            rewritten[site.path] = _rewrite(
                rewritten[site.path], site, tag=target.tag, sha=target.commit_sha
            )
        return tuple(
            ModelFileEdit(path=path, new_text=rewritten[path])
            for path in dict.fromkeys(site.path for site in _sites(caller))
            if rewritten[path] != snapshot.files[path]
        )

    @staticmethod
    def _with_pull_request(
        policy: ModelReceiptGatePinPolicy,
        caller: ModelPinCaller,
        snapshot: ModelCallerSnapshot,
        verdict: EnumCallerVerdict,
        detail: str,
        current_ref: str,
        pinned: ModelCoreDevTag | None,
        target: ModelCoreDevTag,
        edits: tuple[ModelFileEdit, ...],
    ) -> ModelCallerPlan:
        ticket = policy.ticket
        from_text = pinned.tag if pinned is not None else current_ref
        if verdict is EnumCallerVerdict.SITES_DISAGREE:
            title = (
                f"ci({ticket}): align the receipt-gate caller pin sites on {target.tag}"
            )
        else:
            title = f"ci({ticket}): advance the receipt-gate caller pin to {target.tag}"
        body = (
            f"{ticket}: advance this repository's Repo Evidence Gate caller from "
            f"`{from_text}` to the {policy.source_repository} dev-merge tag "
            f"`{target.tag}` (commit `{target.commit_sha}`).\n\n"
            f"Verdict: {verdict.value}. {detail}.\n\n"
            "Edited, together in one commit: "
            + ", ".join(f"`{edit.path}`" for edit in edits)
            + ".\n\nOpened by node_receipt_gate_pin_advance_effect from the callers "
            "declared in node_receipt_gate_pin_advance_compute's contract."
        )
        return ModelCallerPlan(
            repository=caller.repository,
            verdict=verdict,
            pin_kind=caller.pin_sites[0].ref_form,
            detail=detail,
            base_branch=caller.base_branch,
            base_head_sha=snapshot.base_head_sha,
            current_ref=current_ref,
            current_tag=None if pinned is None else pinned.tag,
            target_tag=target.tag,
            target_commit_sha=target.commit_sha,
            edits=edits,
            branch=f"{policy.branch_prefix}/{target.tag}",
            title=title,
            body=body,
            commit_message=f"{title}\n\nfrom {from_text} to {target.tag}",
            idempotency_key=(
                f"receipt-gate-pin:{caller.repository.replace('/', '.')}:{target.tag}"
            ),
        )


__all__ = ["HandlerReceiptGatePinAdvance"]
