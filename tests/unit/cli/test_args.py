import argparse

import pytest

from omnimarket.cli.args import (
    add_output_args,
    report_output_requested,
    resolve_output_config,
)
from omnimarket.models.cli_report import (
    EnumMarketCliOutputFormat,
    EnumMarketCliVerbosity,
)


def test_default_output_is_json_for_back_compat() -> None:
    parser = argparse.ArgumentParser()
    add_output_args(parser)
    ns = parser.parse_args([])
    cfg = resolve_output_config(ns)
    assert cfg.format == EnumMarketCliOutputFormat.JSON
    assert cfg.verbosity == EnumMarketCliVerbosity.STANDARD


def test_explicit_format_resolves() -> None:
    parser = argparse.ArgumentParser()
    add_output_args(parser)
    ns = parser.parse_args(["--output", "text", "--verbose"])
    cfg = resolve_output_config(ns)
    assert cfg.format == EnumMarketCliOutputFormat.TEXT
    assert cfg.verbosity == EnumMarketCliVerbosity.VERBOSE


def test_unknown_format_rejected_by_argparse() -> None:
    parser = argparse.ArgumentParser()
    add_output_args(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--output", "xml"])


def test_report_output_requested_only_for_explicit_flag() -> None:
    assert report_output_requested(["--output", "json"])
    assert report_output_requested(["--output=text"])
    assert not report_output_requested([])
