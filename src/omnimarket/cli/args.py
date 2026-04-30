"""Shared argparse helpers for OmniMarket CLI report output."""

from __future__ import annotations

import argparse

from omnimarket.models.cli_report import (
    EnumMarketCliOutputFormat,
    EnumMarketCliVerbosity,
    ModelMarketCliOutputConfig,
)


def add_output_args(parser: argparse.ArgumentParser) -> None:
    """Add shared output-related flags to a node CLI parser."""
    parser.add_argument(
        "--output",
        choices=[f.value for f in EnumMarketCliOutputFormat],
        default=EnumMarketCliOutputFormat.JSON.value,
    )
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="warning",
    )
    parser.add_argument("--evidence-dir", default=None)


def resolve_output_config(ns: argparse.Namespace) -> ModelMarketCliOutputConfig:
    """Resolve parsed CLI args into the typed output config."""
    verbosity = (
        EnumMarketCliVerbosity.VERBOSE
        if getattr(ns, "verbose", False)
        else EnumMarketCliVerbosity.STANDARD
    )
    return ModelMarketCliOutputConfig(
        format=EnumMarketCliOutputFormat(ns.output),
        verbosity=verbosity,
    )
