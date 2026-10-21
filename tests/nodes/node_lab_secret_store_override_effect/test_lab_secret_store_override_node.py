"""Legacy parity and the real contract/runtime/handler seam (OMN-20688)."""

from __future__ import annotations

import asyncio
import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_secret_store_override_effect import (
    ModelLabSecretStoreOverrideRequest,
    add_override,
    dispatch,
    main,
)

BASE_CONFIGMAP = Path("../base/runtime/configmap.yaml")
GOLDEN = (
    Path(__file__).parents[2] / "fixtures/lab-secret-store-override/legacy-parity.json"
)


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(GOLDEN.read_text())["cases"]
    return cases


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_transform_matches_frozen_legacy_behavior(case: dict[str, Any]) -> None:
    """Expected data and refusals were captured from the old script before deletion."""
    document = case["input"]
    error = None
    try:
        add_override(
            document,
            "http://127.0.0.1:8080",
            "dev",
            case["namespaces"],
            case["mappings"],
        )
    except (LookupError, TypeError, KeyError, json.JSONDecodeError) as exc:
        error = str(exc)
    assert error == case["error"]
    assert document == case["output"]


def _render_copy(tmp_path: Path) -> Path:
    case = _cases()[0]
    lab = tmp_path / "render"
    lab.mkdir()
    base = tmp_path / "base/runtime"
    base.mkdir(parents=True)
    (base / "configmap.yaml").write_text(
        yaml.safe_dump(
            {
                "data": {
                    "ONEX_SECRET_RESOLVER_CONFIG_JSON": json.dumps(
                        {"mappings": case["mappings"], "namespaces": case["namespaces"]}
                    )
                }
            }
        )
    )
    target = lab / "kustomization.yaml"
    target.write_text(yaml.safe_dump(case["input"]))
    return target


def test_real_runtime_dispatch_edits_the_copy_and_returns_terminal(
    tmp_path: Path,
) -> None:
    target = _render_copy(tmp_path)
    result = asyncio.run(
        dispatch(
            ModelLabSecretStoreOverrideRequest(
                kustomization=target,
                addr=" http://127.0.0.1:8080 ",
                environment_slug=" dev ",
                base_configmap=BASE_CONFIGMAP,
            )
        )
    )
    assert result.exit_code == 0
    assert yaml.safe_load(target.read_text()) == _cases()[0]["output"]
    assert "patches 2 -> 3" in result.message


@pytest.mark.parametrize(("addr", "slug"), [("", "dev"), ("http://127.0.0.1", "  ")])
def test_blank_arguments_refuse_over_the_bus_without_writing(
    tmp_path: Path, addr: str, slug: str
) -> None:
    target = tmp_path / "does-not-exist.yaml"
    result = asyncio.run(
        dispatch(
            ModelLabSecretStoreOverrideRequest(
                kustomization=target,
                addr=addr,
                environment_slug=slug,
                base_configmap=BASE_CONFIGMAP,
            )
        )
    )
    assert result.exit_code == 2
    assert "neither may be blank" in result.message
    assert not target.exists()


def test_invalid_patches_refuse_without_rewriting(tmp_path: Path) -> None:
    target = _render_copy(tmp_path)
    original = "patches: {}\n"
    target.write_text(original)
    result = asyncio.run(
        dispatch(
            ModelLabSecretStoreOverrideRequest(
                kustomization=target,
                addr="http://127.0.0.1:8080",
                environment_slug="dev",
                base_configmap=BASE_CONFIGMAP,
            )
        )
    )
    assert result.exit_code == 1
    assert "not a list" in result.message
    assert target.read_text() == original


def test_cli_usage_exit_code() -> None:
    assert main([]) == 2
    assert main(["a", "b", "c"]) == 2


def test_node_declares_no_shared_runtime_wiring() -> None:
    """A shared runtime must not attach this node: its input is a local render path."""
    package = importlib.import_module(
        "omnimarket.nodes.node_lab_secret_store_override_effect"
    )
    contract = yaml.safe_load(
        (Path(str(package.__file__)).parent / "contract.yaml").read_text()
    )
    assert "event_bus" not in contract
    dispatch_block = contract["runtime_dispatch"]
    assert dispatch_block["command_topic"].startswith("onex.cmd.omnimarket.")
    assert dispatch_block["terminal_events"]["success"] in {
        event["topic"] for event in contract["published_events"]
    }


def test_console_script_resolves_to_main() -> None:
    (script,) = tuple(
        entry_points(group="console_scripts", name="onex-secret-store-override")
    )
    assert script.load() is main
