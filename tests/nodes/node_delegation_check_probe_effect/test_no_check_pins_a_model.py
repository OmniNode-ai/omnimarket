# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No delegation check names one model (OMN-26043): the responder chain's served model is the only model a run knows.

A check sends its task through the responder chain the settings store declares, so no source file of the node, of the chain
module or of the responders document model may name a model family. The control proves the scan can fire.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCES = (
    ROOT / "src/omnimarket/nodes/node_delegation_check_probe_effect",
    ROOT / "src/omnimarket/handlers/routing/delegation_check_responder_chain.py",
    ROOT
    / "src/omnimarket/models/overlay_documents/model_delegation_check_responders_overlay.py",
)
MODEL_FAMILIES = re.compile(
    r"inkling|qwen|gpt-?[0-9o]|glm-?[0-9]|gemini-?[0-9]|mistral|claude-(?:opus|sonnet|haiku)|llama-?[0-9]",
    re.IGNORECASE,
)


def files() -> list[Path]:
    found: list[Path] = []
    for source in SOURCES:
        if source.is_dir():
            found.extend(
                p
                for p in sorted(source.rglob("*"))
                if p.suffix in {".py", ".yaml", ".sh"} and "__pycache__" not in p.parts
            )
        else:
            found.append(source)
    return found


def pinned_models(text: str) -> list[str]:
    return [m.group(0) for m in MODEL_FAMILIES.finditer(text)]


def test_the_scan_finds_a_named_model() -> None:
    assert pinned_models('model = "Qwen3-27B"') == ["Qwen"]
    assert pinned_models("claude-sonnet-5-5") == ["claude-sonnet"]


def test_the_scan_reads_the_node_sources() -> None:
    assert len(files()) >= 8


def test_no_source_of_the_checks_names_a_model() -> None:
    offenders = {
        str(p.relative_to(ROOT)): hits
        for p in files()
        if (hits := pinned_models(p.read_text(encoding="utf-8")))
    }
    assert offenders == {}
