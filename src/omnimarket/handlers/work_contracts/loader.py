"""Filesystem binding: validate every pin before making a contract runnable."""

import re
from hashlib import sha256
from pathlib import Path

import yaml

from omnimarket.nodes.node_model_work_contract_compute.models.model_work_contract import (
    InputPin,
    ModelWorkContract,
)

_LOCK_REVISION = re.compile(r"^revision = \d+$", re.MULTILINE)
_LOCK_SELF_VERSION = re.compile(
    r'(\[\[package\]\]\nname = "omnimarket"\nversion = )"[^"\n]*"'
)


def pin_digest(path: Path) -> str:
    """Digest a pinned input; uv.lock is digested without its two bump lines.

    The post-release bot rewrites only the omnimarket self-entry version and the
    lock-format revision. Neither describes the dependency environment the pin
    guards, so both are masked; every other byte of uv.lock still counts.
    """
    data = path.read_bytes()
    if path.name == "uv.lock":
        text = _LOCK_REVISION.sub("revision = *", data.decode(), count=1)
        text = _LOCK_SELF_VERSION.sub(r'\1"*"', text, count=1)
        data = text.encode()
    return sha256(data).hexdigest()


def pinned_path(root: Path, pin: InputPin) -> Path:
    path = (root / pin.path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"outside input root: {pin.path}")
    if pin_digest(path) != pin.sha256:
        raise ValueError(f"input_pin_mismatch: {pin.path}")
    return path


def verify_pins(contract: ModelWorkContract, root: Path) -> None:
    pins = (*contract.inputs, *contract.evaluators)
    if contract.acceptance.kind != "schema":
        pins += (contract.acceptance.spec.environment,)
    if contract.acceptance.kind == "seeded_bug":
        pins += (contract.acceptance.spec.patch,)
    if contract.controls:
        pins += (contract.controls.positive, contract.controls.negative)
    for pin in pins:
        pinned_path(root, pin)


def load_contract(path: Path, *, input_root: Path) -> ModelWorkContract:
    contract = ModelWorkContract.model_validate(yaml.safe_load(path.read_text()))
    verify_pins(contract, input_root)
    return contract


def load_corpus(
    root: Path, *, input_root: Path | None = None
) -> tuple[ModelWorkContract, ...]:
    contracts = tuple(
        load_contract(p, input_root=input_root or root)
        for p in sorted((root / "contracts").glob("*.yaml"))
    )
    ids = [c.contract_id for c in contracts]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate contract_id")
    return contracts
