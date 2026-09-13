"""Extracted verbatim from omnibase_infra scripts/check_dockerfile_pins.py."""


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints."""
    return tuple(int(x) for x in v.split(".")[:3])


def _satisfies_specifier(version_str: str, specifier: str) -> bool:
    """Return True if *version_str* satisfies *specifier*.

    Supports simple ``>=``, ``<=``, ``>``, ``<``, ``==``, ``!=`` clauses
    joined by commas.  Not a full PEP 440 implementation.
    """
    version = _parse_version(version_str)
    for clause in specifier.split(","):
        clause = clause.strip()
        for op in (">=", "<=", "!=", ">", "<", "=="):
            if clause.startswith(op):
                rhs = _parse_version(clause[len(op) :])
                if (
                    (op == ">=" and version < rhs)
                    or (op == "<=" and version > rhs)
                    or (op == ">" and version <= rhs)
                    or (op == "<" and version >= rhs)
                    or (op == "==" and version != rhs)
                    or (op == "!=" and version == rhs)
                ):
                    return False
                break
    return True
