# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the no-hardcoded-topics CI gate (OMN-10909)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_no_hardcoded_topics import main, scan

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


@pytest.mark.unit
def test_scan_flags_bare_topic_literal(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/handlers/handler_x.py",
        'TOPIC = "onex.' + 'cmd.omnimarket.foo.v1"\n',
    )
    violations = scan(tmp_path)
    assert len(violations) == 1
    assert "handler_x.py" in violations[0]


@pytest.mark.unit
def test_scan_allows_topics_py_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/topics.py",
        'TOPIC = "onex.' + 'cmd.omnimarket.foo.v1"\n',
    )
    assert scan(tmp_path) == []


@pytest.mark.unit
def test_scan_allows_test_files(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/test_handler_x.py",
        'EXPECTED = "onex.' + 'evt.omnimarket.foo.v1"\n',
    )
    assert scan(tmp_path) == []


@pytest.mark.unit
def test_scan_allows_inline_annotation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/handlers/handler_x.py",
        'TOPIC = "onex.'
        + 'cmd.omnimarket.foo.v1"  # onex-topic-allow: pending wiring\n',
    )
    assert scan(tmp_path) == []


@pytest.mark.unit
def test_scan_allows_dynamic_fstring(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/handlers/handler_x.py",
        'def t(k: str) -> str:\n    return f"onex.' + 'evt.omnimarket.{k}.v1"\n',
    )
    assert scan(tmp_path) == []


@pytest.mark.unit
def test_scan_only_allows_braces_inside_the_fstring_token(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/handlers/handler_x.py",
        'PATTERN = "{}"\nTOPIC = f"onex.' + 'evt.omnimarket.static.v1"\n',
    )
    violations = scan(tmp_path)
    assert len(violations) == 1
    assert "static.v1" in violations[0]


@pytest.mark.unit
def test_scan_allows_startswith_prefix_probe(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/handlers/handler_x.py",
        'def t(topic: str) -> bool:\n    return topic.startswith("onex.'
        + 'evt.omniclaude.")\n',
    )
    assert scan(tmp_path) == []


@pytest.mark.unit
def test_scan_skips_docstring_and_comment(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_x/handlers/handler_x.py",
        '"""Example: "onex.' + 'evt.omnimarket.foo.v1"."""\n'
        '# also "onex.' + 'cmd.omnimarket.bar.v1"\n',
    )
    assert scan(tmp_path) == []


@pytest.mark.unit
def test_main_passes_on_current_repo() -> None:
    """The live src/omnimarket/ tree must be clean under this gate."""
    assert main() == 0
