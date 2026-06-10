"""Contract-derived event-bus topic and secrets helpers for node handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def contract_subscribe_topics(contract_path: Path) -> tuple[str, ...]:
    """Return subscribe topics declared by a node contract."""
    return _contract_topics(contract_path, "subscribe_topics")


def contract_publish_topics(contract_path: Path) -> tuple[str, ...]:
    """Return publish topics declared by a node contract."""
    return _contract_topics(contract_path, "publish_topics")


def contract_secret_ref(contract_path: Path, secret_name: str) -> str:
    """Return the declared secret ref-name for *secret_name* from the contract.

    The contract ``secrets`` block maps logical secret names to their
    ``ProtocolSecretStore`` reference (the env-var / Infisical key name used to
    look up the value).  For ONEX GitHub nodes the block looks like::

        secrets:
          GITHUB_TOKEN:
            description: "..."
            required: true

    The *ref-name* is the dict key itself (``GITHUB_TOKEN`` in the example above).
    The handler calls this at startup so the literal secret reference lives only
    in the contract, never as a bare string in source.

    Args:
        contract_path: Path to the node's ``contract.yaml``.
        secret_name: Logical name of the secret (key under ``secrets:``).

    Returns:
        The secret reference name (the store key), equal to *secret_name*
        by convention.  Fails fast if the contract does not declare the secret.

    Raises:
        ValueError: When the contract has no ``secrets`` block or does not
            declare *secret_name*.
    """
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")

    secrets_block = raw.get("secrets")
    if not isinstance(secrets_block, dict):
        raise ValueError(
            f"{contract_path} does not declare a 'secrets' block; "
            f"cannot resolve secret ref for {secret_name!r}."
        )
    if secret_name not in secrets_block:
        raise ValueError(
            f"{contract_path} 'secrets' block does not declare {secret_name!r}. "
            "Add the secret to the contract before using contract_secret_ref()."
        )
    # By convention the ref-name equals the key.
    return secret_name


def _contract_topics(contract_path: Path, key: str) -> tuple[str, ...]:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")

    event_bus = raw.get("event_bus")
    if not isinstance(event_bus, dict):
        raise ValueError(f"{contract_path} missing event_bus mapping")

    topics: Any = event_bus.get(key)
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise ValueError(f"{contract_path} event_bus.{key} must be a string list")
    return tuple(topics)


__all__ = [
    "contract_publish_topics",
    "contract_secret_ref",
    "contract_subscribe_topics",
]
