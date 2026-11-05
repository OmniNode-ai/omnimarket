# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read blocked-lane evidence, diagnose ownership and route one shared owner lane."""

from __future__ import annotations

import hashlib
import json
from functools import cached_property
from pathlib import Path
from typing import Literal

from omnimarket.models.model_lane_routing import (
    ModelFillBacklogCommand,
    ModelLaneBlockFacts,
    ModelLaneRoutingOverlay,
    ModelLaneRoutingRequest,
    ModelLaneRoutingResult,
    ModelOwnerDiagnosis,
    ModelReadEvidence,
    ModelRoutingClaim,
    ModelTriageCommand,
)
from omnimarket.nodes.node_lane_block_diagnose_compute import (
    named_paths,
)
from omnimarket.protocols.protocol_lane_routing import ProtocolLaneRoutingGateway

from .adapters.adapter_routing_store import AdapterLaneRoutingStore


def claim_key(class_key: str, owner: str, head: str) -> str:
    return hashlib.sha256(
        json.dumps([class_key, owner, head], separators=(",", ":")).encode()
    ).hexdigest()


def read_bounded(path: Path, roots: tuple[str, ...], limit: int) -> str:
    resolved = path.resolve(strict=True)
    if not any(
        resolved.is_relative_to(Path(root).resolve(strict=True)) for root in roots
    ):
        raise ValueError("named path is outside the declared read roots")
    with resolved.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("named file exceeds the overlay's read bound")
    return content.decode("utf-8")


