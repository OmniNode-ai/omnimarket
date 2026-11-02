# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hosted credential intake's create request (OMN-16316, OMN-21217).

``omnimarket.projection.credential_publisher`` re-exports every name here, so
callers that import them from the publisher keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from omnimarket.handlers.routing.byok_provider_backends import (
    ANTHROPIC_WORKSPACE_ID_PATTERN,
    ByokCatalogError,
    byok_provider_plans,
    byok_routable_plans,
    customer_provider_catalogue,
    resolve_byok_provider_backend,
)

#: OMN-21012. A lab host table row name: one lowercase DNS label (h201, h200),
#: the same shape omnibase_core's delegation request ``source_host`` takes, so
#: the router can compare the two as plain strings.
LAB_HOST_NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"


class ProviderCatalogueUnavailableError(RuntimeError):
    """The customer provider catalogue could not be read, so intake cannot judge.

    Deliberately NOT a ``ValueError``. ``ByokCatalogError`` is one, and pydantic
    converts a ``ValueError`` raised inside a validator into a
    ``ValidationError`` -- which the onex-api route surfaces to the caller as a
    422 "your provider is invalid". A catalogue we cannot load is OUR fault, not
    the customer's, and reporting it as bad input both blames the wrong party
    and hides the real failure. Raised as a ``RuntimeError`` instead so it
    escapes validation untouched and reaches the route's generic handler, which
    maps it to a 503.

    Fail-CLOSED: intake refuses while the catalogue is unreadable rather than
    admitting a provider it cannot check.
    """


