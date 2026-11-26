"""OMN-26620: /doctor rows as the end-to-end pass found them (2026-11-26).

R1  every row that is not healthy says "fix: /config", Docker, Node.js and
    Kafka included, which /config cannot fix.
R2  the rows' columns do not line up: each mark is a different width and a
    long check name pushes its sentence right.
R3  a model server that does not answer, the one thing the user can act on,
    shows no sentence at all (its message holds a URL, so it is hidden).
R4  verify pass 1, 2026-11-26, real onex at 80x24 and 140x40: "Env vars",
    "Node.js version" and "Delegation identity" said "! needs attention" and
    nothing else, so the user could not tell what was wrong.
R5  the plain sentences still show no setting name, path or fault code.
R6  at 80 columns a long row wrapped back to column 1, under the marks.
"""

from __future__ import annotations

import json

from omnimarket.nodes.node_onex_setup_effect.doctor_page import render_doctor_page

_REPORT = json.dumps(
    {
        "total": 6,
        "passed": 1,
        "failed": 3,
        "skipped": 2,
        "checks": [
            {
                "name": "Docker",
                "status": "healthy",
                "message": "Docker daemon is running",
            },
            {
                "name": "Kafka/Redpanda",
                "status": "unknown",
                "message": "Skipped: KAFKA_BOOTSTRAP_SERVERS not set",
            },
            {
                "name": "Repos synced to main",
                "status": "unknown",
                "message": "Skipped: OMNI_HOME not set",
            },
            {
                "name": "Node.js version",
                "status": "unhealthy",
                "message": "Node.js not found: [Errno 2] No such file or directory",
            },
            {
                "name": "Delegation API key",
                "status": "unhealthy",
                "message": "Missing: the key",
            },
            {
                "name": "Delegation local model",
                "status": "unhealthy",
                "message": "[local_model_not_serving] The local model server at "
                "http://192.0.2.10:8000 could not be reached",
            },
        ],
    }
)


def _rows() -> dict[str, str]:
    lines = render_doctor_page(text=_REPORT, width=100)
    names = [
        "Docker",
        "Kafka/Redpanda",
        "Repos synced to main",
        "Node.js version",
        "Delegation API key",
        "Delegation local model",
    ]
    return {name: next(line for line in lines[1:] if name in line) for name in names}


def test_only_what_config_fixes_points_to_config() -> None:
    """R1."""
    rows = _rows()
    for name in ("Docker", "Kafka/Redpanda", "Repos synced to main", "Node.js version"):
        assert "/config" not in rows[name], rows[name]
    assert "/config" in rows["Delegation API key"], rows["Delegation API key"]
    assert "/config" in rows["Delegation local model"], rows["Delegation local model"]


def test_names_and_sentences_line_up() -> None:
    """R2."""
    rows = _rows()
    starts = {name: row.index(name) for name, row in rows.items()}
    assert len(set(starts.values())) == 1, starts
    after = {
        name: len(row) - len(row[starts[name] + len(name) :].lstrip())
        for name, row in rows.items()
        if row[starts[name] + len(name) :].strip()
    }
    assert len(set(after.values())) == 1, (after, rows)


def test_a_server_that_does_not_answer_is_said_plainly() -> None:
    """R3."""
    row = _rows()["Delegation local model"]
    assert "does not answer" in row, row
    assert "192.0.2.10" not in row, row
    assert "local_model_not_serving" not in row, row


_ATTENTION = json.dumps(
    {
        "total": 5,
        "passed": 0,
        "failed": 5,
        "skipped": 0,
        "checks": [
            {
                "name": "env_vars",
                "status": "unhealthy",
                "message": "Missing: LINEAR_API_KEY",
            },
            {
                "name": "Node.js version",
                "status": "unhealthy",
                "message": "Node.js not found: [Errno 2] No such file or "
                "directory: 'node'",
            },
            {
                "name": "Delegation identity",
                "status": "unhealthy",
                "message": "[no_identity] No delegation tenant identity is "
                "configured on this machine. Fix: pbpaste | onex auth login "
                "--tenant-slug <slug> --base-url https://192.0.2.10",
            },
            {
                "name": "Delegation quota",
                "status": "unhealthy",
                "message": "[quota_exhausted] Quota for tenant t_1 is used up",
            },
            {
                "name": "Some new check",
                "status": "unhealthy",
                "message": "[a_code_no_page_knows] at /var/lib/x: 3",
            },
        ],
    }
)


def _attention_rows(width: int) -> list[str]:
    return render_doctor_page(text=_ATTENTION, width=width)[1:]


def _sentence_of(rows: list[str], name: str) -> str:
    """The row's text after its name, continuation lines included."""
    start = next(i for i, row in enumerate(rows) if name in row)
    first = rows[start]
    parts = [first[first.index(name) + len(name) :].strip()]
    for row in rows[start + 1 :]:
        if row[:1].strip():  # a row of its own starts at column 1
            break
        parts.append(row.strip())
    return " ".join(part for part in parts if part)


def test_every_row_that_needs_attention_says_why() -> None:
    """R4."""
    rows = _attention_rows(140)
    for name in (
        "Env vars",
        "Node.js version",
        "Delegation identity",
        "Delegation quota",
        "Some new check",
    ):
        assert _sentence_of(rows, name), (name, rows)
    assert "Linear" in _sentence_of(rows, "Env vars"), rows
    assert "not installed" in _sentence_of(rows, "Node.js version"), rows
    assert "signed in" in _sentence_of(rows, "Delegation identity"), rows


def test_the_sentences_show_no_setting_name_path_or_code() -> None:
    """R5."""
    screen = "\n".join(_attention_rows(140))
    for leak in (
        "LINEAR_API_KEY",
        "Errno",
        "no_identity",
        "pbpaste",
        "192.0.2.10",
        "quota_exhausted",
        "t_1",
        "a_code_no_page_knows",
        "/var/lib",
    ):
        assert leak not in screen, (leak, screen)


def test_a_long_row_wraps_under_its_own_sentence_at_80_columns() -> None:
    """R6: the TUI passes width 74 in an 80-column terminal."""
    rows = render_doctor_page(text=_REPORT, width=74)[1:]
    assert all(len(row) <= 74 for row in rows), rows
    first = next(i for i, row in enumerate(rows) if "Delegation local model" in row)
    sentence_at = rows[first].index("A model server")
    follow = rows[first + 1]
    assert follow[:sentence_at].strip() == "", (follow, rows)
    assert follow[sentence_at : sentence_at + 1].strip(), (follow, rows)
