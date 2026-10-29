# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing rules model, default data, overlay merge, folder map and no-literal check."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.handler_kb_routing_rules import (
    DEFAULT_RULES_PATH,
    HandlerKbRoutingRules,
    deep_merge,
    read_yaml_mapping,
    resolve_manifest,
)
from omnimarket.models.kb_routing import KbRoutingRulesError, ModelKbManifest

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "kb_routing"
SYNTHETIC_OVERLAY = FIXTURES / "synthetic_overlay.yaml"
SYNTHETIC_MANIFEST = FIXTURES / "synthetic_manifest.yaml"
PLANTED_VIOLATION = FIXTURES / "planted_violation_node_code.py.txt"
SRC = Path(__file__).resolve().parents[2] / "src" / "omnimarket"


def _write(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _default() -> dict[str, Any]:
    return read_yaml_mapping(DEFAULT_RULES_PATH)


def test_default_data_loads() -> None:
    rules = HandlerKbRoutingRules().load()
    assert rules.audience_order[0] != rules.audience_order[-1]
    kinds = {store.kind.value for store in rules.stores.values()}
    assert kinds == {"git_repository", "drive", "in_memory", "local_directory"}


def test_overlay_wider_audience_refused_at_load(tmp_path: Path) -> None:
    default = _default()
    plain_class = next(
        k
        for k, v in default["classes"].items()
        if not v.get("sensitive") and v["max_audience"] == "company"
    )
    wide_store = next(
        k for k, v in default["stores"].items() if v["audience"] == "public"
    )
    overlay = _write(
        tmp_path,
        "overlay.yaml",
        {"classes": {plain_class: {"default_store": wide_store}}},
    )
    with pytest.raises(KbRoutingRulesError, match="wider than the class maximum"):
        HandlerKbRoutingRules(overlay_path=overlay).load()


def test_sensitive_class_store_wider_than_ceiling_refused(tmp_path: Path) -> None:
    default = _default()
    sensitive_class = next(
        k for k, v in default["classes"].items() if v.get("sensitive")
    )
    company_store = next(
        k for k, v in default["stores"].items() if v["audience"] == "company"
    )
    overlay = _write(
        tmp_path,
        "overlay.yaml",
        {
            "classes": {
                sensitive_class: {
                    "max_audience": "company",
                    "default_store": company_store,
                }
            }
        },
    )
    with pytest.raises(KbRoutingRulesError, match="sensitive class"):
        HandlerKbRoutingRules(overlay_path=overlay).load()


def test_overlay_can_widen_nothing_but_may_narrow(tmp_path: Path) -> None:
    default = _default()
    narrow_store = next(
        k for k, v in default["stores"].items() if v["audience"] == "operator"
    )
    overlay = _write(
        tmp_path,
        "overlay.yaml",
        {"classes": {"public": {"default_store": narrow_store}}},
    )
    rules = HandlerKbRoutingRules(overlay_path=overlay).load()
    assert rules.classes["public"].default_store == narrow_store


def test_overlay_audience_outside_order_refused(tmp_path: Path) -> None:
    default = _default()
    store = next(iter(default["stores"]))
    overlay = _write(
        tmp_path, "overlay.yaml", {"stores": {store: {"audience": "nowhere"}}}
    )
    with pytest.raises(KbRoutingRulesError, match="not in the audience order"):
        HandlerKbRoutingRules(overlay_path=overlay).load()


def test_external_store_without_checks_refused(tmp_path: Path) -> None:
    default = _default()
    external = next(k for k, v in default["stores"].items() if v.get("required_checks"))
    overlay = _write(
        tmp_path, "overlay.yaml", {"stores": {external: {"required_checks": []}}}
    )
    with pytest.raises(KbRoutingRulesError, match="no required checks"):
        HandlerKbRoutingRules(overlay_path=overlay).load()


def test_every_class_has_a_rule(tmp_path: Path) -> None:
    default = _default()
    required = default["required_classes"]
    rules = HandlerKbRoutingRules().load()
    assert required
    assert all(key in rules.classes for key in required)
    del default["classes"][required[0]]
    broken = _write(tmp_path, "default.yaml", default)
    with pytest.raises(KbRoutingRulesError, match="has no rule"):
        HandlerKbRoutingRules(default_path=broken).load()


def test_overlay_merge_is_deep_and_replaces_lists() -> None:
    merged = deep_merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}})
    assert merged == {"a": {"b": 1, "c": [2]}}


def test_manifest_resolves_to_one_folder_key() -> None:
    manifest = HandlerKbRoutingRules.load_manifest(SYNTHETIC_MANIFEST)
    rules = HandlerKbRoutingRules(overlay_path=SYNTHETIC_OVERLAY).load(manifest)
    assignments = resolve_manifest(rules, manifest)
    assert len(assignments) == len(manifest.files)
    assert manifest.expected_counts
    counts: dict[str, int] = {}
    for folder in assignments.values():
        counts[folder] = counts.get(folder, 0) + 1
    assert counts == manifest.expected_counts
    # the group that spans folders resolves per file through its selectors
    spanning = {assignments[f.name] for f in manifest.files if f.group.endswith("span")}
    assert len(spanning) == 2


