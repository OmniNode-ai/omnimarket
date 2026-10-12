# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment half of the Linear-comments sweep: briefs, paths and standing rules.

The node holds the method (phase order, the decisions, the held tally, the bus
wiring). Everything that names a deployment's report directory, metrics file,
tracker tooling or standing rules arrives in this overlay.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

REQUIRED_TEMPLATES = (
    "linear-comments-collect",
    "linear-comments-verify",
    "linear-comments-write",
)
REQUIRED_VALUES = ("report_dir", "metrics_path", "rules")
TEMPLATE_BLOCK = re.compile(r"<!-- prompt:(.*?) -->\n(.*?)\n<!-- end:\1 -->", re.S)


class ModelSweepOverlay(BaseModel):
    """Validated deployment overlay; built by the loader, never by the node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    templates: dict[str, str]
    values: dict[str, str]

    @classmethod
    def from_documents(cls, overlay: object, briefs: str) -> ModelSweepOverlay:
        """Validate the parsed overlay mapping and the briefs text."""
        if not isinstance(overlay, dict):
            raise ValueError("overlay must be a mapping")
        unknown = set(overlay) - {"briefs", "values"}
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
        values = overlay.get("values")
        if not isinstance(values, dict):
            raise ValueError("overlay values must be a mapping")
        missing = [key for key in REQUIRED_VALUES if key not in values]
        if missing:
            raise ValueError("overlay missing values: " + ", ".join(missing))
        surplus = set(values) - set(REQUIRED_VALUES)
        if surplus:
            raise ValueError(
                "overlay unknown values: " + ", ".join(sorted(map(str, surplus)))
            )
        for key, item in values.items():
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"overlay value {key} must be a nonempty string")
        return cls(
            templates=templates, values={str(k): str(v) for k, v in values.items()}
        )
