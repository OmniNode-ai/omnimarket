# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep range observations off every floor refresh without discarding them.

Small series retain their existing wire format. A large series is represented by
its daily values and summaries, plus a separate gzip file containing the entire
original document. Only the detailed Process page downloads that file. The source
and summary bounds are declared in the publishing node's contract.
"""

from __future__ import annotations

import gzip
import json
from functools import cache
from pathlib import Path

import yaml

from omnimarket.nodes.node_factory_floor_publish_effect.models.model_process_health_payload import (
    ModelProcessHealthPayloadPolicy,
    ModelProcessHealthPayloadRequest,
    ModelProcessHealthPayloadResult,
)


@cache
def load_payload_policy() -> ModelProcessHealthPayloadPolicy:
    path = Path(__file__).resolve().parents[1] / "contract.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelProcessHealthPayloadPolicy.model_validate(
        document["runtime"]["process_health_payload"]
    )


class HandlerProcessHealthPayload:
    """Read once, cap allocation, and produce the two representations together."""

    def handle(
        self, request: ModelProcessHealthPayloadRequest
    ) -> ModelProcessHealthPayloadResult:
        policy = load_payload_policy()
        path = Path(request.source_path)
        try:
            if path.stat().st_size > policy.max_source_bytes:
                return ModelProcessHealthPayloadResult(
                    reason="process-health source exceeds its byte limit"
                )
            with path.open("rb") as source:
                raw = source.read(policy.max_source_bytes + 1)
            if len(raw) > policy.max_source_bytes:
                return ModelProcessHealthPayloadResult(
                    reason="process-health source exceeds its byte limit"
                )
            record = json.loads(raw)
        except OSError as exc:
            return ModelProcessHealthPayloadResult(
                reason=f"not readable ({type(exc).__name__})"
            )
        except ValueError:
            return ModelProcessHealthPayloadResult(reason="not JSON")
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("generated_at"), str)
            or not isinstance(record.get("days"), list)
            or not isinstance(record.get("summary"), dict)
        ):
            return ModelProcessHealthPayloadResult(reason="not a process-health series")
        if len(raw) <= policy.max_summary_bytes:
            return ModelProcessHealthPayloadResult(summary=raw)
        if not isinstance(record.get("range_overlay"), dict):
            return ModelProcessHealthPayloadResult(
                reason="large process-health series has no range overlay"
            )
        summary = {
            key: value for key, value in record.items() if key != "range_overlay"
        }
        summary["detail_file"] = policy.detail_file
        summary_bytes = json.dumps(summary, separators=(",", ":")).encode()
        if len(summary_bytes) > policy.max_summary_bytes:
            return ModelProcessHealthPayloadResult(
                reason="process-health summary exceeds its byte limit"
            )
        detail = gzip.compress(
            json.dumps(record, separators=(",", ":")).encode(), compresslevel=6, mtime=0
        )
        return ModelProcessHealthPayloadResult(
            summary=summary_bytes, detail=detail, detail_file=policy.detail_file
        )
