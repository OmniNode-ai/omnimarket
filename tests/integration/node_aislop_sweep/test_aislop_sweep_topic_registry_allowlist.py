# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural allowlist-broadening tests for hardcoded-topics (OMN-13905 Part-1).

Prior to this change, ``NodeAislopSweep._check_hardcoded_topics`` skipped a
file only when its *path* contained ``contract.yaml`` or ``enum`` — a blanket
file/path skip that both (a) false-flagged canonical StrEnum/flat-constant
topic registries such as ``omnibase_core.topics`` and
``omnibase_infra.topics.platform_topic_suffixes`` (whose paths do not contain
"enum"), and (b) would have gone blind to any stray literal dropped into a
file that merely had "enum" somewhere in its path.

The fix recognizes canonical registry membership *structurally*:

1. Topic literals bound as ``StrEnum``/``Enum`` class members (the shape of
   every real topics.py registry in this codebase).
2. Per-line suppression markers already sanctioned and honored by sibling
   checkers (``onex-topic-allow:``, ``onex-topic-sot``,
   ``onex-topic-test-fixture``, ``onex-topic-doc-example``,
   ``arch-topic-naming``).
3. Module-level constant declarations in a file that self-declares as topic
   source-of-truth via a top-of-file ``# onex-topic-sot`` marker (covers flat
   module-constant registries like platform_topic_suffixes.py that don't use
   an enum wrapper).

Every case below asserts concrete finding structure (not just "no raise"),
and each negative-control case proves the guard has NOT gone blind: a bare
literal with none of the above structural markers is still flagged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)


def _write(tree: Path, rel: str, content: str) -> None:
    target = tree / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _run(target: str, integration_event_bus: Any) -> Any:
    return NodeAislopSweep(event_bus=integration_event_bus).handle(
        AislopSweepRequest(target_dirs=[target], checks=["hardcoded-topics"])
    )


# ---------------------------------------------------------------------------
# (a) canonical registry constants are NO LONGER flagged
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_strenum_registry_member_not_flagged(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """A StrEnum topic-registry member (TopicBase-style) is not a violation."""
    _write(
        tmp_path,
        "src/topics.py",
        (
            "from enum import StrEnum\n\n\n"
            "class TopicBase(StrEnum):\n"
            '    SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"\n'
            '    SESSION_ENDED = "onex.evt.omniclaude.session-ended.v1"\n'
        ),
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "clean"
    assert result.total_findings == 0


@pytest.mark.integration
def test_str_enum_base_registry_member_not_flagged(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """A ``class X(str, Enum)`` registry member (GovernanceTopic-style) is fine."""
    _write(
        tmp_path,
        "src/kafka_topics.py",
        (
            "from enum import Enum, unique\n\n\n"
            "@unique\n"
            "class GovernanceTopic(str, Enum):\n"
            "    GOVERNANCE_CHECK_COMPLETED = (\n"
            '        "onex.evt.onex-change-control.governance-check-completed.v1"\n'
            "    )\n"
        ),
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "clean"
    assert result.total_findings == 0


@pytest.mark.integration
def test_sot_module_level_constant_not_flagged(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """A module-level constant registry (platform_topic_suffixes.py-style),
    self-declared via a top-of-file ``# onex-topic-sot`` marker, is not
    flagged — including its parenthesized multi-line form.
    """
    _write(
        tmp_path,
        "src/platform_topic_suffixes.py",
        (
            "# onex-topic-sot: canonical topic-suffix registry.\n"
            '"""Platform topic suffixes."""\n\n'
            'SUFFIX_NODE_REGISTRATION: str = "onex.evt.platform.node-registration.v1"\n\n'
            "SUFFIX_REGISTRY_REQUEST_INTROSPECTION: str = (\n"
            '    "onex.evt.platform.registry-request-introspection.v1"\n'
            ")\n"
        ),
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "clean"
    assert result.total_findings == 0


@pytest.mark.integration
def test_topic_allow_marker_exempts_single_line(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """The pre-existing ``# onex-topic-allow:`` marker (already honored by
    ValidatorHardcodedTopics / check_no_hardcoded_topics.py) is now also
    honored by the aislop-sweep hardcoded-topics check.
    """
    _write(
        tmp_path,
        "src/handler_x.py",
        'TOPIC_X = "onex.evt.omnimarket.x-completed.v1"  # onex-topic-allow: pending contract auto-wiring\n',
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "clean"
    assert result.total_findings == 0


@pytest.mark.integration
def test_arch_topic_naming_marker_exempts_single_line(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """The pre-existing ``arch-topic-naming`` marker (used for base-prefix /
    topic-shape exceptions) is honored.
    """
    _write(
        tmp_path,
        "src/topics.py",
        'AGENT_INBOX_DIRECTED_BASE: str = "onex.evt.omniclaude.agent-inbox"  # arch-topic-naming: base prefix\n',
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "clean"
    assert result.total_findings == 0


# ---------------------------------------------------------------------------
# (b) the guard must NOT go blind — genuinely stray literals are still caught
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_bare_module_constant_without_sot_header_still_flagged(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """A module-level constant with no enum wrapper, no marker, and no
    ``onex-topic-sot`` header is a genuinely stray hardcoded topic and must
    still be flagged (matches the pre-existing negative control).
    """
    _write(tmp_path, "src/topics.py", 'TOPIC = "onex.evt.core.something.v1"\n')
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "findings"
    assert result.total_findings == 1
    assert result.by_check.get("hardcoded-topics", 0) == 1
    assert result.by_severity.get("ERROR", 0) == 1


@pytest.mark.integration
def test_inline_literal_in_handler_call_still_flagged(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """A topic literal embedded directly in a call (not a named constant
    declaration, not an enum member) is still flagged even with no markers.
    """
    _write(
        tmp_path,
        "src/handler_bad.py",
        (
            "def publish(bus):\n"
            '    bus.publish(topic="onex.evt.omnimarket.stray-thing.v1")\n'
        ),
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "findings"
    assert result.total_findings == 1


@pytest.mark.integration
def test_inline_literal_inside_sot_module_still_flagged(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """A ``# onex-topic-sot`` header exempts module-level constant
    *declarations* in that file, but does NOT blind the guard to a stray
    literal embedded inline (e.g. inside a function body) in the same file.
    """
    _write(
        tmp_path,
        "src/platform_topic_suffixes.py",
        (
            "# onex-topic-sot: canonical topic-suffix registry.\n"
            'SUFFIX_OK: str = "onex.evt.platform.node-registration.v1"\n\n'
            "def build_stray():\n"
            '    return "onex.evt.platform.not-a-constant.v1"\n'
        ),
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "findings"
    assert result.total_findings == 1
    assert "not-a-constant" in result.findings[0].message


@pytest.mark.integration
def test_enum_body_exemption_does_not_leak_past_class_end(
    tmp_path: Path, integration_event_bus: Any
) -> None:
    """A stray literal declared AFTER a StrEnum class ends (back at module
    scope) is still flagged — the enum-body tracker must correctly close at
    the dedent, not leak exemption to the rest of the file.
    """
    _write(
        tmp_path,
        "src/topics.py",
        (
            "from enum import StrEnum\n\n\n"
            "class TopicBase(StrEnum):\n"
            '    SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"\n\n\n'
            'STRAY_AFTER_ENUM = "onex.evt.omniclaude.stray-after-enum.v1"\n'
        ),
    )
    result = _run(str(tmp_path), integration_event_bus)
    assert result.status == "findings"
    assert result.total_findings == 1
    assert "stray-after-enum" in result.findings[0].message
