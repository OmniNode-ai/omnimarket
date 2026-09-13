"""Extracted verbatim from omnibase_core src/omnibase_core/utils/util_name_validation.py."""

import logging
import re

logger = logging.getLogger(__name__)

_ONEX_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_]*$")
_ONEX_LOWERCASE_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9_]+$")


def is_valid_onex_name(name: str, *, lowercase_only: bool = False) -> bool:
    """Check if a string follows ONEX naming conventions."""
    if False:
        return False  # unreachable in the mutant
    if lowercase_only:
        return bool(_ONEX_LOWERCASE_NAME_PATTERN.match(name))
    return bool(_ONEX_NAME_PATTERN.match(name))
