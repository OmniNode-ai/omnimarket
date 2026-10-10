# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment half of the morning process: briefs, artifacts and defaults.

The node holds the method (phase order, idempotency verdicts, delegation
evidence rules, bus wiring). Everything that names a deployment's workspace,
repositories, report locations, tickets or tooling arrives in this overlay.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, JsonValue

REQUIRED_TEMPLATES = (
    "KB_ROOT",
    "KB_WORKTREE",
    "KB_COMMIT",
    "DOC_RESOLUTION",
    "COMMON",
    "DELEGATION_STEP",
    "PRECHECK_BRIEF",
    "idempotency-precheck",
    "decisions-register",
    "ground-state",
    "morning-triage",
    "plan-reconcile",
    "integration-plan",
    "dropped-work",
    "session-goal",
)
REQUIRED_VALUES = (
    "GROUND_STATE_PATH",
    "REBASELINE_PATH",
    "INTEGRATION_PATH",
    "DROPPED_WORK_PATH",
    "GOAL_PATH",
    "SNAPSHOT_PATH",
    "DECISIONS_MD_PATH",
    "DECISIONS_JSON_PATH",
    "LEDGER_PATH",
    "WORKTREE_TICKET",
    "KB_BRANCH",
    "DECISIONS_STALE_DAYS",
    "PEER_CLAIM_MINUTES",
)
REQUIRED_TEXTS = (
    "triage_note_path",
    "kb_resolution_supplied",
    "kb_resolution_unset",
    "publish_key",
    "publish",
    "dry",
    "goal_publish_marker",
    "goal_dry_suffix",
    "ledger_reconcile_command",
)
TEMPLATE_BLOCK = re.compile(r"<!-- prompt:(.*?) -->\n(.*?)\n<!-- end:\1 -->", re.S)


class ModelMorningRequestDefaults(BaseModel):
    """What a request that omits a field falls back to."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_docs: list[str]
    must_do_doc: str
    off_rails_doc: str
    integration_repos: list[str]
    closure_probe_ticket: str


class ModelMorningOverlay(BaseModel):
    """Validated deployment overlay; built by the loader, never by the node."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    templates: dict[str, str]
    phase_specs: list[dict[str, JsonValue]]
    request_defaults: ModelMorningRequestDefaults
    values: dict[str, str]
    texts: dict[str, str]

    @classmethod
    def from_documents(
        cls, overlay: object, briefs: str, phase_specs: object
    ) -> ModelMorningOverlay:
        """Validate the parsed overlay mapping, the briefs text and the phase specs."""
        if not isinstance(overlay, dict):
            raise ValueError("overlay must be a mapping")
        unknown = set(overlay) - {
            "briefs",
            "phase_specs",
            "request_defaults",
            "values",
            "texts",
        }
        if unknown:
            raise ValueError(
                "overlay unknown keys: " + ", ".join(sorted(map(str, unknown)))
            )
        values = _exact_strings(overlay.get("values"), REQUIRED_VALUES, "value")
        texts = _exact_strings(overlay.get("texts"), REQUIRED_TEXTS, "text")
        templates = dict(TEMPLATE_BLOCK.findall(briefs))
        absent = [name for name in REQUIRED_TEMPLATES if name not in templates]
        if absent:
            raise ValueError("briefs missing templates: " + ", ".join(absent))
        if not isinstance(phase_specs, list) or not all(
            isinstance(s, dict) for s in phase_specs
        ):
            raise ValueError("phase_specs must be a list of mappings")
        for spec in phase_specs:
            for key in ("phase", "lanes", "artifact", "conformance"):
                if key not in spec:
                    raise ValueError(f"phase_specs entry missing {key}")
        try:
            defaults = ModelMorningRequestDefaults.model_validate(
                overlay.get("request_defaults")
            )
        except ValueError as exc:
            raise ValueError(
                f"request_defaults invalid ({type(exc).__name__})"
            ) from None
        return cls(
            templates=templates,
            phase_specs=phase_specs,
            request_defaults=defaults,
            values=values,
            texts=texts,
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
