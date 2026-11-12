# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join a lane's name to the agent that ran it (OMN-19998).

A Workflow lane's agent carries its dispatch description in the agent span: the lane's name without its trailing
``-HHMM`` (description ``hook-telemetry-gaps``, lane ``hook-telemetry-gaps-2345``). The Bash call that holds a ledger
stamp belongs to the forked ledger-skill executor beneath the lane, so a join by stamp names the executor: 17 of 26
stamp-joined agents on 2026-11-05 were such executors with three calls, not the lane's own hundred.

A lane is joined only when exactly one agent fits its name and starts within the window of its CLAIM, and that agent
fits no other lane. Two runs of one description inside the window, or two lanes fitting one agent, stay unjoined with
the reason. Nothing is guessed. The request also carries the host of every event, and the result counts them, so a
reader can name which hosts reported and which sent nothing.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from ..models import (
    ModelHookHostCount,
    ModelHookLaneJoin,
    ModelHookLaneJoinAgent,
    ModelHookLaneJoinRequest,
    ModelHookLaneJoinResult,
    ModelHookLaneSilentHost,
    ModelHookLaneUnjoined,
    ModelHookLaneUnmatchedAgent,
)

Key = tuple[str, str | None]


def _key(agent: ModelHookLaneJoinAgent) -> Key:
    return (agent.session_id, agent.agent_id)


class HandlerHookLaneJoin:
    """Pure: the same request gives the same result."""

    def handle(self, request: ModelHookLaneJoinRequest) -> ModelHookLaneJoinResult:
        window = timedelta(seconds=request.name_window_seconds)
        patterns = [
            (agent, re.compile(re.escape(agent.description) + r"-\d{4}"))
            for agent in request.agents
            if agent.description
        ]
        fits: dict[str, list[ModelHookLaneJoinAgent]] = {}
        unjoined: list[ModelHookLaneUnjoined] = []
        for lane in request.lanes:
            if lane.claim_at is None:
                unjoined.append(
                    ModelHookLaneUnjoined(
                        lane=lane.name, reason="no CLAIM in the window"
                    )
                )
                continue
            found = [
                agent
                for agent, pattern in patterns
                if pattern.fullmatch(lane.name)
                and lane.claim_at - window
                <= agent.first_event_at
                <= lane.claim_at + window
            ]
            if not found:
                unjoined.append(
                    ModelHookLaneUnjoined(
                        lane=lane.name, reason="no agent description names the lane"
                    )
                )
            else:
                fits[lane.name] = found
        owners = Counter(_key(agent) for found in fits.values() for agent in found)
        matched_keys = set()
        for found in fits.values():
            if len(found) == 1:
                matched_keys.add(_key(found[0]))
        unmatched_agents = tuple(
            ModelHookLaneUnmatchedAgent(
                session_id=agent.session_id,
                agent_id=agent.agent_id,
                reason="no lane name fits the agent description",
            )
            for agent in request.agents
            if _key(agent) not in matched_keys
        )
        expected_hosts = set(request.expected_hosts)
        silent_hosts = tuple(
            ModelHookLaneSilentHost(host=host)
            for host in sorted(expected_hosts - set(counts))
        )
        joins: list[ModelHookLaneJoin] = []
        for name, found in fits.items():
            if len(found) > 1:
                unjoined.append(
                    ModelHookLaneUnjoined(
                        lane=name,
                        reason=f"{len(found)} agents fit the name and start time",
                    )
                )
            elif owners[_key(found[0])] > 1:
                unjoined.append(
                    ModelHookLaneUnjoined(
                        lane=name, reason="its agent fits another lane too"
                    )
                )
            else:
                joins.append(
                    ModelHookLaneJoin(
                        lane=name,
                        session_id=found[0].session_id,
                        agent_id=found[0].agent_id,
                    )
                )
        counts = Counter(
            host or request.unstamped_label for host in request.event_hosts
        )
        hosts = tuple(
            ModelHookHostCount(host=host, events=n)
            for host, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        return ModelHookLaneJoinResult(
            unmatched_agents=unmatched_agents,
            silent_hosts=silent_hosts,
            joins=tuple(joins), unjoined=tuple(unjoined), hosts=hosts
        )