def test_manifest_count_table_mismatch_fails_load() -> None:
    manifest = HandlerKbRoutingRules.load_manifest(SYNTHETIC_MANIFEST)
    wrong = ModelKbManifest(
        files=manifest.files,
        expected_counts={**manifest.expected_counts, "folder_alpha": 99},
    )
    with pytest.raises(KbRoutingRulesError, match="differ from the table"):
        HandlerKbRoutingRules(overlay_path=SYNTHETIC_OVERLAY).load(wrong)


def test_folder_key_removed_fails_load(tmp_path: Path) -> None:
    manifest = HandlerKbRoutingRules.load_manifest(SYNTHETIC_MANIFEST)
    overlay = read_yaml_mapping(SYNTHETIC_OVERLAY)
    removed = "folder_beta"
    del overlay["drive_folders"][removed]
    path = _write(tmp_path, "overlay.yaml", overlay)
    victim = next(f.name for f in manifest.files if f.group == "group_two")
    with pytest.raises(KbRoutingRulesError, match=victim):
        HandlerKbRoutingRules(overlay_path=path).load(manifest)


def test_two_matching_selectors_fails_load(tmp_path: Path) -> None:
    manifest = HandlerKbRoutingRules.load_manifest(SYNTHETIC_MANIFEST)
    overlay = read_yaml_mapping(SYNTHETIC_OVERLAY)
    overlay["drive_folders"]["folder_alpha"]["selectors"].append({"group": "group_two"})
    path = _write(tmp_path, "overlay.yaml", overlay)
    victim = next(f.name for f in manifest.files if f.group == "group_two")
    with pytest.raises(KbRoutingRulesError, match=victim):
        HandlerKbRoutingRules(overlay_path=path).load(manifest)


def test_folder_naming_a_non_drive_store_refused(tmp_path: Path) -> None:
    overlay = read_yaml_mapping(SYNTHETIC_OVERLAY)
    default = _default()
    other = next(k for k, v in default["stores"].items() if v["kind"] != "drive")
    overlay["drive_folders"]["folder_alpha"]["drive_store"] = other
    path = _write(tmp_path, "overlay.yaml", overlay)
    with pytest.raises(KbRoutingRulesError, match="not a drive store"):
        HandlerKbRoutingRules(overlay_path=path).load()


def test_real_overlay_pass_is_reported_not_run_without_the_environment() -> None:
    """The real overlay and manifest run the same loader; absent, the report says so."""
    report = HandlerKbRoutingRules.run_real_overlay_pass({})
    assert not report.ran
    assert report.detail.startswith("not run:")
    synthetic = HandlerKbRoutingRules.run_real_overlay_pass(
        {
            "KB_ROUTING_OVERLAY_PATH": str(SYNTHETIC_OVERLAY),
            "KB_ROUTING_MANIFEST_PATH": str(SYNTHETIC_MANIFEST),
        }
    )
    assert synthetic.ran
    real = HandlerKbRoutingRules.run_real_overlay_pass(dict(os.environ))
    print(f"REAL-OVERLAY-PASS ran={real.ran} detail={real.detail} counts={real.counts}")


# --- no literals in node code (AC5) -------------------------------------------


def _forbidden_literals() -> set[str]:
    """Every store id, base path, class key, folder key and folder id, from the data."""
    values: set[str] = set()
    for overlay in (None, SYNTHETIC_OVERLAY):
        rules = HandlerKbRoutingRules(overlay_path=overlay).load()
        for store in rules.stores.values():
            values.update({store.id, store.base_path})
        for cls in rules.classes.values():
            values.update({cls.key, cls.path_template})
        for folder in rules.drive_folders.values():
            values.update({folder.key, folder.folder_id})
    return values


def find_literals(source: str, forbidden: set[str]) -> list[tuple[int, str]]:
    """Pure checker: string constants (docstrings excepted) equal to a forbidden value."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and node.value in forbidden
    ]


def _node_code_files() -> list[Path]:
    files = sorted((SRC / "models" / "kb_routing").glob("*.py"))
    files.append(SRC / "handlers" / "handler_kb_routing_rules.py")
    return files


def test_no_literals_in_node_code() -> None:
    forbidden = _forbidden_literals()
    assert forbidden
    files = _node_code_files()
    assert len(files) > 1
    hits = {
        path.name: find_literals(path.read_text(encoding="utf-8"), forbidden)
        for path in files
    }
    assert {name: found for name, found in hits.items() if found} == {}


def test_no_literals_check_refuses_the_planted_violation() -> None:
    found = find_literals(
        PLANTED_VIOLATION.read_text(encoding="utf-8"), _forbidden_literals()
    )
    assert {value for _, value in found} == {
        "drive_admin_example",
        "synthetic-folder-id-0001",
        "folder_alpha",
    }
