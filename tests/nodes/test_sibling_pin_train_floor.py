"""The nightly sibling-pin train moved the sibling pins forward and the lock agrees."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]

# Floor of the sibling dev tags the train rolled; later nightly moves only raise it.
FLOORS = {
    "omnibase-core": "20261105194015",
    "omnibase-infra": "20261108054615",
    "omnibase-spi": "20261108015921",
    "omninode-memory": "20261106074413",
}


def _stamp(tag: str) -> str:
    match = re.fullmatch(r"dev-(\d{14})-[0-9a-f]{12}", tag)
    assert match, f"unexpected dev tag shape: {tag}"
    return match.group(1)


def test_sibling_pins_at_or_past_train_floor() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sources = pyproject["tool"]["uv"]["sources"]
    for name, floor in FLOORS.items():
        assert _stamp(sources[name]["tag"]) >= floor, name


def test_uv_lock_resolves_the_pyproject_sibling_tags() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sources = pyproject["tool"]["uv"]["sources"]
    lock_text = (ROOT / "uv.lock").read_text()
    for name in FLOORS:
        assert f"?tag={sources[name]['tag']}#" in lock_text, name
