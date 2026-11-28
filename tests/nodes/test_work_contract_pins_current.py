"""The work contract corpus loads: every pinned input matches the file on disk."""

from pathlib import Path

import pytest

from omnimarket.handlers.work_contracts.loader import (
    load_corpus,
    pin_digest,
    pinned_path,
)
from omnimarket.nodes.node_model_work_contract_compute.models.model_work_contract import (
    InputPin,
)

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts"


def test_work_contract_corpus_pins_match_tree() -> None:
    contracts = load_corpus(CORPUS, input_root=ROOT)
    assert contracts


def test_work_contract_environment_pins_match_uv_lock() -> None:
    import tomllib

    import yaml

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    version = next(p["version"] for p in lock["package"] if p["name"] == "omnimarket")
    assert tuple(int(x) for x in version.split(".")) >= (0, 4, 321), (
        f"uv.lock predates the post-release re-lock: omnimarket {version}"
    )
    lock_digest = pin_digest(ROOT / "uv.lock")
    mismatched = []
    for path in sorted((CORPUS / "contracts").glob("*.yaml")):
        data = yaml.safe_load(path.read_bytes())
        spec = (data.get("acceptance") or {}).get("spec") or {}
        pin = spec.get("environment")
        if (
            isinstance(pin, dict)
            and pin.get("path") == "uv.lock"
            and pin.get("sha256") != lock_digest
        ):
            mismatched.append(path.name)
    assert not mismatched, f"environment pin differs from uv.lock: {mismatched[:5]}"


def _bumped(lock: str, *, version: str, revision: int) -> str:
    """What the post-release bot writes: own version line and lock revision only."""
    head, sep, tail = lock.partition('[[package]]\nname = "omnimarket"\nversion = "')
    assert sep, "uv.lock has no omnimarket self entry"
    _, _, rest = tail.partition('"')
    out = f'{head}{sep}{version}"{rest}'
    return out.replace(
        next(ln for ln in out.splitlines() if ln.startswith("revision = ")),
        f"revision = {revision}",
        1,
    )


def test_environment_pin_survives_the_post_release_bump(tmp_path: Path) -> None:
    """A bot bump (own version + lock revision) must not break the pin."""
    lock = (ROOT / "uv.lock").read_text()
    bumped = _bumped(lock, version="9.9.9", revision=5)
    assert bumped != lock
    (tmp_path / "uv.lock").write_text(bumped)
    pin = InputPin(path="uv.lock", sha256=pin_digest(ROOT / "uv.lock"))
    assert pinned_path(tmp_path, pin) == (tmp_path / "uv.lock").resolve()


def test_environment_pin_still_refuses_a_dependency_change(tmp_path: Path) -> None:
    """Known bad: a changed dependency version still fails the pin."""
    lock = (ROOT / "uv.lock").read_text()
    changed = lock.replace(
        'name = "pytest"\nversion = ', 'name = "pytest"\nversion = "0.0.1" #', 1
    )
    assert changed != lock
    (tmp_path / "uv.lock").write_text(changed)
    pin = InputPin(path="uv.lock", sha256=pin_digest(ROOT / "uv.lock"))
    with pytest.raises(ValueError, match=r"input_pin_mismatch: uv\.lock"):
        pinned_path(tmp_path, pin)


def test_non_lock_pins_stay_raw_sha256(tmp_path: Path) -> None:
    import hashlib

    f = tmp_path / "other.txt"
    f.write_text('revision = 3\n[[package]]\nname = "omnimarket"\nversion = "1"\n')
    assert pin_digest(f) == hashlib.sha256(f.read_bytes()).hexdigest()
