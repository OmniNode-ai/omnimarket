from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import yaml
from click.testing import CliRunner

from omnimarket.cli import market as market_module
from omnimarket.cli.market import market


def _write_installed_node(root: Path) -> Path:
    package_path = root / "registry" / "node_ticket_pipeline"
    package_path.mkdir(parents=True)
    (package_path / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "node_ticket_pipeline",
                "version": "1.0.0",
                "description": "Per-ticket execution pipeline",
                "entry_points": {
                    "onex.nodes": {
                        "node_ticket_pipeline": "omnimarket.nodes.node_ticket_pipeline"
                    }
                },
                "capabilities": {
                    "standalone": True,
                    "full_runtime": True,
                    "requires_network": False,
                    "requires_repo": False,
                    "requires_secrets": False,
                    "requires_docker": False,
                    "side_effect_class": "read_only",
                },
                "tags": ["ticket", "pipeline"],
                "display_name": "Ticket Pipeline",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    registry_path = root / "installed_nodes.json"
    registry_path.write_text(
        json.dumps(
            {
                "node_ticket_pipeline": {
                    "package": "node_ticket_pipeline",
                    "version": "1.0.0",
                    "source": "oncp",
                    "path": str(package_path),
                }
            }
        ),
        encoding="utf-8",
    )
    return registry_path


def test_market_list_prints_installed_package_capabilities(tmp_path: Path) -> None:
    registry_path = _write_installed_node(tmp_path)

    result = CliRunner().invoke(market, ["list", "--registry-path", str(registry_path)])

    assert result.exit_code == 0
    assert "node_ticket_pipeline v1.0.0 - Ticket Pipeline" in result.output
    assert "capabilities: standalone,runtime,read_only" in result.output


def test_market_list_json(tmp_path: Path) -> None:
    registry_path = _write_installed_node(tmp_path)

    result = CliRunner().invoke(
        market, ["list", "--registry-path", str(registry_path), "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["packages"][0]["name"] == "node_ticket_pipeline"
    assert payload["packages"][0]["capabilities"]["standalone"] is True


def test_market_search_reads_index_and_matches_tags(tmp_path: Path) -> None:
    index_path = tmp_path / "index.yaml"
    index_path.write_text(
        yaml.safe_dump(
            {
                "packages": [
                    {
                        "name": "node_gap_compute",
                        "package": "omnimarket",
                        "version": "1.0.0",
                        "description": "Gap analysis compute node",
                        "tags": ["planning", "gap"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        market, ["search", "planning", "--index-path", str(index_path), "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["matches"][0]["name"] == "node_gap_compute"


def test_market_install_delegates_to_core_install(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_invoke_core_install(ctx, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(market_module, "_invoke_core_install", fake_invoke_core_install)

    result = CliRunner().invoke(
        market,
        [
            "install",
            "node_ticket_pipeline.oncp",
            "--dry-run",
            "--upgrade",
            "--allow-unsigned",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "package_path": "node_ticket_pipeline.oncp",
            "test": False,
            "dry_run": True,
            "upgrade": True,
            "allow_unsigned": True,
            "verbose": False,
        }
    ]
    payload = json.loads(result.output)
    assert payload["delegated_to"] == "onex install"


def test_market_cli_entry_point_registered() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert (
        data["project"]["entry-points"]["onex.cli"]["market"]
        == "omnimarket.cli.market:market"
    )
