# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime's writable state root, resolved fail-fast from its declared env.

OMN-19654. A deployed runtime pod runs with ``readOnlyRootFilesystem: true``
and ``HOME=/``, so any state written under a ``Path.home()``-derived default
lands on ``/`` and fails with ``[Errno 30] Read-only file system``. The runtime
manifests already declare where writable state goes -- ``ONEX_STATE_DIR``
(the onex-dev family, including the onex-lab lane) and ``ONEX_STATE_ROOT``
(both that family and the prod runtime ConfigMap), each backed by a writable
volume -- so state that belongs to a running runtime is resolved from those,
in that order, and nowhere else.

There is no fallback. When neither is set this raises a named error rather
than choosing a home, a working directory or a temp directory: a silent
default is how the claim store ended up on a read-only root in the first place
(CLAUDE.md rules 6 and 8).

These two keys are bootstrap-only by the OMN-10548 settings design, which is
why this reads them directly rather than through ``Settings``; the read is
confined to this one module so that callers do not each re-derive it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

ONEX_STATE_ROOT_ENV_KEYS: Final[tuple[str, ...]] = ("ONEX_STATE_DIR", "ONEX_STATE_ROOT")


class OnexStateRootUnconfiguredError(RuntimeError):
    """No writable runtime state root is declared for this process."""

    def __init__(self, purpose: str) -> None:
        super().__init__(
            f"{purpose} needs a writable runtime state root, and none is "
            f"configured: set one of {', '.join(ONEX_STATE_ROOT_ENV_KEYS)} to a "
            "writable directory (a deployed runtime mounts a volume there). "
            "There is no home-directory or working-directory fallback "
            "(OMN-19654)."
        )
        self.purpose = purpose


def resolve_onex_state_root(*, purpose: str) -> Path:
    """Return the declared state root, or raise naming ``purpose`` and the keys.

    ``ONEX_STATE_DIR`` wins over ``ONEX_STATE_ROOT``, matching every other
    runtime state-root reader in this package. A value that is empty or only
    whitespace counts as unset.
    """
    for key in ONEX_STATE_ROOT_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value)
    raise OnexStateRootUnconfiguredError(purpose)


__all__ = [
    "ONEX_STATE_ROOT_ENV_KEYS",
    "OnexStateRootUnconfiguredError",
    "resolve_onex_state_root",
]
