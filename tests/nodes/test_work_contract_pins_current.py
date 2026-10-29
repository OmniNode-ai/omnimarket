"""The work contract corpus loads: every pinned input matches the file on disk."""

from pathlib import Path

from omnimarket.handlers.work_contracts.loader import load_corpus

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts"


def test_work_contract_corpus_pins_match_tree() -> None:
    contracts = load_corpus(CORPUS, input_root=ROOT)
    assert contracts


def test_work_contract_environment_pins_match_uv_lock() -> None:
    import hashlib
    import tomllib

    import yaml

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    version = next(p["version"] for p in lock["package"] if p["name"] == "omnimarket")
    assert tuple(int(x) for x in version.split(".")) >= (0, 4, 321), (
        f"uv.lock predates the post-release re-lock: omnimarket {version}"
    )
    lock_digest = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
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
