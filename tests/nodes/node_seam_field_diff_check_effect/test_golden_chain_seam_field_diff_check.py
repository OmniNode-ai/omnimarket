# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a matching pair, unchanged or newly added, passes the merge check (OMN-24121)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.seam_field_diff.model_seam_field_diff_check import (
    ModelSeamFieldDiffCheckRequest,
    ModelSeamFieldDiffCheckResult,
)
from omnimarket.nodes.node_seam_field_diff_check_effect.handlers.handler_seam_field_diff_check import (
    HandlerSeamFieldDiffCheck,
    contract_config,
)
from tests.nodes.node_seam_field_diff_check_effect.fixture_repo import (
    commit,
    config,
    new_package_name,
    new_repo,
    write_package,
)

pytestmark = pytest.mark.unit


def _check(
    repo: Path, package: str, ref: str = "HEAD~1", floor: int = 1
) -> ModelSeamFieldDiffCheckResult:
    return HandlerSeamFieldDiffCheck(
        config_override=config(package, floor=floor)
    ).handle(ModelSeamFieldDiffCheckRequest(repo_root=str(repo), base_ref=ref))


def test_a_matching_pair_unchanged_since_the_base_is_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    (repo / "README.md").write_text("an unrelated change\n")
    commit(repo, "head")
    monkeypatch.syspath_prepend(str(repo / "src"))
    result = _check(repo, package)
    assert result.exit_code == 0
    assert result.status == "clean"
    assert result.pairs_compared == 1
    assert result.output_lines[-1].startswith(
        "seam field diff: clean; 1 pairs compared"
    )


def test_a_new_matching_consumer_field_the_producer_has_is_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    repo = new_repo(tmp_path, package)
    write_package(
        repo, package, consumer_fields="    correlation_id: str\n    count: int\n"
    )
    commit(repo, "head")
    monkeypatch.syspath_prepend(str(repo / "src"))
    assert _check(repo, package).exit_code == 0


def test_the_contract_config_carries_every_value_the_check_uses() -> None:
    config_block = contract_config()
    assert set(config_block) == {
        "repo_packages",
        "contract_packages",
        "envelope_models",
        "nested_depth_limit",
        "min_pairs_compared",
        "base_load_timeout_s",
    }


def test_a_consumer_with_an_adapter_is_named_as_skipped_not_judged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = new_package_name()
    prelude = (
        "\n    @model_validator(mode='before')\n"
        "    @classmethod\n"
        "    def reshape(cls, data: object) -> object:\n"
        "        return data\n"
    )
    repo = new_repo(
        tmp_path,
        package,
        consumer_fields="    correlation_id: str\n    label: str\n",
        consumer_prelude=prelude,
    )
    (repo / "README.md").write_text("an unrelated change\n")
    commit(repo, "head")
    monkeypatch.syspath_prepend(str(repo / "src"))
    result = _check(repo, package, floor=1)
    assert result.exit_code == 0
    assert result.pairs_compared == 0
    assert result.adapter_skipped_count == 1
    assert any(
        line.startswith("skipped (consumer adapter): ") for line in result.output_lines
    )
