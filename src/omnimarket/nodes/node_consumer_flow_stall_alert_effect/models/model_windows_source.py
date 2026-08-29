# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the node reads its own window history from — declared, not coded.

OMN-16778 redesign (operator-approved 2026-08-28).  The node reads the trailing
window history itself instead of demanding a caller assemble one, so *which*
relation it reads, and under *which* workload identity, become part of the
contract rather than strings in a handler.

The DSN environment variable is named here on purpose rather than picked in
Python.  OMN-16911 is the precedent and it is a recent one: the consumer-flow
projection writer inherited an omnimarket settings default that prefers
``OMNIDASH_ANALYTICS_DB_URL`` — the dashboard-facing ``role_omnidash`` login,
which holds no USAGE on ``omninode_internal`` — for tables it declares in that
schema.  Every statement was denied on the ``.201`` dev lane,
``consumer_flow_windows`` sat at 0 rows and the DLQ climbed ~6/min.  A handler
that picks its own login role by convention is a handler whose grants nobody
declared, so the binding is written down here and matched against
``db_io.db_tables`` by ``tests/test_omn16778_stall_alert_windows_source.py``.

Like ``alert_policy``, this block carries **no Python defaults**: a missing or
malformed block raises rather than substituting a guess.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_SOURCE_KEY = "windows_source"

#: ``schema.table``, lowercase SQL identifiers only. The relation is
#: interpolated into a SELECT (Postgres cannot parameterize an identifier), so
#: it is validated against this pattern first and rejected otherwise — an
#: operator-editable contract field must not become an injection seam.
_RELATION_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}$")

#: POSIX-portable environment variable name.
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


class ModelWindowsSource(BaseModel):
    """Contract-declared read target for the trailing window history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: str = Field(
        ...,
        description=(
            "Fully-qualified 'schema.table' the trailing history is read from. "
            "Must match the relation declared in db_io.db_tables."
        ),
    )
    binding_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "Deployment-topology binding whose principal holds the grants this "
            "read needs. Recorded so the DSN below is traceable to a declared "
            "workload identity rather than to a convention (OMN-16911)."
        ),
    )
    dsn_env: str = Field(
        ...,
        description=(
            "Environment variable carrying that binding's DSN. Named in the "
            "contract, never chosen in Python."
        ),
    )
    history_windows: int = Field(
        ...,
        ge=1,
        description=(
            "How many trailing windows to read per (consumer_group, topic). "
            "Must be at least the largest hysteresis threshold, or the node "
            "cannot see far enough back to reach a clear verdict."
        ),
    )
    max_keys_per_trigger: int = Field(
        ...,
        ge=1,
        description=(
            "Ceiling on distinct keys evaluated from one applied event. A "
            "platform-wide heartbeat batch can carry hundreds of keys; this "
            "bounds the read fan-out per trigger."
        ),
    )

    def validate_relation(self) -> str:
        """Return the relation, refusing anything that is not a plain identifier.

        Raises:
            WindowsSourceError: The declared relation is not a bare
                ``schema.table`` pair of lowercase SQL identifiers.
        """
        if not _RELATION_RE.fullmatch(self.relation):
            raise WindowsSourceError(
                f"windows_source.relation {self.relation!r} is not a plain "
                "'schema.table' identifier pair; it is interpolated into a "
                "SELECT and must never carry anything else"
            )
        return self.relation


class WindowsSourceError(RuntimeError):
    """The contract does not declare a usable ``windows_source``.

    Raised rather than defaulted, for the same reason
    :class:`~omnimarket.nodes.node_consumer_flow_stall_alert_effect.models.model_stall_alert_policy.StallAlertPolicyError`
    is: a node that invents its own read target is a node reading something
    nobody declared.
    """


def load_windows_source(contract_path: Path) -> ModelWindowsSource:
    """Read the ``windows_source`` block out of a node contract.

    Args:
        contract_path: Path to the ``contract.yaml`` to read. Passed in rather
            than resolved from a module constant so a test can point it at a
            modified copy — the same seam ``load_stall_alert_policy`` uses.

    Returns:
        The parsed, fully-required source declaration, with its relation
        already validated.

    Raises:
        WindowsSourceError: The file is missing, unparseable, carries no
            ``windows_source`` block, declares one that does not validate, or
            names a relation or env var that is not a plain identifier.
    """
    try:
        raw: Any = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WindowsSourceError(
            f"cannot read stall-alert contract at {contract_path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise WindowsSourceError(
            f"stall-alert contract at {contract_path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise WindowsSourceError(
            f"stall-alert contract at {contract_path} is not a mapping"
        )
    block = raw.get(_SOURCE_KEY)
    if not isinstance(block, dict):
        raise WindowsSourceError(
            f"stall-alert contract at {contract_path} declares no "
            f"{_SOURCE_KEY!r} block; this node reads its own window history "
            "and carries no code default for where to read it from"
        )
    source = ModelWindowsSource.model_validate(block)
    source.validate_relation()
    if not _ENV_NAME_RE.fullmatch(source.dsn_env):
        raise WindowsSourceError(
            f"windows_source.dsn_env {source.dsn_env!r} is not a plain "
            "environment variable name"
        )
    return source


__all__ = [
    "ModelWindowsSource",
    "WindowsSourceError",
    "load_windows_source",
]