class HandlerLaneBlockDiagnoseEffect:
    def __init__(
        self,
        overlay: ModelLaneRoutingOverlay | None = None,
        gateway: ProtocolLaneRoutingGateway | None = None,
        store: AdapterLaneRoutingStore | None = None,
    ) -> None:
        self._provided_overlay = overlay
        self._provided_gateway = gateway
        self._provided_store = store

    @cached_property
    def _dependencies(
        self,
    ) -> tuple[ModelLaneRoutingOverlay, ProtocolLaneRoutingGateway]:
        # A clean runtime can construct and wire this handler before deployment
        # facts are available. Resolve them only when a command needs them.
        overlay, gateway = self._provided_overlay, self._provided_gateway
        if overlay is None or gateway is None:
            from .container.container_lane_block_diagnose_effect import (
                build_dependencies,
            )

            configured_overlay, configured_gateway = build_dependencies()
            overlay = overlay if overlay is not None else configured_overlay
            gateway = gateway if gateway is not None else configured_gateway
        return overlay, gateway

    @property
    def overlay(self) -> ModelLaneRoutingOverlay:
        return self._dependencies[0]

    @property
    def gateway(self) -> ProtocolLaneRoutingGateway:
        return self._dependencies[1]

    @cached_property
    def store(self) -> AdapterLaneRoutingStore:
        return (
            self._provided_store
            if self._provided_store is not None
            else AdapterLaneRoutingStore(Path(self.overlay.state_path))
        )

    def _exception(
        self, key: str, evidence: str, reason: str
    ) -> ModelLaneRoutingResult:
        self.gateway.exception(key, evidence, reason)
        return ModelLaneRoutingResult(
            status="exception",
            key=key,
            reason=reason,
            diagnosis=ModelOwnerDiagnosis(reason=reason),
        )

    def handle(self, request: ModelLaneRoutingRequest) -> ModelLaneRoutingResult:
        if request.operation == "initial":
            return self._initial(request)
        terminal = request.terminal
        if terminal is None:
            raise ValueError("diagnose requires a blocked or failed TERMINAL")
        key = claim_key(terminal.class_key, terminal.repository, terminal.head)
        try:
            report = read_bounded(
                Path(terminal.report_path),
                self.overlay.report_roots,
                self.overlay.max_report_bytes,
            )
        except (OSError, ValueError, UnicodeError) as error:
            return self._exception(key, terminal.blocker, f"report unreadable: {error}")
        paths = named_paths(terminal.blocker + "\n" + report)
        if len(paths) > self.overlay.max_named_files:
            return self._exception(
                key, report, "named files exceed the overlay's read bound"
            )
        files: list[ModelReadEvidence] = []
        errors: list[str] = []
        for named in paths:
            path = Path(named)
            candidates = (
                (path,)
                if path.is_absolute()
                else tuple(Path(root) / path for root in self.overlay.repository_roots)
            )
            for candidate in candidates:
                try:
                    text = read_bounded(
                        candidate,
                        self.overlay.repository_roots,
                        self.overlay.max_file_bytes,
                    )
                    files.append(ModelReadEvidence(path=named, text=text))
                    break
                except (OSError, ValueError, UnicodeError) as error:
                    failure = f"{named}: {type(error).__name__}: {error}"
            else:
                errors.append(failure)
        diagnosis = self.gateway.diagnose(
            ModelLaneBlockFacts(
                blocker=terminal.blocker,
                report_text=report,
                files=tuple(files),
                owner_map=self.overlay.owner_map,
            )
        )
        if diagnosis.owner is None:
            result = self._exception(key, report, diagnosis.reason)
        elif diagnosis.owner == terminal.repository:
            result = ModelLaneRoutingResult(
                status="same-owner", reason="named owner is the originating repository"
            )
        else:
            result = self._dispatch(
                terminal.class_key,
                diagnosis.owner,
                terminal.head,
                terminal.item_ids,
                terminal.brief,
                report,
                terminal.blocker,
                terminal.subject_kind,
            )
        return result.model_copy(
            update={
                "diagnosis": diagnosis,
                "read_files": tuple(file.path for file in files),
                "read_errors": tuple(errors),
            }
        )

    def _dispatch(
        self,
        class_key: str,
        owner: str,
        head: str,
        items: tuple[str, ...],
        brief: str,
        report: str,
        blocker: str,
        subject_kind: Literal["item", "ticket", "pull_request"],
    ) -> ModelLaneRoutingResult:
        key = claim_key(class_key, owner, head)
        command = ModelFillBacklogCommand(
            target_repo=owner,
            subject_kind=subject_kind,
            key=key,
            class_key=class_key,
            head=head,
            for_items=items,
            report_text=report,
            brief=brief
            + "\n\n"
            + "\n".join("for-item=" + item for item in items)
            + "\n\nOriginal blocker:\n"
            + blocker
            + "\n\nOriginal report:\n"
            + report,
        )
        # Admission is the core validator's typed seam, including target_repo and subject kind.
        refusal = self.gateway.admit(command, self.overlay.owner_map)
        if refusal is not None:
            return self._exception(key, report, refusal)
        with self.store.locked():
            state = self.store.read()
            claim = state.claims.get(key)
            if claim is None:
                claim = ModelRoutingClaim(
                    key=key,
                    class_key=class_key,
                    owner=owner,
                    head=head,
                    item_ids=tuple(dict.fromkeys(items)),
                    command=command,
                )
            else:
                claim = claim.model_copy(
                    update={"item_ids": tuple(dict.fromkeys((*claim.item_ids, *items)))}
                )
            state.claims[key] = claim
            # Save all origins even when this is a join, and BEFORE the owner call.
            self.store.write(state)
            dispatched = claim.lane is None
            if dispatched:
                lane = self.gateway.fill(claim.command)
                if not lane:
                    raise ValueError("fill-backlog-item returned no owner lane")
                claim = claim.model_copy(update={"lane": lane})
                state.claims[key] = claim
                self.store.write(state)
            assert claim.lane is not None
            self.gateway.bind(key, claim.lane, claim.item_ids)
            return ModelLaneRoutingResult(
                status="dispatched" if dispatched else "joined",
                key=key,
                lane=claim.lane,
                reason="shared claim owns one owner lane",
            )

    def _initial(self, request: ModelLaneRoutingRequest) -> ModelLaneRoutingResult:
        item = request.item
        if item is None:
            raise ValueError("initial routing requires an item")
        key = claim_key("initial", item.item_id, item.head)
        with self.store.locked():
            existing = next(
                (
                    claim
                    for claim in self.store.read().claims.values()
                    if claim.class_key == "initial:" + item.item_id
                    and claim.head == item.head
                ),
                None,
            )
        if existing is not None and item.ticket is None:
            command = existing.command
            return self._dispatch(
                existing.class_key,
                existing.owner,
                existing.head,
                existing.item_ids,
                item.text,
                command.report_text,
                "",
                command.subject_kind,
            )
        if item.ticket is not None:
            owner = self.gateway.ticket_repository(item.ticket)
            if owner is None:
                return self._exception(
                    key, item.text, "ticket repository is unreadable or unset"
                )
        elif request.triage_report is not None:
            with self.store.locked():
                if item.item_id not in self.store.read().triages:
                    return self._exception(
                        key, item.text, "no open triage for this completion"
                    )
            diagnosis = self.gateway.diagnose(
                ModelLaneBlockFacts(
                    blocker="",
                    report_text=request.triage_report,
                    owner_map=self.overlay.owner_map,
                )
            )
            if diagnosis.owner is None:
                result = self._exception(key, request.triage_report, diagnosis.reason)
                self._close_triage(item.item_id)
                return result
            owner = diagnosis.owner
        else:
            with self.store.locked():
                state = self.store.read()
                if (
                    item.item_id not in state.triages
                    and len(state.triages) >= self.overlay.max_open_triage
                ):
                    return ModelLaneRoutingResult(
                        status="triage-cap", key=key, reason="max_open_triage reached"
                    )
                lane = state.triages.get(item.item_id)
                if lane is None:
                    state.triages[item.item_id] = None
                    self.store.write(state)
                    lane = self.gateway.triage(
                        ModelTriageCommand(
                            key=key, item_id=item.item_id, text=item.text
                        )
                    )
                    if not lane:
                        raise ValueError("triage returned no read-only lane")
                    state.triages[item.item_id] = lane
                    self.store.write(state)
                return ModelLaneRoutingResult(
                    status="triage",
                    key=key,
                    lane=lane,
                    reason="read-only triage; no repository guessed",
                )
        result = self._dispatch(
            "initial:" + item.item_id,
            owner,
            item.head,
            (item.item_id,),
            item.text,
            request.triage_report or item.text,
            "",
            item.subject_kind,
        )
        if request.triage_report is not None:
            self._close_triage(item.item_id)
        return result

    def _close_triage(self, item_id: str) -> None:
        with self.store.locked():
            state = self.store.read()
            state.triages.pop(item_id, None)
            self.store.write(state)
