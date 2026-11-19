"""Node lane github read guard effect entry point and contract golden test."""

import copy
import importlib
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE_NAME = "node_lane_gh_read_guard_effect"
FOREIGN_MODULE = "omniclaude.nodes.node_lane_gh_read_guard_effect.models"


def _load_entries() -> dict[str, str]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]


def _load_contract() -> dict:
    path = ROOT / "src/omnimarket/nodes" / NODE_NAME / "contract.yaml"
    return yaml.safe_load(path.read_text())


def _module_values(contract: dict) -> list[str]:
    values: list[str] = []
    if isinstance(contract, dict):
        for key, value in contract.items():
            if key == "module" and isinstance(value, str):
                values.append(value)
            elif isinstance(value, (dict, list)):
                values.extend(_module_values(value))
    elif isinstance(contract, list):
        for item in contract:
            if isinstance(item, (dict, list)):
                values.extend(_module_values(item))
    return values


def _handler_event_model_pairs(contract: dict) -> list[tuple[dict, dict]]:
    pairs: list[tuple[dict, dict]] = []
    if isinstance(contract, dict):
        if isinstance(contract.get("handler"), dict) and isinstance(
            contract.get("event_model"), dict
        ):
            pairs.append((contract["handler"], contract["event_model"]))
        for value in contract.values():
            pairs.extend(_handler_event_model_pairs(value))
    elif isinstance(contract, list):
        for item in contract:
            pairs.extend(_handler_event_model_pairs(item))
    return pairs


def foreign_modules(contract: dict) -> list[str]:
    return [
        value
        for value in _module_values(contract)
        if not value.startswith("omnimarket.")
    ]


def test_entry_point_is_registered_and_contract_present() -> None:
    entries = _load_entries()
    assert entries[NODE_NAME] == f"omnimarket.nodes.{NODE_NAME}"
    assert (ROOT / "src/omnimarket/nodes" / NODE_NAME / "contract.yaml").is_file()


def test_contract_modules_import_and_names_resolve() -> None:
    contract = _load_contract()
    modules = _module_values(contract)
    assert modules, "contract.yaml declares no module values"
    for module in modules:
        assert module.startswith("omnimarket."), f"foreign module {module}"
        importlib.import_module(module)
    for handler, event_model in _handler_event_model_pairs(contract):
        handler_module = importlib.import_module(handler["module"])
        assert getattr(handler_module, handler["name"]) is not None
        event_module = importlib.import_module(event_model["module"])
        assert getattr(event_module, event_model["name"]) is not None


def test_foreign_modules_reports_rewritten_module() -> None:
    contract = _load_contract()
    assert foreign_modules(contract) == []
    broken = _deep_copy_with_foreign_module(contract)
    assert foreign_modules(broken) == [FOREIGN_MODULE]


def _deep_copy_with_foreign_module(contract: dict) -> dict:
    broken = copy.deepcopy(contract)
    _rewrite_first_module(broken, FOREIGN_MODULE)
    return broken


def _rewrite_first_module(node: object, replacement: str) -> bool:
    if isinstance(node, dict):
        if isinstance(node.get("module"), str):
            node["module"] = replacement
            return True
        return any(_rewrite_first_module(value, replacement) for value in node.values())
    if isinstance(node, list):
        return any(_rewrite_first_module(item, replacement) for item in node)
    return False
