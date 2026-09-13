"""Extracted from omnibase_core src/omnibase_core/utils/util_name_validation.py."""

import keyword
import re

_DANGEROUS_IMPORT_CHARS: frozenset[str] = frozenset(
    ["<", ">", "|", "&", ";", "`", "$", "'", '"', "*", "?", "[", "]"]
)
_PYTHON_IDENTIFIER_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def is_valid_python_identifier(name: str) -> bool:
    if not name:
        return False
    return bool(_PYTHON_IDENTIFIER_PATTERN.match(name))


def validate_import_path_format(import_path: str) -> tuple[bool, str | None]:
    """Validate a Python import path format.

    Returns (is_valid, error_message); error_message is None when valid.
    """
    if not import_path or not import_path.strip():
        return False, "Import path cannot be empty"

    import_path = import_path.strip()

    found_dangerous = [c for c in _DANGEROUS_IMPORT_CHARS if c in import_path]
    if found_dangerous:
        return False, f"Import path contains invalid characters: {found_dangerous}"

    if ".." in import_path or "/" in import_path or "\\" in import_path:
        return False, "Import path cannot contain path separators or '..'"

    parts = import_path.split(".")
    if len(parts) < 2:
        return False, "Import path must include module and class (at least 2 segments)"

    if any(not part for part in parts):
        return False, "Import path contains empty segment"

    for part in parts:
        if not is_valid_python_identifier(part):
            return (
                False,
                f"Import path segment '{part}' is not a valid Python identifier",
            )

    for part in parts:
        if keyword.iskeyword(part):
            return False, f"Import path segment '{part}' is a Python reserved keyword"

    return True, None
