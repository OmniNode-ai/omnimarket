# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No lab configuration value is hard-coded in the product's non-test source (OMN-19184).

The lab's hostnames, LAN addresses and served model ids are deployment facts. They belong in
contract overlays (``<root>/<node>/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``), which
change without a pull request, never in the package every customer installs.

The scan reads the values a file carries, not its prose: scalars and keys of YAML and JSON,
and the string constants of Python that are not docstrings. Comments and docstrings explain
and route nothing, so they are out of scope.

``lab_values_inventory.json`` is a shrink-only ratchet, not an allowlist that passes the gate:
it names the files that still carry a lab value and are owed a move. A file outside it that
gains a lab value fails; a file in it that has lost every lab value fails until its entry is
removed. Moved units are listed in ``MOVED_UNITS`` and can never re-enter the inventory.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SOURCE = _REPO / "src" / "omnimarket"
_INVENTORY = Path(__file__).with_name("lab_values_inventory.json")
_YamlLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: A served model id that exists only on the lab's hosts, a lab LAN address, a lab hostname,
#: or a lab runtime address written as a short host suffix and port.
LAB_VALUE = re.compile(
    r"Qwen3\.8-27B|deepseek-v4-flash|\b192\.168\.86\.\d{1,3}\b"
    r"|\bh(?:0\d\d|1\d\d|2\d\d)\b|\.20[12]:\d{4,5}",
    re.IGNORECASE,
)

#: Units already moved into overlays; each must read as clean, whatever the inventory says.
MOVED_UNITS = (
    "configs/model_bindings.v1.yaml",
    "nodes/node_ab_compare_orchestrator/models_registry.yaml",
    "nodes/node_delegation_rubric_check_compute/handlers/handler_delegation_route_check.py",
    "nodes/node_ab_compare_orchestrator/handlers/handler_ab_compare_orchestrator.py",
)


def _python_values(text: str) -> Iterator[str]:
    tree = ast.parse(text)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(
                node,
                ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def _document_values(document: object) -> Iterator[str]:
    if isinstance(document, str):
        yield document
    elif isinstance(document, dict):
        for key, value in document.items():
            yield from _document_values(key)
            yield from _document_values(value)
    elif isinstance(document, list):
        for value in document:
            yield from _document_values(value)


def lab_values_in(name: str, text: str) -> list[str]:
    """The distinct lab values a file's content carries, comments and docstrings excluded."""
    suffix = Path(name).suffix
    if suffix == ".py":
        values = list(_python_values(text))
    elif suffix == ".json":
        values = list(_document_values(json.loads(text)))
    else:
        values = [
            v
            for doc in yaml.load_all(text, Loader=_YamlLoader)
            for v in _document_values(doc)
        ]
    return sorted({m.group(0) for value in values for m in LAB_VALUE.finditer(value)})


def _is_test_source(path: Path) -> bool:
    relative = path.relative_to(_SOURCE).parts
    return (
        "tests" in relative
        or path.name.startswith("test_")
        or path.name == "conftest.py"
        or "__pycache__" in relative
    )


def _scan_tree() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_SOURCE.rglob("*")):
        if path.suffix not in {".py", ".yaml", ".yml", ".json"} or not path.is_file():
            continue
        if _is_test_source(path):
            continue
        text = path.read_text(encoding="utf-8")
        if not LAB_VALUE.search(text):
            continue  # a value is part of the text; only files that mention one are parsed
        hits = lab_values_in(path.name, text)
        if hits:
            found[path.relative_to(_SOURCE).as_posix()] = hits
    return found


def _inventory() -> set[str]:
    document = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    return set(document["files_owed_a_move"])


def test_scanner_flags_a_lab_value_in_each_file_kind() -> None:
    """Positive control: the scan reads yaml, json and python values."""
    assert lab_values_in("a.yaml", "model: Qwen3.8-27B\n") == ["Qwen3.8-27B"]
    lan_address = ".".join(("192", "168", "86", "201"))
    assert lab_values_in("a.yaml", f"base_url: http://{lan_address}:8000/v1\n") == [
        lan_address
    ]
    assert lab_values_in("a.yaml", "hosts:\n  h201: [onex]\n") == ["h201"]
    assert lab_values_in("a.json", '{"expected_model": "Qwen3.8-27B"}') == [
        "Qwen3.8-27B"
    ]
    assert lab_values_in("a.py", 'MODEL = "Qwen3.8-27B"\n') == ["Qwen3.8-27B"]
    assert lab_values_in("a.py", "def f(host: str = 'h053'):\n    return host\n") == [
        "h053"
    ]


def test_scanner_ignores_comments_and_docstrings() -> None:
    """Negative control: prose is not a configuration value."""
    assert (
        lab_values_in("a.yaml", "# served on Qwen3.8-27B at h201\nkey: value\n") == []
    )
    assert lab_values_in("a.py", '"""Qwen3.8-27B on h201."""\nX = 1  # h053\n') == []


def test_moved_units_are_free_of_lab_values() -> None:
    """Failing control for a regression: a moved value that is hard-coded again fails here."""
    for unit in MOVED_UNITS:
        path = _SOURCE / unit
        assert path.is_file(), unit
        assert lab_values_in(path.name, path.read_text(encoding="utf-8")) == [], unit


def test_moved_units_are_not_in_the_inventory() -> None:
    assert not (set(MOVED_UNITS) & _inventory())


def test_no_file_outside_the_inventory_carries_a_lab_value() -> None:
    found = _scan_tree()
    new = {name: hits for name, hits in found.items() if name not in _inventory()}
    assert not new, (
        "lab configuration is hard-coded in product source; move it to a contract "
        f"overlay (OMN-19184): {new}"
    )


def test_inventory_only_shrinks() -> None:
    """A file in the inventory that no longer carries a lab value must leave it."""
    found = _scan_tree()
    stale = sorted(name for name in _inventory() if name not in found)
    assert not stale, (
        f"remove from lab_values_inventory.json, they are clean now: {stale}"
    )
