# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for PlaybookRegistry and alert_remediation_playbooks.yaml — OMN-8887."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from omnimarket.nodes.node_monitor_alert_responder.models.model_alert_event import (
    ModelAlertEvent,
)
from omnimarket.nodes.node_monitor_alert_responder.playbook_registry import (
    Playbook,
    PlaybookRegistry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_event(**kwargs: object) -> ModelAlertEvent:
    defaults: dict[str, object] = {
        "alert_id": "pb-test-001",
        "source": "omninode-runtime",
        "severity": "ERROR",
        "pattern_matched": "generic_error",
        "container": "omninode-runtime",
        "full_message_text": "Something went wrong",
        "detected_at": "2026-04-15T12:00:00+00:00",
        "host": "omni-host",
    }
    defaults.update(kwargs)
    return ModelAlertEvent(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def registry() -> PlaybookRegistry:
    return PlaybookRegistry.load()


# ---------------------------------------------------------------------------
# PlaybookRegistry.load()
# ---------------------------------------------------------------------------


class TestRegistryLoad:
    def test_loads_at_least_one_playbook(self, registry: PlaybookRegistry) -> None:
        assert len(registry) > 0

    def test_all_playbooks_have_ids(self, registry: PlaybookRegistry) -> None:
        for pb in registry.playbooks:
            assert pb.id, f"Playbook missing id: {pb}"

    def test_all_playbooks_have_keywords(self, registry: PlaybookRegistry) -> None:
        for pb in registry.playbooks:
            assert len(pb.pattern_keywords) > 0, f"Playbook {pb.id} has no keywords"

    def test_all_playbooks_have_dispatch_prompt(
        self, registry: PlaybookRegistry
    ) -> None:
        for pb in registry.playbooks:
            assert pb.dispatch_prompt.strip(), (
                f"Playbook {pb.id} has empty dispatch_prompt"
            )

    def test_graceful_degradation_on_bad_path(self) -> None:
        r = PlaybookRegistry.load(Path("/nonexistent/path.yaml"))
        assert len(r) == 0

    def test_load_from_custom_yaml(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""
            playbooks:
              - id: test-pb
                title: "Test Playbook"
                pattern_keywords: [test_pattern]
                severity_scope: [ERROR]
                dispatch_prompt: "Fix {container} on {host}"
                dod_evidence: ["Container healthy"]
        """)
        p = tmp_path / "playbooks.yaml"
        p.write_text(yaml_content)
        r = PlaybookRegistry.load(p)
        assert len(r) == 1
        assert r.playbooks[0].id == "test-pb"


# ---------------------------------------------------------------------------
# PlaybookRegistry.match()
# ---------------------------------------------------------------------------


class TestRegistryMatch:
    def test_oom_event_matches_oom_playbook(self, registry: PlaybookRegistry) -> None:
        event = _make_event(pattern_matched="oom_killer_invoked")
        pb = registry.match(event)
        assert pb is not None
        assert "oom" in pb.id.lower() or any("oom" in kw for kw in pb.pattern_keywords)

    def test_memory_event_matches_memory_playbook(
        self, registry: PlaybookRegistry
    ) -> None:
        event = _make_event(pattern_matched="high_memory_usage")
        pb = registry.match(event)
        assert pb is not None

    def test_restart_event_matches_restart_playbook(
        self, registry: PlaybookRegistry
    ) -> None:
        event = _make_event(pattern_matched="container_restart_detected")
        pb = registry.match(event)
        assert pb is not None

    def test_timeout_event_matches_timeout_playbook(
        self, registry: PlaybookRegistry
    ) -> None:
        event = _make_event(pattern_matched="kafka_connection_timeout")
        pb = registry.match(event)
        assert pb is not None

    def test_disk_full_event_matches_disk_playbook(
        self, registry: PlaybookRegistry
    ) -> None:
        event = _make_event(pattern_matched="disk_full_alert")
        pb = registry.match(event)
        assert pb is not None

    def test_unknown_pattern_returns_none(self, registry: PlaybookRegistry) -> None:
        event = _make_event(pattern_matched="exotic_db_corruption_xyzzy")
        pb = registry.match(event)
        assert pb is None

    def test_match_is_case_insensitive(self, registry: PlaybookRegistry) -> None:
        event = _make_event(pattern_matched="OOM_KILLER_ACTIVE")
        pb = registry.match(event)
        assert pb is not None

    def test_empty_registry_returns_none(self) -> None:
        r = PlaybookRegistry(playbooks=())
        event = _make_event(pattern_matched="oom_killer")
        assert r.match(event) is None


# ---------------------------------------------------------------------------
# Playbook.render()
# ---------------------------------------------------------------------------


class TestPlaybookRender:
    def test_render_substitutes_container(self, registry: PlaybookRegistry) -> None:
        event = _make_event(pattern_matched="oom_killer", container="my-container")
        pb = registry.match(event)
        assert pb is not None
        rendered = pb.render(event)
        assert "my-container" in rendered

    def test_render_substitutes_host(self, registry: PlaybookRegistry) -> None:
        event = _make_event(pattern_matched="oom_killer", host="my-host")
        pb = registry.match(event)
        assert pb is not None
        rendered = pb.render(event)
        assert "my-host" in rendered

    def test_render_substitutes_restart_count(self, registry: PlaybookRegistry) -> None:
        event = _make_event(pattern_matched="oom_killer", restart_count=3)
        pb = registry.match(event)
        assert pb is not None
        rendered = pb.render(event)
        assert "3" in rendered

    def test_render_handles_none_restart_count(
        self, registry: PlaybookRegistry
    ) -> None:
        event = _make_event(pattern_matched="oom_killer", restart_count=None)
        pb = registry.match(event)
        assert pb is not None
        rendered = pb.render(event)
        assert "N/A" in rendered

    def test_render_from_custom_playbook(self) -> None:
        pb = Playbook(
            id="test",
            title="Test",
            pattern_keywords=("testpat",),
            severity_scope=("ERROR",),
            dispatch_prompt="Fix {container} on {host} at {detected_at}",
            dod_evidence=(),
        )
        event = _make_event(container="my-svc", host="my-host")
        rendered = pb.render(event)
        assert "my-svc" in rendered
        assert "my-host" in rendered
