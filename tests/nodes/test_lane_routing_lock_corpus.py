"""The routing nodes ship with a lock the work-contract corpus pins exactly."""

import hashlib
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts/contracts"
LANE_NODES = ("node_lane_block_diagnose_compute", "node_lane_block_diagnose_effect")


def test_lane_routing_nodes_are_registered_entry_points() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    for name in LANE_NODES:
        assert entries.get(name) == f"omnimarket.nodes.{name}"
        assert (ROOT / "src/omnimarket/nodes" / name / "contract.yaml").is_file()


def test_lane_routing_lock_matches_every_corpus_environment_pin() -> None:
    digest = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    pinned = 0
    for path in sorted(CORPUS.glob("*.yaml")):
        spec = (yaml.safe_load(path.read_bytes()).get("acceptance") or {}).get(
            "spec"
        ) or {}
        pin = spec.get("environment")
        if isinstance(pin, dict) and pin.get("path") == "uv.lock":
            pinned += 1
            assert pin.get("sha256") == digest, path.name
    assert pinned
