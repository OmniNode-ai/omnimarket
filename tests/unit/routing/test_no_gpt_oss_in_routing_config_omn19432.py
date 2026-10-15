# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: no routing config in the package names the retired gpt-oss model.

gpt-oss-120b left the lab Studio on 2026-10-08; its server serves Qwen3.8-27B.
A config that still names it as a backend model, an alias, a family match or an
allowlist entry routes or labels traffic to a model nothing serves. The check
reads parsed YAML keys and values, so a dated comment recording what once served
a rung is history and does not count.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
_GPT_OSS = re.compile(r"gpt[-_ ]?oss", re.IGNORECASE)


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


def _gpt_oss_values(text: str) -> list[str]:
    return [
        s
        for doc in yaml.safe_load_all(text)
        for s in _strings(doc)
        if _GPT_OSS.search(s)
    ]


def test_detector_finds_every_spelling_in_keys_and_values_but_not_comments() -> None:
    text = (
        "# gpt-oss-120b served this rung before 2026-10-08\n"
        "backends:\n"
        "  - model_name: gpt-oss-120b\n"
        "    aliases: [gpt_oss, 'GPT OSS 20B']\n"
        "gptoss: true\n"
    )
    assert _gpt_oss_values(text) == ["gpt-oss-120b", "gpt_oss", "GPT OSS 20B", "gptoss"]
    assert (
        _gpt_oss_values("# gpt-oss-120b only in a comment\nmodel_name: Qwen3.8-27B\n")
        == []
    )


def test_package_has_yaml_to_scan() -> None:
    assert len(list(_PACKAGE_ROOT.rglob("*.yaml"))) > 100


def test_no_package_yaml_names_gpt_oss() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.yaml")):
        found = _gpt_oss_values(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(_PACKAGE_ROOT))] = found
    assert offenders == {}
