# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One place that parses YAML text with libyaml's loader when PyYAML has it.

libyaml's loader reads the same documents about ten times faster than the pure-Python
one. Gates that parse every node contract (over a thousand) spent nearly all of their
time in that parse (OMN-26270). The fallback keeps a PyYAML built without libyaml
working. Callers that sit in a baselined file import this instead of re-spelling the
loader choice, so the baselined file does not grow.
"""

from __future__ import annotations

from typing import Any

import yaml

YAML_LOADER: type[yaml.SafeLoader] | type[yaml.CSafeLoader] = getattr(
    yaml, "CSafeLoader", yaml.SafeLoader
)


def load_yaml_text(text: str) -> Any:
    """Parse one YAML document with the safe loader, libyaml-backed when present."""
    return yaml.load(text, Loader=YAML_LOADER)
