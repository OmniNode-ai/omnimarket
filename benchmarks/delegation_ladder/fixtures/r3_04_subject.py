"""Extracted verbatim from omnibase_infra scripts/check_dockerfile_pins.py."""


def _iter_logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued physical lines into logical lines.

    Returns a list of ``(start_line_number, joined_line)`` tuples.
    """
    logical_lines: list[tuple[int, str]] = []
    pending = ""
    start_line = 1
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.rstrip()
        if pending == "":
            start_line = line_no
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        logical_lines.append((start_line, pending + stripped))
        pending = ""
    if pending:
        logical_lines.append((start_line, pending))
    return logical_lines
