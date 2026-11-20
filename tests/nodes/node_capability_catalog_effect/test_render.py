"""Tests for the capability catalog schema and renderers.

Run: uv run pytest tests/capability_catalog -q
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_capability_catalog_effect.handlers import (
    handler_capability_catalog_render as render,
)
from omnimarket.nodes.node_capability_catalog_effect.models.model_capability_catalog import (
    Catalog,
)

pytestmark = pytest.mark.unit

find_term = render.find_term
load_static_facts = render.load_static_facts
render_brief_part = render.render_brief_part
render_claude_md_block = render.render_claude_md_block
render_digest = render.render_digest
render_full_markdown = render.render_full_markdown

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "catalog_2026-09-27.json"
PACKAGE_SRC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_capability_catalog_effect"
)


def _load_fixture() -> Catalog:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return Catalog.from_dict(json.load(fh))


def _static_facts() -> dict[str, Any]:
    return load_static_facts()


def test_fixture_round_trips_through_to_dict() -> None:
    catalog = _load_fixture()
    again = Catalog.from_dict(catalog.to_dict())
    assert again.to_dict() == catalog.to_dict()


def test_two_renders_of_the_fixture_are_byte_identical() -> None:
    catalog = _load_fixture()
    static_facts = _static_facts()
    first = render_full_markdown(catalog, static_facts)
    second = render_full_markdown(catalog, static_facts)
    assert first == second

    first_digest = render_digest(catalog, static_facts)
    second_digest = render_digest(catalog, static_facts)
    assert first_digest == second_digest

    first_brief = render_brief_part(catalog, static_facts)
    second_brief = render_brief_part(catalog, static_facts)
    assert first_brief == second_brief


def test_unknown_lane_never_renders_free() -> None:
    catalog = _load_fixture()
    markdown = render_full_markdown(catalog, _static_facts())
    # The fixture's lab-201/dev lane is status "unknown".
    for line in markdown.splitlines():
        if "| lab-201 | dev |" in line:
            assert "FREE" not in line
            assert "UNKNOWN" in line
            return
    pytest.fail("fixture's unknown lane (lab-201/dev) not found in rendered markdown")


def test_unknown_host_never_renders_up() -> None:
    catalog = _load_fixture()
    markdown = render_full_markdown(catalog, _static_facts())
    for line in markdown.splitlines():
        if line.startswith("| lab-202"):
            assert "| up |" not in line
            assert "OUT OF SERVICE" in line
            return
    pytest.fail(
        "fixture's out-of-service host (lab-202) not found in rendered markdown"
    )


def test_digest_is_at_most_40_lines() -> None:
    digest = render_digest(_load_fixture(), _static_facts())
    lines = digest.splitlines()
    assert len(lines) <= 40


def test_digest_names_every_declared_host() -> None:
    catalog = _load_fixture()
    digest = render_digest(catalog, _static_facts())
    for host in catalog.hosts:
        assert host.host_id in digest


def test_digest_names_every_static_facts_engine() -> None:
    digest = render_digest(_load_fixture(), _static_facts())
    for name in (
        "Codex",
        "onex delegate",
        "GLM",
        "Jev",
        "crush",
        "OpenRouter",
        "Gemini",
    ):
        assert name in digest, f"digest is missing engine {name!r}"


def test_digest_names_the_front_door_skills() -> None:
    static_facts = _static_facts()
    digest = render_digest(_load_fixture(), static_facts)
    for skill in static_facts["front_door_skills"]:
        assert skill in digest


def test_claude_md_block_never_shows_an_unknown_lane_as_free() -> None:
    block = render_claude_md_block(_load_fixture())
    for line in block.splitlines():
        if "| lab-201 | dev |" in line:
            assert "FREE" not in line
            return
    pytest.fail("unknown lane not found in claude-md block")


def test_find_returns_the_glm_engine_and_its_credential_ref() -> None:
    hits = find_term(_load_fixture(), _static_facts(), "glm")
    joined = "\n".join(hits)
    assert "GLM" in joined
    assert "llm.glm.api_key" in joined


def test_find_with_no_match_returns_empty() -> None:
    assert (
        find_term(_load_fixture(), _static_facts(), "definitely-not-a-real-term") == []
    )


def test_never_reads_secret_value() -> None:
    """No code path in this package ever reads a credential value, only its ref name."""
    for path in PACKAGE_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "secret_value" not in text, (
            f"{path} names secret_value; this package must never read it"
        )
        # Belt and suspenders: no SQL string anywhere selects it either.
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "secret_value" not in node.value.lower(), (
                    f"{path} embeds a SQL string naming secret_value"
                )
