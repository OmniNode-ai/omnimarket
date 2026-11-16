# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The glm-4.5 alias and the glm-5.3-flash contract id name each other (OMN-19127).

The thread-reply registry keys the cloud-glm backend by "glm-4.5" while the
delegation contract declares that backend as "glm-5.3-flash". The two are read
by different paths; a reader of either must find the other, so a change to one
does not read as a typo in the other. Config text only: no network I/O.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_CONFIGS = Path(__file__).resolve().parents[3] / "src/omnimarket/configs"
_NAMES = _CONFIGS / "node_model_names.v1.yaml"
_BIFROST = _CONFIGS / "bifrost_delegation.yaml"


def _sites_missing_the_cross_reference(names_text: str, bifrost_text: str) -> list[str]:
    missing: list[str] = []
    if "OMN-19127" not in names_text or "glm-5.3-flash" not in names_text:
        missing.append("node_model_names.v1.yaml")
    if "OMN-19127" not in bifrost_text or "glm-4.5" not in bifrost_text:
        missing.append("bifrost_delegation.yaml")
    return missing


def test_the_two_spellings_still_differ() -> None:
    """The note is only needed while they differ; drop it if they converge."""
    names = yaml.safe_load(_NAMES.read_text(encoding="utf-8"))
    alias = names["nodes"]["node_thread_reply_effect"]["glm_model"]
    bifrost = yaml.safe_load(_BIFROST.read_text(encoding="utf-8"))
    contract = next(
        b["model_name"] for b in bifrost["backends"] if b["backend_id"] == "cloud-glm"
    )

    assert (alias, contract) == ("glm-4.5", "glm-5.3-flash")


def test_both_sites_carry_the_cross_reference() -> None:
    assert (
        _sites_missing_the_cross_reference(
            _NAMES.read_text(encoding="utf-8"), _BIFROST.read_text(encoding="utf-8")
        )
        == []
    )


def test_the_detector_flags_a_site_with_no_note() -> None:
    """Failing control: a site without the note is reported."""
    assert _sites_missing_the_cross_reference("glm_model: glm-4.5", "") == [
        "node_model_names.v1.yaml",
        "bifrost_delegation.yaml",
    ]
