"""Extracted verbatim from omnibase_core src/omnibase_core/utils/util_name_validation.py."""


def detect_add_remove_conflicts(
    add_values: list[str] | None,
    remove_values: list[str] | None,
    field_name: str,
    *,
    case_sensitive: bool = True,
) -> list[str]:
    """Detect conflicts between add and remove operations.

    Returns the sorted list of values appearing in both lists. Comparison is
    case-insensitive unless case_sensitive is True, and the returned values are
    the normalised (lower-cased) forms in that mode.
    """
    if add_values is None or remove_values is None:
        return []

    if case_sensitive:
        add_set = set(add_values)
        remove_set = set(remove_values)
    else:
        add_set = {v.lower() for v in add_values}
        remove_set = {v.lower() for v in remove_values}

    return sorted(add_set & remove_set)
