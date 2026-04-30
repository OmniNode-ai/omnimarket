"""Static CLI output handler registry."""

from __future__ import annotations

from omnimarket.cli.output.protocol import ProtocolCliOutputHandler
from omnimarket.models.cli_report import EnumMarketCliOutputFormat


class UnknownOutputFormatError(ValueError):
    """Raised when an unsupported CLI output format is requested."""


KNOWN_FORMATS: tuple[EnumMarketCliOutputFormat, ...] = tuple(EnumMarketCliOutputFormat)


def resolve_handler(fmt: EnumMarketCliOutputFormat) -> ProtocolCliOutputHandler:
    """Return the output handler for a known CLI output format."""
    from omnimarket.cli.output.handlers.handler_json import HandlerCliOutputJson
    from omnimarket.cli.output.handlers.handler_markdown import HandlerCliOutputMarkdown
    from omnimarket.cli.output.handlers.handler_text import HandlerCliOutputText
    from omnimarket.cli.output.handlers.handler_yaml import HandlerCliOutputYaml

    table: dict[EnumMarketCliOutputFormat, ProtocolCliOutputHandler] = {
        EnumMarketCliOutputFormat.TEXT: HandlerCliOutputText(),
        EnumMarketCliOutputFormat.JSON: HandlerCliOutputJson(),
        EnumMarketCliOutputFormat.YAML: HandlerCliOutputYaml(),
        EnumMarketCliOutputFormat.MARKDOWN: HandlerCliOutputMarkdown(),
    }
    if fmt not in table:
        allowed = ", ".join(f.value for f in EnumMarketCliOutputFormat)
        raise UnknownOutputFormatError(
            f"unknown output format: {getattr(fmt, 'value', fmt)!r} (allowed: {allowed})"
        )
    return table[fmt]
