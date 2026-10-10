# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneLivenessDropAlert: a dropped lane becomes a Slack command.

Only DROPPED alerts. UNOBSERVABLE and UNKNOWN_RELAY_SILENT are the compute
node's refusals to conclude, and alerting on them would turn a relay outage into
a page about every lane.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.models.liveness.model_lane_liveness import ModelLaneLivenessReport
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_lane_liveness_drop_alert_effect.models import (
    ModelLaneDropSlackCommand,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"


class ModelLaneDropAlertConfig(BaseModel):
    """The ``config.lane_liveness_drop_alert`` block of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    renotify_after_seconds: int = Field(..., gt=0)


@lru_cache(maxsize=1)
def alert_config() -> ModelLaneDropAlertConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelLaneDropAlertConfig.model_validate(
        data["config"]["lane_liveness_drop_alert"]
    )


def _resolve_channel() -> str:
    ref = contract_secret_ref(_CONTRACT, _CHANNEL_SECRET)
    secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"secret ref {ref!r} resolved to None; the drop alert has no channel "
            "to post to and will not guess one"
        )
    return secret.get_secret_value()


class HandlerLaneLivenessDropAlert:
    """EFFECT handler: evaluation in, Slack command out when a lane dropped."""

    def __init__(
        self,
        *,
        channel_resolver: Callable[[], str] | None = None,
        config: ModelLaneDropAlertConfig | None = None,
    ) -> None:
        self._cfg = config or alert_config()
        self._channel = channel_resolver or _resolve_channel

    def handle(
        self, request: ModelLaneLivenessReport
    ) -> ModelLaneDropSlackCommand | None:
        dropped = request.dropped
        if not dropped:
            return None
        lanes = sorted(v.lane for v in dropped)
        bucket = int(request.window_end.timestamp()) // self._cfg.renotify_after_seconds
        digest = hashlib.sha256("|".join(lanes).encode("utf-8")).hexdigest()[:16]
        lines = [
            f":rotating_light: *LANE DROPPED* — {len(lanes)} lane(s) silent while "
            "the relay was carrying traffic"
        ]
        lines.extend(
            f"`{v.lane}`: {v.reason} (silent {v.silent_seconds}s, "
            f"{v.hook_event_count} events)"
            for v in sorted(dropped, key=lambda v: v.lane)
        )
        lines.append(
            f"window {request.window_start.isoformat()} → {request.window_end.isoformat()}"
        )
        return ModelLaneDropSlackCommand(
            channel=self._channel(),
            text="\n".join(lines),
            idempotency_key=f"lane-drop|{digest}|{bucket}",
            correlation_id=uuid4(),
        )


__all__: list[str] = [
    "HandlerLaneLivenessDropAlert",
    "ModelLaneDropAlertConfig",
    "alert_config",
]
