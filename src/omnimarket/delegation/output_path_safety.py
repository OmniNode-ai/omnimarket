# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one path rule for files a delegation writes (OMN-19600).

A delegated task may declare output files, and every such path is chosen, in
the end, by a model. This module is the syntactic half of keeping those writes
inside the declared target: a path must be relative, POSIX-separated, free of
parent references, empty or dot parts, git metadata, control characters and
drive letters, and bounded in depth and component length.

The syntactic rule is not sufficient on its own. A path that passes it can
still leave the target through a symlink already present on disk, which is why
the materialize effect walks the target one component at a time with
``O_NOFOLLOW`` rather than resolving and then writing. Both halves are needed.
"""

from __future__ import annotations

import re

#: Deepest directory nesting a declared output may use.
MAX_OUTPUT_PATH_DEPTH: int = 16
#: Longest single path component, the common filesystem NAME_MAX.
MAX_OUTPUT_COMPONENT_CHARS: int = 255
#: Longest whole path.
MAX_OUTPUT_PATH_CHARS: int = 1024

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


def check_relative_output_path(value: str) -> str:
    """Return ``value`` unchanged, or raise ``ValueError`` naming the rule it breaks."""
    if not value or value != value.strip():
        raise ValueError(f"output path is empty or padded: {value!r}")
    if len(value) > MAX_OUTPUT_PATH_CHARS:
        raise ValueError(f"output path is longer than {MAX_OUTPUT_PATH_CHARS} chars")
    if _CONTROL_CHARACTERS.search(value):
        raise ValueError(f"output path contains a control character: {value!r}")
    if "\\" in value:
        raise ValueError(f"output path must use '/' separators only: {value!r}")
    if value.startswith(("/", "~")) or _DRIVE_LETTER.match(value):
        raise ValueError(f"output path must be relative to the target: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(
            f"output path may not contain '.', '..' or empty parts: {value!r}"
        )
    if ".git" in parts:
        raise ValueError(f"output path may not reach into git metadata: {value!r}")
    if len(parts) > MAX_OUTPUT_PATH_DEPTH:
        raise ValueError(
            f"output path is deeper than {MAX_OUTPUT_PATH_DEPTH} components: {value!r}"
        )
    if any(len(part) > MAX_OUTPUT_COMPONENT_CHARS for part in parts):
        raise ValueError(
            f"output path has a component longer than {MAX_OUTPUT_COMPONENT_CHARS} chars"
        )
    return value


__all__ = [
    "MAX_OUTPUT_COMPONENT_CHARS",
    "MAX_OUTPUT_PATH_CHARS",
    "MAX_OUTPUT_PATH_DEPTH",
    "check_relative_output_path",
]
