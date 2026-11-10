# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Redpanda bus visibility source: admin reads only, as the probed login.

It never produces, consumes, joins a group, commits or changes an ACL. The broker answers topic
metadata and the group listing with what the login holds DESCRIBE on, so that answer is the login's
exposure. ``ConfluentBrokerAdmin`` is the production admin; tests pass a stand-in for
``ProtocolBrokerAdmin`` instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    EnumBusReadFailure,
    ModelBusVisibilityRead,
)
from omnimarket.nodes.node_bus_login_exposure_effect.overlay import ModelRedpandaSource
from omnimarket.nodes.node_bus_login_exposure_effect.protocols.protocol_bus_visibility_source import (
    ProtocolBrokerAdmin,
)

AdminFactory = Callable[[Mapping[str, str]], ProtocolBrokerAdmin]

_DENIED_MARKERS = ("AUTHORIZATION_FAILED", "AUTHENTICATION_FAILED", "SASL")


class ConfluentBrokerAdmin:
    """Read-only admin calls through confluent-kafka's AdminClient."""

    def __init__(self, client_config: Mapping[str, str]) -> None:
        from confluent_kafka.admin import AdminClient

        self._admin = AdminClient(dict(client_config))

    def visible_topics(self, timeout: float) -> tuple[str, ...]:
        metadata = self._admin.list_topics(timeout=timeout)
        return tuple(sorted(metadata.topics))

    def visible_groups(self, timeout: float) -> tuple[str, ...]:
        listing = self._admin.list_consumer_groups(request_timeout=timeout).result(
            timeout=timeout
        )
        if listing.errors:
            raise RuntimeError("broker group listing incomplete")
        return tuple(sorted(group.group_id for group in listing.valid))


class RedpandaBusSource:
    def __init__(
        self,
        source: ModelRedpandaSource,
        environ: Mapping[str, str],
        admin_factory: AdminFactory | None = None,
    ) -> None:
        self._source = source
        self._environ = environ
        self._admin_factory = admin_factory or ConfluentBrokerAdmin

    def _failed(
        self, now: datetime, failure: EnumBusReadFailure
    ) -> ModelBusVisibilityRead:
        return ModelBusVisibilityRead(read_at=now, read_failure=failure)

    def read(self, *, now: datetime) -> ModelBusVisibilityRead:
        source = self._source
        username = self._environ.get(source.username_env)
        password = self._environ.get(source.password_env)
        if not username or not password:
            return self._failed(now, EnumBusReadFailure.CREDENTIALS_MISSING)
        config = {
            "bootstrap.servers": source.bootstrap_servers,
            "security.protocol": source.security_protocol,
            "sasl.mechanism": source.sasl_mechanism,
            "sasl.username": username,
            "sasl.password": password,
        }
        try:
            admin = self._admin_factory(config)
            topics = admin.visible_topics(source.timeout_seconds)
            groups = admin.visible_groups(source.timeout_seconds)
        except (
            Exception
        ) as error:  # boundary: an unread view is UNKNOWN, never an empty one
            text = f"{type(error).__name__} {error}".upper()
            if any(marker in text for marker in _DENIED_MARKERS):
                return self._failed(now, EnumBusReadFailure.DESCRIBE_DENIED)
            return self._failed(now, EnumBusReadFailure.UNREADABLE)
        return ModelBusVisibilityRead(topics=topics, groups=groups, read_at=now)
