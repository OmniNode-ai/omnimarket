# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Interactive model selection for the shared setup command."""

from collections.abc import Callable

import click
from pydantic import SecretStr

from omnimarket.handlers.routing import byok_provider_backends
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_models import (
    ModelEndpointModelsResult,
)
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_probe import (
    ModelEndpointProbeRequest,
    ModelEndpointProbeResult,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    KEYLESS_PROVIDERS,
    URL_PROVIDERS,
)


def _suggested_order(
    backend: byok_provider_backends.ModelByokProviderBackend | None,
    available: tuple[str, ...],
) -> tuple[str, ...]:
    """Rank only live IDs; unmatched vendor IDs stay selectable unchanged."""
    if backend is None:
        return available
    preferred = [name for name in backend.default_models if name in available]
    while choice := byok_provider_backends.select_byok_model(
        backend, available, exclude=preferred
    ):
        preferred.append(choice)
    return (*preferred, *(name for name in available if name not in preferred))


def ask_choices(
    provider: str | None,
    model: str | None,
    url: str | None,
    *,
    discover: Callable[..., ModelEndpointModelsResult],
    probe: Callable[..., ModelEndpointProbeResult],
    read_key: Callable[[str | None, bool], SecretStr | None],
    endpoint_auth: str | None = None,
    credentials: dict[str, SecretStr] | None = None,
    connection_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Prompt for the provider, model and URL a terminal run was not given."""
    providers = byok_provider_backends.load_byok_provider_catalog()
    if provider is None:
        choices = tuple(dict.fromkeys((*providers, *URL_PROVIDERS)))
        default = byok_provider_backends.first_key_backend().provider
        provider = str(
            click.prompt(
                "Provider",
                type=click.Choice(choices),
                default=default if default in choices else None,
            )
        )
    if provider in URL_PROVIDERS and url is None:
        url = str(click.prompt("Server base URL (http://<host>:<port>)", type=str))
    backend = providers.get(provider) if provider not in URL_PROVIDERS else None
    catalog_url = (
        url if provider in URL_PROVIDERS else backend.endpoint_url if backend else None
    )
    if model is None and catalog_url is not None:
        key = None
        if provider not in KEYLESS_PROVIDERS or endpoint_auth == "api-key":
            key = read_key(
                "openai_compatible" if provider == "ollama" else provider, True
            )
            if key is None:
                raise click.ClickException(
                    "This endpoint requires a key on hidden input or stdin."
                )
            if credentials is not None:
                credentials[
                    "connection:" + connection_id if connection_id else provider
                ] = key
        try:
            catalog = discover(
                url=catalog_url, key=key, **({"provider": provider} if backend else {})
            )
        except (ValueError, OSError):
            raise click.ClickException(
                "The endpoint could not be read. Check its URL and retry."
            ) from None
        if catalog.status == "unsupported":
            click.echo(catalog.message)
        elif catalog.status != "ready":
            raise click.ClickException(catalog.message or "Model discovery failed.")
        elif not catalog.models:
            raise click.ClickException(
                "The endpoint exposes no models. Prepare a model on your server, then retry."
            )
        else:
            models_to_probe = catalog.models
            probe_limit = int(
                ModelEndpointProbeRequest.model_json_schema()["properties"]["models"][
                    "maxItems"
                ]
            )
            if len(catalog.models) > probe_limit:
                ordered_catalog = _suggested_order(backend, catalog.models)
                click.echo(
                    f"The endpoint lists {len(catalog.models)} models. These IDs have not been probed. Choose at most {probe_limit} to check."
                )
                for name in ordered_catalog:
                    click.echo(name)
                models_to_probe = tuple(
                    item.strip()
                    for item in str(
                        click.prompt(
                            "Models to check (comma-separated IDs)",
                            type=str,
                            default=ordered_catalog[0],
                        )
                    ).split(",")
                )
                if (
                    not all(models_to_probe)
                    or len(models_to_probe) > probe_limit
                    or len(set(models_to_probe)) != len(models_to_probe)
                    or any(name not in catalog.models for name in models_to_probe)
                ):
                    raise click.ClickException(
                        f"Choose at most {probe_limit} unique IDs from this endpoint's catalog. Nothing was probed."
                    )
            if not click.confirm(
                "Check these models with one-token requests, accepting any provider API charges?",
                default=False,
            ):
                raise click.Abort()
            availability = probe(
                url=catalog_url,
                models=models_to_probe,
                key=key,
                **({"provider": provider} if backend else {}),
            )
            for item in availability.probes:
                if item.status == "unavailable":
                    click.echo(
                        f"Unavailable model {item.model}: {item.message}", err=True
                    )
            if not availability.models:
                raise click.ClickException(
                    "None of the listed models answered an availability check. Nothing was stored; review the endpoint and retry."
                )
            if availability.all_throttled:
                click.echo(
                    "All listed models are busy (HTTP 429); they remain selectable. Retry before expecting a successful delegation."
                )
            throttled = {
                item.model for item in availability.probes if item.status == "throttled"
            }
            unprobed = {
                item.model for item in availability.probes if item.status == "unprobed"
            }
            ordered = tuple(
                name
                for state in ("ready", "throttled", "unprobed")
                for name in _suggested_order(backend, availability.models)
                if (
                    "unprobed"
                    if name in unprobed
                    else "throttled"
                    if name in throttled
                    else "ready"
                )
                == state
            )
            if backend:
                recommendation = byok_provider_backends.load_byok_recommended_order()
                click.echo(
                    "Recommendation basis: "
                    + recommendation.orders[recommendation.onboarding].basis
                    + ". Suggestions use the live catalog; your chosen order controls delegation."
                )
            for name in ordered:
                suggested = backend is not None and (
                    name in backend.default_models
                    or byok_provider_backends.catalogue_prefers_model(backend, name)
                )
                click.echo(
                    name
                    + (" (suggested)" if suggested else "")
                    + (" (busy just now, HTTP 429)" if name in throttled else "")
                    + (
                        " (not probed; availability unknown)"
                        if name in unprobed
                        else ""
                    )
                )
            selected = tuple(
                item.strip()
                for item in str(
                    click.prompt(
                        "Models in preference order (comma-separated IDs)",
                        type=str,
                        default=",".join(
                            name
                            for name in ordered
                            if backend and name in backend.default_models
                        )
                        or ordered[0],
                    )
                ).split(",")
            )
            if (
                not all(selected)
                or len(set(selected)) != len(selected)
                or any(name not in availability.models for name in selected)
            ):
                raise click.ClickException(
                    "Choose unique model IDs from this endpoint's catalog."
                )
            model = ",".join(selected)
    if model is None:
        model = str(click.prompt("Model", type=str)).strip()
    return provider, model, url
