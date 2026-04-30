import pytest

from omnimarket.cli.output.protocol import ProtocolCliOutputHandler
from omnimarket.cli.output.registry import (
    KNOWN_FORMATS,
    UnknownOutputFormatError,
    resolve_handler,
)
from omnimarket.models.cli_report import EnumMarketCliOutputFormat


def test_known_formats_match_enum() -> None:
    assert set(KNOWN_FORMATS) == set(EnumMarketCliOutputFormat)


def test_resolve_handler_returns_protocol_for_each_format() -> None:
    for fmt in EnumMarketCliOutputFormat:
        handler = resolve_handler(fmt)
        assert isinstance(handler, ProtocolCliOutputHandler)
        assert handler.format_name == fmt.value


def test_unknown_format_raises_with_allowed_values() -> None:
    class _Fake:
        value = "bogus"

    with pytest.raises(UnknownOutputFormatError) as exc:
        resolve_handler(_Fake())  # type: ignore[arg-type]
    assert "text" in str(exc.value)
    assert "json" in str(exc.value)