class ModelInferenceCredentialCreateRequest(BaseModel):
    """Typed body of ``POST /v1/tenants/me/inference-credentials``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=100,
        description="Customer-chosen logical label for this credential.",
    )
    provider: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Inference provider id (e.g. 'openrouter', 'openai'). Restricted "
            "to a safe opaque-token charset: provider is interpolated "
            "unencoded into api_key_ref (mint_api_key_ref), which in turn "
            "becomes the Infisical secret path segment and the Kafka message "
            "key -- whitespace, control bytes, or path separators here would "
            "corrupt or path-traverse those downstream identifiers."
        ),
    )
    key_value: SecretStr = Field(
        description=(
            "Raw customer API key. Held in-process only long enough to reach "
            "set_secret(); never logged, never re-serialized, never returned."
        ),
    )

    plan: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "OMN-20157. The provider product this key belongs to, for a provider "
            "that has more than one (glm: 'general_api'). Omit it and intake "
            "tries the key against the provider's plans to find out. A plan the "
            "provider's terms bar from third-party systems (glm: 'coding_plan') "
            "is refused with a typed code, never routed."
        ),
    )

    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "OMN-20844. The model this key runs, as the provider names it. It is "
            "checked against the provider's own model list for the key and "
            "recorded with the credential. Required for a provider where the "
            "customer chooses the model (openrouter): without it the key is "
            "refused with BYOK_MODEL_NOT_CHOSEN rather than given a model the "
            "customer did not choose."
        ),
    )

    models: list[str] | None = Field(default=None, min_length=1)
    expected_version: int | None = Field(default=None, strict=True, gt=0)

    @field_validator("models")
    @classmethod
    def _validate_models(cls, models: list[str] | None) -> list[str] | None:
        if models is None:
            return None
        if any(not model.strip() or len(model) > 200 for model in models):
            raise ValueError(
                "Each model must be a nonblank provider ID of at most 200 characters"
            )
        if len(set(models)) != len(models):
            raise ValueError("Models must be distinct and ordered")
        return models

    @model_validator(mode="after")
    def _normalize_model_alias(self) -> ModelInferenceCredentialCreateRequest:
        if self.model is not None and self.models is not None:
            raise ValueError("Choose model or models, not both")
        if self.model is not None:
            if not self.model.strip():
                raise ValueError("Model must not be blank")
            object.__setattr__(self, "models", [self.model])
        return self

    expires_at: datetime | None = Field(
        default=None,
        description=(
            "OMN-20991. When the provider stops honouring this key, as set where "
            "the key was created. Recorded on the credential so routing can skip "
            "a lapsed key and one alert can cover a pool before it lapses. "
            "Company keys all expire on January 1; any other date, or none, is "
            "stored with a warning, never refused."
        ),
    )

    @field_validator("expires_at", mode="after")
    @classmethod
    def _expires_at_must_be_aware_and_ahead(
        cls, expires_at: datetime | None
    ) -> datetime | None:
        """A naive time has no instant, and a past one names a dead key.

        Both are refused before anything is stored: a naive value would be read
        in whatever zone the reader happens to run in, and a key whose expiry
        already passed can only ever fail at the provider.
        """
        if expires_at is None:
            return None
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError(
                "expires_at must carry a timezone (for example 2027-01-01T00:00:00Z); "
                "nothing was stored."
            )
        if expires_at <= datetime.now(UTC):
            raise ValueError(
                f"expires_at {expires_at.isoformat()} has already passed: a key "
                "past its expiry cannot be used; nothing was stored."
            )
        return expires_at

    bound_host: str | None = Field(
        default=None,
        pattern=LAB_HOST_NAME_PATTERN,
        description=(
            "OMN-21012. The lab host whose Claude account owns this key, as its "
            "lab host table row name (for example h201). Operator ruling "
            "2026-10-11T17:58:45Z binds each account's key to the machine the "
            "account lives on, so the router spends it only on work sent from "
            "that host. None leaves the key usable from any host, as before."
        ),
    )

    workspace_id: str | None = Field(
        default=None,
        pattern=ANTHROPIC_WORKSPACE_ID_PATTERN,
        description=(
            "OMN-20967. The Anthropic workspace the key belongs to. A company "
            "Claude API key is answered only when each request names its "
            "workspace in the anthropic-workspace-id header, so the id is "
            "stored with the key and sent with every request made on it. Only "
            "an anthropic key has one. None sends no workspace header, as before."
        ),
    )

    @field_validator("provider", mode="after")
    @classmethod
    def _provider_must_be_on_the_customer_catalogue(cls, provider: str) -> str:
        """Refuse a provider that is not on the customer-facing catalogue.

        ``mode="after"`` so the field's own ``pattern`` runs FIRST: the charset
        constraint is what keeps ``provider`` safe to interpolate into
        ``api_key_ref`` (an Infisical path segment and a Kafka message key), and
        a path-traversing value must be refused on those grounds before it is
        ever compared against the catalogue.

        ``customer_provider_catalogue()`` (OMN-17353) is the single authority
        for this set, and its own docstring already declared that intake
        surfaces validate against it -- this is that contract, wired. Placing it
        on the MODEL rather than in the onex-api route handler is deliberate:
        the model is what onex-api imports, so the route, this package's tests
        and any future client inherit one refusal instead of re-implementing it.

        Membership is EXACT, not case-folded. Catalogue ids are normalised to
        lower case on load, while the field's charset pattern admits upper case,
        so folding here would let ``OpenRouter`` and ``openrouter`` both pass and
        mint two different ``api_key_ref`` values -- and therefore two different
        secret paths -- for one provider.
        """
        try:
            offered = customer_provider_catalogue()
        except ByokCatalogError as exc:
            raise ProviderCatalogueUnavailableError(
                "BYOK intake cannot validate the submitted provider: the "
                "customer provider catalogue could not be loaded. Refusing the "
                "registration rather than storing a key for a provider that "
                f"was never checked. Underlying error type: {type(exc).__name__}."
            ) from exc

        if provider not in offered:
            raise ValueError(
                f"{provider!r} is not a provider a key may be registered for. "
                f"Offered: {', '.join(offered)}. A provider the platform holds "
                "a house key for but does not offer to customers is declared "
                "not-offered in the catalogue with the ticket that owns lifting "
                "it; registering a key here for anything else would store a "
                "credential that can never be routed."
            )
        return provider

    @model_validator(mode="after")
    def _workspace_id_is_an_anthropic_fact(
        self,
    ) -> ModelInferenceCredentialCreateRequest:
        """Only a provider whose catalogue row declares workspaces takes an id."""
        if self.workspace_id is None:
            return self
        backend = resolve_byok_provider_backend(self.provider)
        if backend is None or backend.workspace_header is None:
            raise ValueError(
                f"{self.provider!r} has no workspaces, so a workspace id cannot "
                "be stored with its key; nothing was stored."
            )
        return self

    @model_validator(mode="after")
    def _plan_must_be_one_the_provider_declares(
        self,
    ) -> ModelInferenceCredentialCreateRequest:
        """A named plan must be one the catalogue declares for that provider.

        Runs after the provider field's own catalogue check, so ``provider`` is
        already known offered here. An unknown plan is refused rather than
        widened to the default: a key filed under the wrong product routes to an
        endpoint that refuses it.

        A DECLARED plan passes here even when it is detection-only (z.ai's Coding
        Plan), so that ``register_inference_credential`` refuses it with the
        plan's typed code (``BYOK_CODING_PLAN_NOT_PERMITTED``) and not as a
        generic validation failure. The plans this message offers back are the
        routable ones.
        """
        if self.plan is None:
            return self
        if self.plan not in byok_provider_plans(self.provider):
            raise ValueError(
                f"{self.plan!r} is not a plan of provider {self.provider!r}. "
                f"Plans: {', '.join(byok_routable_plans(self.provider))}."
            )
        return self
