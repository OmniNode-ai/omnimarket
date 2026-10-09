# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment half of the morning friction sweep: briefs, paths and bounds.

The node holds the method (phase order, idempotency verdicts, delegation
evidence and premise-audit rules, bus wiring). Everything that names a
deployment's workspace, repositories, report locations, lanes, tickets or
tooling arrives in this overlay.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

REQUIRED_TEMPLATES = (
    "friction-precheck",
    "friction-scan",
    "friction-source-linear",
    "friction-source-checkpoints",
    "friction-source-ci",
    "friction-source-guards",
    "friction-synthesize",
    "friction-adjudicate",
    "friction-report",
)
REQUIRED_VALUES = (
    "state_path",
    "report_path",
    "ledger_path",
    "kb_prefix",
    "kb_branch",
    "worktree_ticket",
    "watermark_tool",
    "watermark_schema_version",
    "peer_claim_minutes",
    "pr_watcher_max_age_minutes",
    "linear_comments_max_age_hours",
    "occurrence_comment_ceiling",
    "checkpoints_max_age_hours",
    "ci_lookback_max_age_hours",
    "guard_logs_max_age_hours",
)
REQUIRED_TEXTS = (
    "adjudication_dry",
    "adjudication_live",
    "report_dry",
    "report_live",
)
REQUIRED_LISTS = ("phase_stems", "ci_repos", "premise_control_lanes")
TEMPLATE_BLOCK = re.compile(r"<!-- prompt:(.*?) -->\n(.*?)\n<!-- end:\1 -->", re.S)


class ModelFrictionOverlay(BaseModel):
    """Validated deployment overlay; built by the loader, never by the node."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    templates: dict[str, str]
    values: dict[str, str]
    texts: dict[str, str]
    lists: dict[str, list[str]]

    @classmethod
    def from_documents(cls, overlay: object, briefs: str) -> ModelFrictionOverlay:
        """Validate the parsed overlay mapping and the briefs text."""
        if not isinstance(overlay, dict):
            raise ValueError("overlay must be a mapping")
        unknown = set(overlay) - {"briefs", "values", "texts", *REQUIRED_LISTS}
        if unknown:
            raise ValueError(
                "overlay unknown keys: " + ", ".join(sorted(map(str, unknown)))
            )
        templates = dict(TEMPLATE_BLOCK.findall(briefs))
        absent = [name for name in REQUIRED_TEMPLATES if name not in templates]
        if absent:
            raise ValueError("briefs missing templates: " + ", ".join(absent))
        extra = set(templates) - set(REQUIRED_TEMPLATES)
        if extra:
            raise ValueError("briefs unknown templates: " + ", ".join(sorted(extra)))
        lists: dict[str, list[str]] = {}
        for key in REQUIRED_LISTS:
            raw = overlay.get(key)
            if (
                not isinstance(raw, list)
                or not raw
                or not all(isinstance(v, str) and v.strip() for v in raw)
            ):
                raise ValueError(f"overlay {key} must be a nonempty list of strings")
            lists[key] = list(raw)
        return cls(
            templates=templates,
            values=_exact_strings(overlay.get("values"), REQUIRED_VALUES, "value"),
            texts=_exact_strings(overlay.get("texts"), REQUIRED_TEXTS, "text"),
            lists=lists,
        )


def _exact_strings(raw: object, required: tuple[str, ...], noun: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"overlay {noun}s must be a mapping")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"overlay missing {noun}s: " + ", ".join(missing))
    extra = set(raw) - set(required)
    if extra:
        raise ValueError(
            f"overlay unknown {noun}s: " + ", ".join(sorted(map(str, extra)))
        )
    for key, item in raw.items():
        if not isinstance(item, str):
            raise ValueError(f"overlay {noun} {key} must be a string")
    return {str(k): str(v) for k, v in raw.items()}
