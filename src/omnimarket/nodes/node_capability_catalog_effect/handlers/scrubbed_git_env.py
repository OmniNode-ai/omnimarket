# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An environment copy that cannot redirect a child git at the repository running a hook.

Git exports ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE`` and ``GIT_COMMON_DIR`` into the
environment of every hook it runs, and those variables override both ``cwd=`` and ``git -C``. A
child process started from such an environment can therefore rewrite the invoking repository.
``scrubbed_git_env`` returns a copy with every ``GIT_`` variable removed and the user's global and
system git configuration files ignored, so the child is an ordinary process.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

_GIT_PREFIX = "GIT_"

# Values the scrub sets rather than removes: ignore the real global and system configuration
# files entirely, and never open an editor.
_GIT_ENV_SET: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_EDITOR": "true",
}


def scrubbed_git_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``environ`` (default ``os.environ``) safe to pass as ``env=``.

    Everything outside the ``GIT_`` prefix (``PATH``, ``HOME``, ``TMPDIR``) is preserved.
    """
    source = os.environ if environ is None else environ
    copied = {
        key: value for key, value in source.items() if not key.startswith(_GIT_PREFIX)
    }
    copied.update(_GIT_ENV_SET)
    return copied
