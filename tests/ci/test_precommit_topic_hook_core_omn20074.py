# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20074: no-hardcoded-topics comes from omnibase_core, with its exclude unchanged."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONFIG = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"
CORE_REPO = "https://github.com/OmniNode-ai/omnibase_core"
# sha256 of the exclude string the onex_change_control block carried (773 characters).
EXCLUDE_SHA256 = "7782a602b80955bb8aebd99979e2c23f2ffa96c0c752154d50f8636d097dfde8"


def _repos() -> list[dict[str, object]]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    repos = config["repos"]
    assert isinstance(repos, list)
    return [repo for repo in repos if isinstance(repo, dict)]


def _topic_hooks() -> list[tuple[dict[str, object], dict[str, object]]]:
    found: list[tuple[dict[str, object], dict[str, object]]] = []
    for repo in _repos():
        hooks = repo.get("hooks", [])
        assert isinstance(hooks, list)
        found.extend(
            (repo, hook)
            for hook in hooks
            if isinstance(hook, dict) and hook.get("id") == "no-hardcoded-topics"
        )
    return found


def test_the_topic_hook_is_one_block_sourced_from_omnibase_core() -> None:
    found = _topic_hooks()
    assert len(found) == 1
    repo, _ = found[0]
    assert repo["repo"] == CORE_REPO
    rev = repo["rev"]
    assert isinstance(rev, str)
    assert re.fullmatch(r"[0-9a-f]{40}", rev)


def test_the_topic_hook_keeps_its_exclude_and_settings() -> None:
    _, hook = _topic_hooks()[0]
    exclude = hook["exclude"]
    assert isinstance(exclude, str)
    assert hashlib.sha256(exclude.encode()).hexdigest() == EXCLUDE_SHA256
    assert hook["require_serial"] is True
    assert hook["stages"] == ["pre-commit"]
    assert set(hook) == {"id", "require_serial", "stages", "exclude"}


def test_no_onex_change_control_block_remains() -> None:
    urls = [repo["repo"] for repo in _repos()]
    assert not [
        url
        for url in urls
        if isinstance(url, str) and url.endswith("onex_change_control")
    ]
