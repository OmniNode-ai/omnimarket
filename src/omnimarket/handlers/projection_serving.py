# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply a contract-owned pure health judgement to copies of a served page."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from typing import Any

from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.models.model_local_service_health import (
    ModelLocalServiceHealth,
    ModelLocalServiceVerdict,
)


class ServeTimeJudgementError(Exception):
    """A declared judgement could not resolve or produce a complete valid page."""


def judge_served_rows(
    path: str, rows: list[dict[str, Any]], observed_at: datetime
) -> list[dict[str, Any]]:
    """Resolve the declared callable on each read; never mutate source rows.

    The dotted path is trusted contract configuration, never a request field.
    Every error is wrapped so the public read can give one fixed refusal without
    leaking exception details or falling back to stored healthy verdicts.
    """
    try:
        module_name, attribute = path.rsplit(".", 1)
        judge = getattr(import_module(module_name), attribute)
        services = tuple(
            ModelLocalServiceHealth.model_validate(
                {field: row[field] for field in ModelLocalServiceHealth.model_fields}
            )
            for row in rows
        )
        verdicts, _ = judge(services, observed_at, load_local_alert_rules())
        if len(verdicts) != len(rows):
            raise ValueError("incomplete judgement")
        result = []
        for row, service, raw_verdict in zip(rows, services, verdicts, strict=True):
            verdict = ModelLocalServiceVerdict.model_validate(raw_verdict)
            if (verdict.tenant_id, verdict.mode, verdict.service) != (
                service.tenant_id,
                service.mode,
                service.service,
            ):
                raise ValueError("judgement identity mismatch")
            wire = verdict.model_dump(mode="json")
            result.append(
                {
                    **row,
                    "verdict": wire["verdict"],
                    "verdict_reason": wire["reason"],
                    "alert_code": wire["alert_code"],
                    "alert_severity": wire["alert_severity"],
                    "alert_since": (
                        None
                        if verdict.alert_since is None
                        else verdict.alert_since.isoformat()
                    ),
                }
            )
        return result
    except Exception as exc:
        raise ServeTimeJudgementError("serve-time judgement failed") from exc
