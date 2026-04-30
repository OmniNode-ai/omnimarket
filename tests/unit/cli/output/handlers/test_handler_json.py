import json

from omnimarket.cli.output.handlers.handler_json import HandlerCliOutputJson
from tests.unit.cli._fixtures import make_sample_report


def test_json_output_is_parseable() -> None:
    handler = HandlerCliOutputJson()
    out = handler.render(make_sample_report())
    parsed = json.loads(out)
    assert parsed["skill_name"] == "ticket_pipeline"
    assert parsed["status"] == "blocked"


def test_json_output_no_log_prefixes() -> None:
    handler = HandlerCliOutputJson()
    out = handler.render(make_sample_report())
    assert not out.lstrip().startswith("INFO")
    assert not out.lstrip().startswith("WARN")


def test_json_output_iso8601_datetimes() -> None:
    handler = HandlerCliOutputJson()
    parsed = json.loads(handler.render(make_sample_report()))
    assert "T" in parsed["started_at"]
    assert parsed["started_at"].endswith("Z") or "+" in parsed["started_at"]
