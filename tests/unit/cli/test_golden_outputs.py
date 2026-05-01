import json
from pathlib import Path

import yaml

from omnimarket.cli.output.registry import resolve_handler
from omnimarket.models.cli_report import EnumMarketCliOutputFormat
from tests.unit.cli._fixtures import make_sample_report

GOLDEN = Path(__file__).parent / "golden"


def test_json_matches_golden() -> None:
    out = resolve_handler(EnumMarketCliOutputFormat.JSON).render(make_sample_report())
    expected = (GOLDEN / "ticket_pipeline.json").read_text(encoding="utf-8")
    assert json.loads(out) == json.loads(expected)


def test_markdown_matches_golden() -> None:
    out = resolve_handler(EnumMarketCliOutputFormat.MARKDOWN).render(
        make_sample_report()
    )
    expected = (GOLDEN / "ticket_pipeline.md").read_text(encoding="utf-8")
    assert out.strip() == expected.strip()


def test_yaml_matches_golden() -> None:
    out = resolve_handler(EnumMarketCliOutputFormat.YAML).render(make_sample_report())
    parsed = yaml.safe_load(out)
    expected = yaml.safe_load(
        (GOLDEN / "ticket_pipeline.yaml").read_text(encoding="utf-8")
    )
    assert parsed == expected


def test_text_matches_golden() -> None:
    out = resolve_handler(EnumMarketCliOutputFormat.TEXT).render(make_sample_report())
    expected = (GOLDEN / "ticket_pipeline.txt").read_text(encoding="utf-8")
    assert out.strip() == expected.strip()
