# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pair loader resolves producer and consumer models the way the runtime does (OMN-24121).

Exemplar followed: tests/nodes/node_broker_acl_overlay_render_compute/test_coverage_gate.py (a real
temporary repository, a synthetic fixture). The loader is also run as a SCRIPT, as it is against a base
revision's source, to show it needs nothing from omnimarket.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_seam_field_diff_check_effect.handlers import pair_loader
from tests.nodes.node_seam_field_diff_check_effect.fixture_repo import (
    MISSING_FIELD_CONSUMER_FIELDS,
    TOPIC,
    new_package_name,
    new_repo,
)

pytestmark = pytest.mark.unit


def _collect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    envelope_models: tuple[str, ...] = (),
    **kwargs: str,
) -> dict[str, Any]:
    package = new_package_name()
    repo = new_repo(tmp_path, package, **kwargs)
    monkeypatch.syspath_prepend(str(repo / "src"))
    loaded = pair_loader.collect_pairs(
        {package: repo / "src" / package}, envelope_models
    )
    return dict(loaded)


def test_a_producer_and_consumer_on_one_topic_resolve_to_their_declared_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loaded = _collect(monkeypatch, tmp_path)
    assert loaded["unreadable"] == []
    (pair,) = loaded["pairs"]
    assert pair["topic"] == TOPIC
    assert pair["producer_rule"] == "R3"
    assert pair["consumer_rule"] == "C6"
    assert pair["producer"]["model_ref"].endswith(".models.alpha.ModelAlphaEvent")
    assert pair["consumer"]["model_ref"].endswith(".models.beta.ModelBetaRequest")
    assert pair["consumer"]["extra_forbidden"] is True
    assert pair["producer"]["fields"]["count"]["required"] is True
    assert pair["consumer"]["has_adapter"] is False


def test_a_consumer_with_a_before_validator_is_marked_as_having_an_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prelude = (
        "\n    @model_validator(mode='before')\n"
        "    @classmethod\n"
        "    def reshape(cls, data: object) -> object:\n"
        "        return data\n"
    )
    loaded = _collect(monkeypatch, tmp_path, consumer_prelude=prelude)
    assert loaded["pairs"][0]["consumer"]["has_adapter"] is True


def test_a_generic_envelope_consumer_is_never_a_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    monkeypatch.syspath_prepend(str(repo / "src"))
    envelope = f"{package}.models.beta.ModelBetaRequest"
    loaded = pair_loader.collect_pairs({package: repo / "src" / package}, (envelope,))
    assert loaded["pairs"] == []


def test_a_model_that_cannot_be_imported_is_named_not_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    (repo / "src" / package / "models" / "alpha.py").write_text(
        "raise RuntimeError('broken')\n"
    )
    monkeypatch.syspath_prepend(str(repo / "src"))
    loaded = pair_loader.collect_pairs({package: repo / "src" / package}, ())
    assert loaded["pairs"] == []
    (reason,) = loaded["unreadable"]
    assert "RuntimeError" in reason
    assert TOPIC in reason


def test_a_consumer_field_the_producer_lacks_is_in_both_summaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loaded = _collect(
        monkeypatch, tmp_path, consumer_fields=MISSING_FIELD_CONSUMER_FIELDS
    )
    (pair,) = loaded["pairs"]
    assert "label" in pair["consumer"]["fields"]
    assert "label" not in pair["producer"]["fields"]


def test_the_loader_runs_as_a_script_and_needs_nothing_from_omnimarket(
    tmp_path: Path,
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(pair_loader.__file__).resolve()),
            "--root",
            f"{package}={repo / 'src' / package}",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(repo / "src"), "PATH": ""},
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert len(loaded["pairs"]) == 1
    assert "omnimarket" not in Path(pair_loader.__file__).read_text().split('"""')[2]
