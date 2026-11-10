# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model the overlay home validates the ``lane_commit_signing`` overlay through (OMN-22993).

Every host, key and address here is synthetic: the real document is private deployment data and
stays in the overlay home.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.node_overlays.model_lane_commit_signing_overlay import (
    ModelLaneCommitSigningOverlay,
)

pytestmark = pytest.mark.unit

HOST = "host-a"
KEY = ".ssh/id_synthetic"
EMAIL = "lane@example.invalid"


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {"hosts": {HOST: {"key": KEY}}}
    document.update(overrides)
    return document


def test_golden_loads_the_way_the_overlay_home_loads_it(tmp_path: Path) -> None:
    (tmp_path / "lane_commit_signing").mkdir()
    (tmp_path / "lane_commit_signing" / "overlay.yaml").write_text(
        yaml.safe_dump(_document()), encoding="utf-8"
    )
    loaded = load_structured_overlay(
        "lane_commit_signing",
        ModelLaneCommitSigningOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    row = loaded.signing_for(HOST)
    assert row is not None
    assert (row.key, row.email) == (KEY, None)
    assert loaded.signing_for("host-z") is None


def test_golden_an_absolute_key_and_an_email_are_kept() -> None:
    loaded = ModelLaneCommitSigningOverlay.model_validate(
        _document(
            hosts={
                HOST: {"key": "/etc/lane/id_synthetic", "email": EMAIL},
                "host-b": {"key": KEY},
            }
        )
    )
    row = loaded.signing_for(HOST)
    assert row is not None
    assert (row.key, row.email) == ("/etc/lane/id_synthetic", EMAIL)
    other = loaded.signing_for("host-b")
    assert other is not None
    assert other.email is None


@pytest.mark.parametrize(
    ("document", "field"),
    [
        (_document(stray="x"), "stray"),
        (_document(hosts={}), "hosts"),
        (_document(hosts={"": {"key": KEY}}), "hosts"),
        (_document(hosts={HOST: {}}), "key"),
        (_document(hosts={HOST: {"key": ""}}), "key"),
        (_document(hosts={HOST: {"key": "a b"}}), "key"),
        (_document(hosts={HOST: {"key": "a;rm"}}), "key"),
        (_document(hosts={HOST: {"key": "../id_synthetic"}}), "key"),
        (_document(hosts={HOST: {"key": ".ssh/../id_synthetic"}}), "key"),
        (_document(hosts={HOST: {"key": KEY, "email": "not-an-address"}}), "email"),
        (
            _document(hosts={HOST: {"key": KEY, "email": "a b@example.invalid"}}),
            "email",
        ),
        (_document(hosts={HOST: {"key": KEY, "stray": "x"}}), "stray"),
        (_document(hosts={HOST: {"key": False}}), "key"),
    ],
)
def test_error_a_malformed_document_refuses_by_field(
    document: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelLaneCommitSigningOverlay.model_validate(document)
    assert field in str(excinfo.value)


PROBE = (
    "import sys\n"
    "{extra}"
    "import omnimarket.models.node_overlays.model_lane_commit_signing_overlay\n"
    "allowed = set(sys.stdlib_module_names) | {{'pydantic', 'pydantic_core', 'annotated_types', 'typing_extensions', 'omnimarket', '_distutils_hack', 'typing_inspection'}}\n"
    "heavy = sorted({{n.split('.')[0] for n in sys.modules}} - allowed - {{n for n in sys.modules if n.startswith('_')}})\n"
    "assert not heavy, heavy\n"
)


def _import_probe(extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", PROBE.format(extra=extra)],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )


def test_golden_the_model_imports_with_pydantic_alone() -> None:
    result = _import_probe("")
    assert result.returncode == 0, result.stderr


def test_control_an_extra_import_turns_the_import_check_red() -> None:
    """The failing control: the same probe with yaml imported first must refuse."""
    result = _import_probe("import yaml\n")
    assert result.returncode != 0
    assert "yaml" in result.stderr
