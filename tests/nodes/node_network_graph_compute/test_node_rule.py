# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N1.6: the node rule holds for the compute node.

The node imports no I/O library, takes command_segment, domain_segment_index and
window from the overlay it is handed, sorts every list by key and gives the same
bytes twice. Each omnibase_core checker passes over the node directory and rejects
one planted violation, so a pass is never an unread zero.
"""

from __future__ import annotations

import ast
import shutil
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from omnibase_core.models.common.model_validation_result import ModelValidationResult
from omnibase_core.validation import checker_literal_duplication
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi
from omnibase_core.validation.validator_handler_di import ValidatorHandlerDi
from omnibase_core.validation.validator_hardcoded_model_literals import (
    ValidatorHardcodedModelLiterals,
)
from omnibase_core.validation.validator_hardcoded_topics import ValidatorHardcodedTopics
from omnibase_core.validation.validator_inline_logic import ValidatorInlineLogic
from omnibase_core.validation.validator_no_io_outside_effects import (
    ValidatorNoIoOutsideEffects,
)

from omnimarket.models.network_graph import ModelNetworkGraphOverlay
from omnimarket.nodes.node_network_graph_compute.handlers.handler_network_graph_compute import (
    HandlerNetworkGraphCompute,
)

from .support import OVERLAY, bus_text, contract, node_text, request

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_NODE_REL = Path("src/omnimarket/nodes/node_network_graph_compute")
_NODE = _REPO / _NODE_REL
_MODELS = _REPO / "src/omnimarket/models/network_graph"
_CONTRACT_OF_TICKET = _REPO / "contracts/OMN-22255.yaml"

_IO_ROOTS = frozenset(
    {
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "urllib",
        "aiokafka",
        "confluent_kafka",
        "psycopg",
        "asyncpg",
        "sqlite3",
        "shutil",
        "glob",
        "os",
        "pathlib",
        "tempfile",
    }
)


@pytest.fixture
def plant_root() -> Iterator[Path]:
    """A scratch root whose path holds no test marker the checkers exempt."""
    with tempfile.TemporaryDirectory(prefix="plant_") as root:
        yield Path(root)


def _issues(result: ModelValidationResult[None]) -> list[str]:
    return [issue.message for issue in result.issues]


def _tree(plant_root: Path) -> Path:
    """A copy of the node under a src/omnimarket/nodes path, where scopes apply."""
    dst = plant_root / _NODE_REL
    shutil.copytree(_NODE, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def _plant(plant_root: Path, rel: str, body: str) -> Path:
    path = _tree(plant_root) / rel
    path.write_text(body, encoding="utf-8")
    return path


# --- the node's own rules -------------------------------------------------------------


def _node_sources() -> list[Path]:
    return sorted([*_NODE.rglob("*.py"), *_MODELS.rglob("*.py")])


def test_node_rule_imports_no_io_library() -> None:
    offenders: list[str] = []
    for source in _node_sources():
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [f"{source.name}: {r}" for r in roots if r in _IO_ROOTS]
    assert offenders == []


def test_node_rule_planted_io_import_is_found_by_the_import_scan() -> None:
    tree = ast.parse("import subprocess\nfrom pathlib import Path\n")
    roots = {
        a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
    } | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert roots & _IO_ROOTS == {"subprocess", "pathlib"}


def test_node_rule_values_come_from_the_handed_overlay() -> None:
    contracts = (contract("r", "a/contract.yaml", bus_text("a", ("t.x",), ("t.y",))),)
    other = ModelNetworkGraphOverlay(
        command_segment="order", domain_segment_index=5, window=timedelta(hours=3)
    )
    first = HandlerNetworkGraphCompute().handle(request(*contracts))
    second = HandlerNetworkGraphCompute().handle(
        request(*contracts).model_copy(update={"overlay": other})
    )
    assert first.overlay == OVERLAY
    assert second.overlay == other
    assert first.vertices == second.vertices
    assert first.edges == second.edges


def test_node_rule_two_runs_on_the_same_input_give_identical_bytes() -> None:
    texts = (
        contract("r", "b/contract.yaml", bus_text("b", ("t.q",), ("t.p",))),
        contract("r", "a/contract.yaml", bus_text("a", ("t.p",), ("t.q",))),
        contract("r", "c/contract.yaml", node_text("c", "event_bus: [unclosed\n")),
    )
    one = HandlerNetworkGraphCompute().handle(request(*texts))
    two = HandlerNetworkGraphCompute().handle(request(*texts))
    assert one.model_dump_json() == two.model_dump_json()
    assert one.digest() == two.digest()


def test_node_rule_every_list_is_sorted_by_key() -> None:
    document = HandlerNetworkGraphCompute().handle(
        request(
            contract(
                "z", "n/contract.yaml", bus_text("n", ("t.2", "t.1"), ("t.9", "t.3"))
            ),
            contract("a", "n/contract.yaml", bus_text("n", ("t.3",), ("t.1", "t.2"))),
        )
    )
    keys = [v.key for v in document.vertices]
    assert keys == sorted(keys)
    edges = [(e.publisher, e.topic, e.subscriber) for e in document.edges]
    assert edges == sorted(edges)
    for vertex in document.vertices:
        assert list(vertex.publishes) == sorted(vertex.publishes)
        assert list(vertex.subscribes) == sorted(vertex.subscribes)


# --- the checkers: pass on the node, reject one planted violation each ----------------

_PLANT_IO = "import subprocess\n\n\ndef run() -> None:\n    subprocess.run(['true'], check=False)\n"
_PLANT_DI = (
    "class HandlerPlant:\n"
    "    def __init__(self, client: object | None = None) -> None:\n"
    "        self.client = client or object()\n"
)
_PLANT_HAND = (
    "class HandlerPlant:\n"
    "    def handle(self, request: object) -> object:\n"
    "        return HandlerPlant()\n"
)
_PLANT_NUMBER = "DEFAULT_WINDOW = 86400\n"
_PLANT_TOPIC = 'TOPIC = "onex.evt.fixture.planted.v1"\n'
_PLANT_PROGRAM = (
    "ticket_id: OMN-1\n"
    "dod_evidence:\n"
    "  - id: d1\n"
    "    checks:\n"
    "      - check_type: command\n"
    "        check_value: \"python -c 'import sys\\nsys.exit(0)'\"\n"
)


def _no_io(plant_root: Path, plant: Path | None) -> list[str]:
    validator = ValidatorNoIoOutsideEffects(root=plant_root)
    return _issues(validator.validate(plant if plant else plant_root / _NODE_REL))


def test_node_rule_no_io_checker_passes_the_node_and_rejects_a_plant(
    plant_root: Path,
) -> None:
    _tree(plant_root)
    assert _no_io(plant_root, None) == []
    plant = plant_root / _NODE_REL / "handlers/plant_io.py"
    plant.write_text(_PLANT_IO, encoding="utf-8")
    assert _no_io(plant_root, plant)


def test_node_rule_handler_di_checker_passes_the_node_and_rejects_a_plant(
    plant_root: Path,
) -> None:
    assert _issues(ValidatorHandlerDi().validate(_NODE)) == []
    plant = _plant(plant_root, "handlers/plant_di.py", _PLANT_DI)
    assert _issues(ValidatorHandlerDi().validate(plant))


def test_node_rule_contracts_and_di_checker_passes_the_node_and_rejects_a_plant(
    plant_root: Path,
) -> None:
    assert _issues(ValidatorContractsAndDi().validate(_NODE)) == []
    assert _issues(ValidatorContractsAndDi().validate(_MODELS)) == []
    plant = _plant(plant_root, "handlers/plant_hand.py", _PLANT_HAND)
    assert _issues(ValidatorContractsAndDi().validate(plant))


def test_node_rule_hardcoded_literals_checker_passes_the_node_and_rejects_a_plant(
    plant_root: Path,
) -> None:
    assert _issues(ValidatorHardcodedModelLiterals().validate(_NODE)) == []
    assert _issues(ValidatorHardcodedModelLiterals().validate(_MODELS)) == []
    plant = _plant(plant_root, "handlers/plant_number.py", _PLANT_NUMBER)
    assert _issues(ValidatorHardcodedModelLiterals().validate(plant))


def test_node_rule_hardcoded_topics_checker_passes_the_node_and_rejects_a_plant(
    plant_root: Path,
) -> None:
    assert _issues(ValidatorHardcodedTopics().validate(_NODE / "handlers")) == []
    plant = _plant(plant_root, "handlers/plant_topic.py", _PLANT_TOPIC)
    assert _issues(ValidatorHardcodedTopics().validate(plant))


def test_node_rule_inline_logic_checker_passes_the_ticket_contract_and_rejects_a_plant(
    plant_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_REPO)
    assert _issues(ValidatorInlineLogic().validate(_CONTRACT_OF_TICKET)) == []
    (plant_root / "contracts").mkdir()
    plant = plant_root / "contracts/OMN-99999.yaml"
    plant.write_text(_PLANT_PROGRAM, encoding="utf-8")
    monkeypatch.chdir(plant_root)
    assert _issues(ValidatorInlineLogic().validate(plant))


def _duplication(paths: list[Path]) -> list[str]:
    known = checker_literal_duplication.get_enum_names_from_directory(
        checker_literal_duplication.find_enums_directory() or Path()
    )
    messages: list[str] = []
    for path in paths:
        messages += checker_literal_duplication.validate_file(path, known)
    return messages


def test_node_rule_literal_duplication_checker_passes_the_node_and_rejects_a_plant(
    plant_root: Path,
) -> None:
    assert _duplication(_node_sources()) == []
    plant = plant_root / "plant_literal.py"
    plant.write_text(
        "from typing import Literal\n\nLiteralValidationLevel = Literal['a', 'b']\n",
        encoding="utf-8",
    )
    assert _duplication([plant])
