"""Read an environment variable whose name the operator supplies at run time."""

from __future__ import annotations

import os


def read_named_env(env_name: str) -> str:
    """Return the value of ``env_name``, failing fast with ``KeyError`` when unset."""
    return os.environ[env_name]
