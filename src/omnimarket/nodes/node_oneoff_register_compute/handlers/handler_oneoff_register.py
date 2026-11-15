# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one-off register: ``handle(ModelOneOffRegisterRequest) -> ModelOneOffRegisterResult`` (OMN-19998).

* Register: a liveness process on an in-scope host and state, neither an entry's
  liveness process nor an exclusion, fails the reading, as does an entry naming a
  liveness process the liveness does not know.
* Replacement: an entry of kind ``node`` whose node is not among the nodes that
  exist fails the reading. Unplanned and bypass readings are reported, not failures.
* A ``retired`` entry is not checked against nodes or liveness and reads no findings.
"""

from __future__ import annotations

from collections import Counter

from omnimarket.nodes.node_oneoff_register_compute.models.model_oneoff_register import (
    EnumBypassReading,
    EnumOneOffState,
    EnumReplacementKind,
    ModelLivenessProcessRef,
    ModelOneOffReading,
    ModelOneOffRegisterRequest,
    ModelOneOffRegisterResult,
)


def _in_scope(
    process: ModelLivenessProcessRef,
    scope_hosts: tuple[str, ...],
    scope_states: tuple[str, ...],
) -> bool:
    return process.host in scope_hosts and process.state in scope_states


class HandlerOneOffRegister:
    """Read the register against the liveness processes and the nodes that exist; touch nothing."""

    def handle(self, request: ModelOneOffRegisterRequest) -> ModelOneOffRegisterResult:
        policy = request.policy
        live_ids = {process.process_id for process in request.liveness_processes}
        registered_process_ids = {
            entry.liveness_process_id
            for entry in policy.entries
            if entry.liveness_process_id
        }
        excluded_process_ids = {exclusion.process_id for exclusion in policy.exclusions}
        node_names = set(request.node_names)

        unregistered = {
            process.process_id
            for process in request.liveness_processes
            if _in_scope(process, policy.scope_hosts, policy.scope_states)
            and process.process_id not in registered_process_ids
            and process.process_id not in excluded_process_ids
        }
        unknown = {
            entry.one_off_id
            for entry in policy.entries
            if entry.state is not EnumOneOffState.RETIRED
            and entry.liveness_process_id
            and entry.liveness_process_id not in live_ids
        }
        missing_nodes = {
            entry.one_off_id
            for entry in policy.entries
            if entry.state is not EnumOneOffState.RETIRED
            and entry.replacement_kind is EnumReplacementKind.NODE
            and entry.replacement_ref not in node_names
        }
        unplanned = {
            entry.one_off_id
            for entry in policy.entries
            if entry.state is EnumOneOffState.NO_REPLACEMENT
        }
        bypass_yes = {
            entry.one_off_id
            for entry in policy.entries
            if entry.bypass is EnumBypassReading.YES
        }
        bypass_unread = {
            entry.one_off_id
            for entry in policy.entries
            if entry.bypass is EnumBypassReading.UNREAD
        }
        active = {
            entry.one_off_id
            for entry in policy.entries
            if entry.state is not EnumOneOffState.RETIRED
        }
        unplanned &= active
        bypass_yes &= active
        bypass_unread &= active

        readings = []
        for entry in policy.entries:
            findings: list[str] = []
            replacement_found: bool | None = None
            if entry.state is not EnumOneOffState.RETIRED:
                if entry.replacement_kind is EnumReplacementKind.NODE:
                    replacement_found = entry.replacement_ref in node_names
                    if not replacement_found:
                        findings.append(
                            f"replacement_node_missing:{entry.replacement_ref}"
                        )
                if (
                    entry.liveness_process_id
                    and entry.liveness_process_id not in live_ids
                ):
                    findings.append(f"process_unknown:{entry.liveness_process_id}")
            readings.append(
                ModelOneOffReading(
                    one_off_id=entry.one_off_id,
                    state=entry.state,
                    replacement_kind=entry.replacement_kind,
                    replacement_ref=entry.replacement_ref,
                    replacement_found=replacement_found,
                    findings=tuple(findings),
                )
            )

        counts = Counter(entry.state.value for entry in policy.entries)
        state_counts = tuple(
            sorted((state, number) for state, number in counts.items())
        )

        return ModelOneOffRegisterResult(
            correlation_id=request.correlation_id,
            passed=not (unregistered or unknown or missing_nodes),
            readings=tuple(readings),
            unregistered_processes=tuple(sorted(unregistered)),
            unknown_processes=tuple(sorted(unknown)),
            missing_replacement_nodes=tuple(sorted(missing_nodes)),
            unplanned=tuple(sorted(unplanned)),
            bypass_yes=tuple(sorted(bypass_yes)),
            bypass_unread=tuple(sorted(bypass_unread)),
            state_counts=state_counts,
            one_off_count=len(policy.entries),
            exclusion_count=len(policy.exclusions),
        )
