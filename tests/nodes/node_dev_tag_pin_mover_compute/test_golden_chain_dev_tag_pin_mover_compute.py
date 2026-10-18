# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_dev_tag_pin_mover_compute [OMN-19998].

The contract and metadata load, the handler is routed from the contract, a move
runs end to end through the CLI (candidate, then accepted), and every refusal
leaves the pin where it was. The negative control: a consumer whose tests fail
on the new tag is refused and its file is not rewritten.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dev_tag_pin_mover_compute.__main__ import main
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models import (
    EnumConsumerTestOutcome,
    EnumPinMoveRefusal,
    ModelDevTagPinMoveRequest,
    ModelDevTagPinMoveResult,
)

pytestmark = pytest.mark.unit

NODE_NAME = "node_dev_tag_pin_mover_compute"
NODE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
URL = "https://github.com/OmniNode-ai/omnibase_infra.git"
OLD = "dev-20261016204728-0f7bee98bd8f"
NEW = "dev-20261018005623-b5f4c2e36e72"
PYPROJECT = (
    "[tool.uv.sources]\n"
    "# a comment naming omnibase-infra stays put\n"
    f'omnibase-infra = {{ git = "{URL}", tag = "{OLD}" }}\n'
    'omnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
    'tag = "dev-20261001000000-aaaaaaaaaaaa" }\n'
)


def _handle(**overrides: Any) -> ModelDevTagPinMoveResult:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    module = importlib.import_module(contract["handler"]["module"])
    handler = getattr(module, contract["handler"]["class"])()
    fields: dict[str, Any] = {
        "pyproject_text": PYPROJECT,
        "distribution": "omnibase-infra",
        "repository_url": URL,
        "new_tag": NEW,
        "consumer_tests": EnumConsumerTestOutcome.PASSED,
    }
    fields.update(overrides)
    result = handler.handle(ModelDevTagPinMoveRequest(**fields))
    assert isinstance(result, ModelDevTagPinMoveResult)
    return result


def test_contract_names_the_node_and_declares_no_bus_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == NODE_NAME
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"]["external_trigger"]
    assert contract["descriptor"]["purity"] == "pure"


def test_metadata_registers_the_entry_point() -> None:
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == NODE_NAME
    assert (
        metadata["entry_points"]["onex.nodes"][NODE_NAME]
        == f"omnimarket.nodes.{NODE_NAME}"
    )


def test_passed_tests_move_only_the_named_sibling() -> None:
    result = _handle()
    assert result.accepted
    assert (result.current_tag, result.new_tag) == (OLD, NEW)
    assert f'tag = "{NEW}"' in result.candidate_text
    assert OLD not in result.candidate_text
    assert "dev-20261001000000-aaaaaaaaaaaa" in result.candidate_text
    assert "# a comment naming omnibase-infra stays put" in result.candidate_text


def test_failed_tests_refuse_the_move() -> None:
    result = _handle(consumer_tests=EnumConsumerTestOutcome.FAILED)
    assert not result.accepted
    assert result.refusal is EnumPinMoveRefusal.CONSUMER_TESTS_FAILED
    assert result.candidate_text == ""


def test_untested_move_returns_a_candidate_but_is_not_accepted() -> None:
    result = _handle(consumer_tests=EnumConsumerTestOutcome.NOT_RUN)
    assert not result.accepted
    assert result.refusal is EnumPinMoveRefusal.CONSUMER_TESTS_NOT_RUN
    assert f'tag = "{NEW}"' in result.candidate_text


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"new_tag": "v0.38.75"}, EnumPinMoveRefusal.NOT_A_DEV_TAG),
        ({"new_tag": "dev-2026-b5f4c2e36e72"}, EnumPinMoveRefusal.NOT_A_DEV_TAG),
        ({"new_tag": OLD}, EnumPinMoveRefusal.NOT_FORWARD),
        (
            {"new_tag": "dev-20261015000000-0123456789ab"},
            EnumPinMoveRefusal.NOT_FORWARD,
        ),
        ({"distribution": "omnibase-spi"}, EnumPinMoveRefusal.NO_TAG_PIN),
        (
            {"repository_url": "https://github.com/OmniNode-ai/other.git"},
            EnumPinMoveRefusal.NO_TAG_PIN,
        ),
        (
            {"pyproject_text": '[project]\ndependencies = ["omnibase-infra>=0.38"]\n'},
            EnumPinMoveRefusal.NO_TAG_PIN,
        ),
    ],
)
def test_refusals(overrides: dict[str, Any], refusal: EnumPinMoveRefusal) -> None:
    result = _handle(**overrides)
    assert not result.accepted
    assert result.refusal is refusal


def test_a_release_tag_pin_moves_to_a_dev_tag() -> None:
    text = f'omnibase-infra = {{ git = "{URL}", tag = "v0.38.74" }}\n'
    result = _handle(pyproject_text=text)
    assert result.accepted
    assert (
        result.candidate_text
        == f'omnibase-infra = {{ git = "{URL}", tag = "{NEW}" }}\n'
    )


def _cli(path: Path, tests: str) -> int:
    return main(
        [
            "--pyproject",
            str(path),
            "--distribution",
            "omnibase-infra",
            "--repository-url",
            URL,
            "--tag",
            NEW,
            "--tests",
            tests,
        ]
    )


def test_cli_writes_candidate_then_move_and_leaves_a_failed_move_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT)
    code = _cli(path, "failed")
    assert code == 1
    assert path.read_text() == PYPROJECT

    code = _cli(path, "not_run")
    assert code == 3
    assert f'tag = "{NEW}"' in path.read_text()

    path.write_text(PYPROJECT)
    code = _cli(path, "passed")
    assert code == 0
    assert f'tag = "{NEW}"' in path.read_text()
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last)["accepted"] is True
