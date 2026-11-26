"""OMN-26620: /doctor rows as the end-to-end pass found them (2026-11-26).

R1  every row that is not healthy says "fix: /config", Docker, Node.js and
    Kafka included, which /config cannot fix.
R2  the rows' columns do not line up: each mark is a different width and a
    long check name pushes its sentence right.
R3  a model server that does not answer, the one thing the user can act on,
    shows no sentence at all (its message holds a URL, so it is hidden).
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
