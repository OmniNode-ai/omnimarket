from pathlib import Path

from omnimarket.cli.output.handlers.handler_markdown import HandlerCliOutputMarkdown
from tests.unit.cli._fixtures import make_sample_report


def test_markdown_contains_heading_and_tables() -> None:
    out = HandlerCliOutputMarkdown().render(make_sample_report())
    assert "# " in out
    assert "## Execution" in out
    assert "## Evidence" in out
    assert "## Result" in out
    assert "| " in out


def test_markdown_no_github_or_linear_calls() -> None:
    import omnimarket.cli.output.handlers.handler_markdown as m

    src = Path(m.__file__).read_text()
    forbidden = ("import requests", "from gh ", "from linear ", "github.")
    for f in forbidden:
        assert f not in src, f"unexpected dependency: {f}"
