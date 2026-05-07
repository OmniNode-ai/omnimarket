# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket-validate-config — CLI that validates the current environment config.

Loads Settings from the environment, runs validate_required_services(), and
prints a status table showing each field, its source, and its value (masked
for secrets). Exits 0 if valid, exits 1 if any required fields are missing.

OMN-10581. Wave 1 / Task 9 of the Public-Shippable plan.
"""

from __future__ import annotations

import sys

import click
from pydantic import SecretStr

from omnimarket.config.settings import Settings


def _mask(value: str, is_secret: bool) -> str:
    if not value:
        return "(not set)"
    if is_secret:
        return "***"
    return value


def _field_source(settings: Settings, field_name: str) -> str:
    """Return 'env', 'default', or 'file' heuristic based on field value."""
    field_info = Settings.model_fields.get(field_name)
    if field_info is None:
        return "unknown"

    raw = getattr(settings, field_name)
    if isinstance(raw, SecretStr):
        raw_str = raw.get_secret_value()
    elif isinstance(raw, bool):
        raw_str = str(raw).lower()
    elif isinstance(raw, int):
        raw_str = str(raw)
    else:
        raw_str = str(raw)

    default = field_info.default
    if isinstance(default, SecretStr):
        default_str = default.get_secret_value()
    elif isinstance(default, bool):
        default_str = str(default).lower()
    elif isinstance(default, int):
        default_str = str(default)
    else:
        default_str = str(default) if default is not None else ""

    if raw_str == default_str:
        return "default"
    return "env"


def _get_display_value(settings: Settings, field_name: str) -> str:
    raw = getattr(settings, field_name)
    if isinstance(raw, SecretStr):
        return _mask(raw.get_secret_value(), is_secret=True)
    if isinstance(raw, bool):
        return str(raw).lower()
    if isinstance(raw, int):
        return str(raw) if raw != 0 else "(not set)"
    return _mask(str(raw), is_secret=False)


@click.command(name="omnimarket-validate-config")
@click.option(
    "--all-fields",
    is_flag=True,
    default=False,
    help="Show all fields, not just service-relevant ones.",
)
def main(all_fields: bool) -> None:
    """Validate omnimarket configuration from the current environment.

    Loads Settings, runs validate_required_services(), and prints a status
    table. Exits 0 if valid, exits 1 if required fields are missing.
    """
    settings = Settings()
    errors = settings.validate_required_services()

    service_flag_fields = {
        "enable_kafka",
        "enable_postgres",
        "enable_qdrant",
        "enable_valkey",
        "enable_memory_service",
    }

    required_when_enabled: dict[str, list[str]] = {
        "enable_kafka": ["kafka_bootstrap_servers", "kafka_broker"],
        "enable_postgres": [
            "postgres_host",
            "postgres_port",
            "postgres_database",
            "postgres_user",
            "postgres_password",
            "omnibase_infra_db_url",
            "omnidash_analytics_db_url",
        ],
        "enable_qdrant": ["qdrant_host", "qdrant_port"],
        "enable_valkey": ["valkey_host", "valkey_port"],
        "enable_memory_service": ["embedding_model_url"],
    }

    # Determine which fields to show
    if all_fields:
        show_fields = list(Settings.model_fields.keys())
    else:
        show_fields = list(service_flag_fields)
        for flag, fields in required_when_enabled.items():
            flag_value = getattr(settings, flag)
            if flag_value:
                show_fields.extend(fields)

    # Print header
    col_w = (30, 10, 30)
    header = f"{'Field':<{col_w[0]}}  {'Source':<{col_w[1]}}  {'Value':<{col_w[2]}}"
    separator = "-" * (sum(col_w) + 4)

    click.echo()
    click.echo("omnimarket configuration status")
    click.echo(separator)
    click.echo(header)
    click.echo(separator)

    for field_name in show_fields:
        source = _field_source(settings, field_name)
        display = _get_display_value(settings, field_name)
        click.echo(
            f"{field_name:<{col_w[0]}}  {source:<{col_w[1]}}  {display:<{col_w[2]}}"
        )

    click.echo(separator)

    if errors:
        click.echo()
        click.echo(f"INVALID — {len(errors)} error(s):")
        for i, err in enumerate(errors, 1):
            click.echo(f"  {i}. {err}")
        click.echo()
        sys.exit(1)
    else:
        click.echo()
        click.echo("OK — configuration is valid.")
        click.echo()
