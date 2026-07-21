# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Governed config loaders for the OMN-14890 author-identity independence gate.

Both files are GOVERNED DATA (not code), mirroring the posture of
``omnibase_core/contracts/runtime_ops_verb_allowlist.yaml``: adding an
identity alias or sanctioning a new automation identity is a reviewable
governance change (edit the YAML, get it reviewed), never a code edit.

* ``identity_aliases.yaml`` — {raw_identity_lower: canonical_id} so one
  physical operator cannot launder independence via a second address that
  resolves to the same person.
* ``sanctioned_occ_autobind_identities.yaml`` — identities treated as
  independent-by-construction sanctioned automation (bot git-author email /
  GitHub App login), regardless of the product-PR author.

Both loaders fail SAFE (return an empty map/set), not fail-open: a missing or
malformed governance file must not crash Linear triage, and an empty alias
map / empty sanctioned set is the conservative default (no aliasing, no
automation exemption) rather than a silent bypass.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

# omnimarket/src/omnimarket/nodes/node_linear_triage/services/<this file>
# parents[3] == omnimarket/src/omnimarket (mirrors the ROUTING_TIERS_YAML
# resolution pattern in omnimarket/src/omnimarket/pricing.py).
_CONFIGS_DIR = Path(__file__).resolve().parents[3] / "configs"
_ALIASES_YAML = _CONFIGS_DIR / "identity_aliases.yaml"
_SANCTIONED_YAML = _CONFIGS_DIR / "sanctioned_occ_autobind_identities.yaml"


@cache
def load_identity_aliases() -> dict[str, str]:
    """Return the governed ``{raw_identity_lower: canonical_id}`` alias map.

    Cached for the process lifetime — re-reads only happen in a fresh
    interpreter (tests should not rely on hot-reloading an edited YAML
    within the same process).
    """
    try:
        raw = yaml.safe_load(_ALIASES_YAML.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}
    aliases = raw.get("aliases")
    if not isinstance(aliases, dict):
        return {}
    return {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in aliases.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }


@cache
def load_sanctioned_occ_autobind_identities() -> frozenset[str]:
    """Return the governed sanctioned-automation identity allowlist (lower-cased)."""
    try:
        raw = yaml.safe_load(_SANCTIONED_YAML.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    identities = raw.get("sanctioned_identities")
    if not isinstance(identities, list):
        return frozenset()
    return frozenset(
        str(v).strip().lower() for v in identities if isinstance(v, str) and v.strip()
    )


__all__ = [
    "load_identity_aliases",
    "load_sanctioned_occ_autobind_identities",
]
