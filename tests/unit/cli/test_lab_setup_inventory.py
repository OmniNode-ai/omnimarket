# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Legacy surface audit, not evidence that setup implements those capabilities.

The old install.sh inventory stays intact. These controls require an additive
inventory for the full lab walk and both sourced configuration helpers. Source
fixtures are never executed. A deliberate omission and a new source flag must
both fail the same coverage check used on the real inventory.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _ROOT / "tests/fixtures/onex_setup"
_INVENTORY = (
    _ROOT / "src/omnimarket/nodes/node_onex_setup_effect/setup_step_inventory.yaml"
)
_SOURCES = {
    "walk": ("lab_onboarding_8c1b3e5e.txt", "8c1b3e5e60f7f124fc5d1fb10e7bc6affcaa038e"),
    "preflight": (
        "onboarding_preflight_caf4b1a8.txt",
        "caf4b1a8fa85fea42c4db0644539f5dc954bba3f",
    ),
    "model-config": (
        "onboarding_model_config_c378a729.txt",
        "c378a729561d06b46bfbdab5bbee83c08975de60",
    ),
}
_ARTIFACTS = {
    "phases.v2.done",
    "choices",
    "stack.anchor",
    ".omninode-dev-setup",
    "routing_overlay.yaml",
    "runtime_config.yaml",
    "delegation.sqlite",
    "bifrost_overrides.yaml",
    ".onex-dispatch-venv",
    "shell-profile",
    "workspace-config-git",
    "onex-symlink",
    "model-credential-store",
    "claude-plugin-registration",
    "proven-floor-stamp",
    "run-log-status",
    "docker-settings",
    "docker-local-env",
    "stack-tenant-and-secrets",
}


def _flags(text: str) -> set[str]:
    flags: set[str] = set()
    for arm in re.findall(r"(?:^|;;)\s*([-a-z|=*]+)\)", text, re.MULTILINE):
        for option in arm.split("|"):
            name = option.split("=", 1)[0]
            if re.fullmatch(r"--[a-z][a-z-]*", name):
                flags.add(name)
    return flags


def _surface(sources: dict[str, str]) -> set[tuple[str, str, str]]:
    result: set[tuple[str, str, str]] = set()
    for source, text in sources.items():
        if source == "model-config":
            functions = {
                n.name
                for n in ast.walk(ast.parse(text))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        else:
            functions = set(
                re.findall(r"^([a-zA-Z_][a-zA-Z0-9_]*)\(\)\s*\{", text, re.MULTILINE)
            )
        result.update((source, "function", f) for f in functions)
        result.update((source, "flag", flag) for flag in _flags(text))
    result.update(("walk", "phase", f"phase{n}") for n in range(7))
    result.update(("walk", "artifact", a) for a in _ARTIFACTS)
    return result


def _sources() -> dict[str, str]:
    return {key: (_FIXTURES / name).read_text() for key, (name, _) in _SOURCES.items()}


def _entries() -> list[dict[str, str]]:
    data = yaml.safe_load(_INVENTORY.read_text())
    assert isinstance(data, dict)
    assert "entries" in data, "the original install.sh inventory must remain"
    return data["lab_onboarding"]["entries"]


def _assert_coverage(sources: dict[str, str], entries: list[dict[str, str]]) -> None:
    actual = _surface(sources)
    owned = {(e["source"], e["kind"], e["identifier"]) for e in entries}
    assert len(entries) == len(owned), "duplicate legacy inventory entries"
    assert actual == owned, (
        f"missing={sorted(actual - owned)}; stale={sorted(owned - actual)}"
    )


def _assert_fixture_bytes(source: str, data: bytes) -> None:
    metadata = yaml.safe_load(_INVENTORY.read_text())["lab_onboarding"]["sources"]
    entry = next(item for item in metadata if item["id"] == source)
    assert hashlib.sha256(data).hexdigest() == entry["fixture_sha256"], (
        "fixture bytes differ from the audited public copy"
    )


@pytest.mark.parametrize("source", _SOURCES)
def test_fixture_matches_audited_public_copy(source: str) -> None:
    filename, expected = _SOURCES[source]
    data = (_FIXTURES / filename).read_bytes()
    _assert_fixture_bytes(source, data)
    if source != "walk":
        digest = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        assert digest == expected


def test_unrecorded_fixture_change_is_detected() -> None:
    data = (_FIXTURES / _SOURCES["walk"][0]).read_bytes()
    _assert_fixture_bytes("walk", data)
    changed = data.replace(b"--provider", b"--unrecorded-provider", 1)
    assert changed != data
    with pytest.raises(AssertionError, match="fixture bytes differ"):
        _assert_fixture_bytes("walk", changed)


def test_instrument_sees_real_flags_functions_and_helpers() -> None:
    sources = _sources()
    assert {
        "--preflight-only",
        "--containers",
        "--no-containers",
        "--provider",
        "--ollama-model",
        "--local-url",
        "--local-model",
        "--compat-url",
        "--workspace",
        "--set-aside",
        "--developer",
        "--restart",
        "--help",
    } <= _flags(sources["walk"])
    surface = _surface(sources)
    assert all(("walk", "function", f"phase{n}") in surface for n in range(7))
    assert ("preflight", "function", "save_choices") in surface
    assert ("model-config", "function", "_place_local_url") in surface


def test_every_real_surface_has_one_owner() -> None:
    _assert_coverage(_sources(), _entries())
    for entry in _entries():
        # OMN-23413 Amendment 4: surfaces the operator left with the unchanged
        # onboarding script name it as their external owner; every other
        # surface is owned by an onex setup capability.
        assert entry["owner"].startswith("onex setup:") or (
            entry["disposition"] == "external"
            and entry["owner"] == "lab-onboarding.sh (unchanged, Amendment 3)"
        ), entry
        assert entry["disposition"] in {
            "integrated",
            "external",
            "retired",
        }, entry
        assert len(entry["reason"].strip()) >= 20, entry


def test_missing_real_provider_flag_is_detected() -> None:
    entries = _entries()
    removed = [
        e for e in entries if e["kind"] == "flag" and e["identifier"] == "--provider"
    ]
    assert removed, "positive control must remove a real, inventoried capability"
    broken = [e for e in entries if e not in removed]
    with pytest.raises(AssertionError, match="--provider"):
        _assert_coverage(_sources(), broken)


def test_new_source_capability_without_owner_is_detected() -> None:
    sources = _sources()
    sources["walk"] += "\n    --future-capability) echo changed ;;\n"
    assert "--future-capability" in _flags(sources["walk"])
    with pytest.raises(AssertionError, match="--future-capability"):
        _assert_coverage(sources, _entries())


def test_inventory_pins_the_same_audited_sources() -> None:
    data = yaml.safe_load(_INVENTORY.read_text())["lab_onboarding"]
    assert {s["id"]: (s["fixture"], s["git_blob"]) for s in data["sources"]} == _SOURCES
