# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator's model restriction for autonomous background lanes."""

BACKGROUND_MODEL_RULING = (
    "RULING 2026-10-26T11:31:48Z lane=orchestrator-31d9 (OMN-17427)"
)
OPUS_REFUSAL = (
    f"background-opus-disabled: {BACKGROUND_MODEL_RULING}; "
    "use Sonnet for Claude workflows and Codex/GLM for other work"
)


def is_opus(model: str) -> bool:
    """Recognize the model and host-engine spellings accepted by lane producers."""
    return model.strip().lower().replace("_", "-") in {"opus", "claude-opus"}
