from omnimarket.cli.output.handlers.handler_text import HandlerCliOutputText
from omnimarket.models.cli_report import EnumMarketCliVerbosity
from tests.unit.cli._fixtures import make_sample_report


def test_text_contains_required_sections() -> None:
    out = HandlerCliOutputText().render(make_sample_report())
    for marker in (
        "OMNIMARKET SKILL: ticket_pipeline",
        "Node: node_ticket_pipeline",
        "Contract: ticket_pipeline",
        "Run ID:",
        "Mode:",
        "Input",
        "Execution",
        "Evidence",
        "Result",
        "terminal_event:",
    ):
        assert marker in out, f"missing marker: {marker}"


def test_text_no_ansi_escapes_by_default() -> None:
    out = HandlerCliOutputText().render(make_sample_report())
    assert "\x1b[" not in out


def test_text_verbose_includes_timing() -> None:
    report = make_sample_report(verbosity=EnumMarketCliVerbosity.VERBOSE)
    out = HandlerCliOutputText().render(report)
    assert "duration_ms" in out or "duration:" in out
