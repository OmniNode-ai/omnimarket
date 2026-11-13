# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real confluent_kafka AdminClient implementations of the reap effect's protocols (OMN-23992).

The request timeout is a constructor argument fed from the execution overlay's
``request_timeout_seconds``, not a module constant. The three functions are copied from the retiring
guard effect's reaper; they are not imported from it.
"""

from __future__ import annotations

from collections.abc import Mapping


class KafkaGroupAdmin:
    """The broker's group listing, deletion and health, over a confluent_kafka AdminClient."""

    def __init__(self, request_timeout_seconds: float) -> None:
        self._timeout = request_timeout_seconds

    def list_groups(self, client_config: Mapping[str, str]) -> dict[str, str]:
        """Admin-only listing of group name to upper-case state."""
        from confluent_kafka.admin import AdminClient

        admin = AdminClient(dict(client_config))
        listing = admin.list_consumer_groups(request_timeout=self._timeout).result(
            timeout=self._timeout
        )
        if listing.errors:
            raise RuntimeError("broker group listing incomplete")
        return {
            g.group_id: str(getattr(g.state, "name", g.state))
            .rsplit(".", 1)[-1]
            .upper()
            for g in listing.valid
        }

    def delete_groups(
        self, client_config: Mapping[str, str], names: list[str]
    ) -> dict[str, str]:
        """Delete the groups; the value is "" on success or the error name."""
        from confluent_kafka.admin import AdminClient

        admin = AdminClient(dict(client_config))
        futures = admin.delete_consumer_groups(names, request_timeout=self._timeout)
        outcome: dict[str, str] = {}
        for name, future in futures.items():
            try:
                future.result(timeout=self._timeout)
                outcome[name] = ""
            except Exception as exc:
                error = getattr(exc, "args", [None])[0]
                code = getattr(error, "name", None)
                outcome[name] = (callable(code) and str(code())) or type(exc).__name__
        return outcome

    def check_health(self, client_config: Mapping[str, str]) -> str:
        """ "" when the broker answers a cluster and a group listing, else the reason."""
        from confluent_kafka.admin import AdminClient

        try:
            admin = AdminClient(dict(client_config))
            cluster = admin.describe_cluster(request_timeout=self._timeout).result(
                timeout=self._timeout
            )
            if not cluster.nodes:
                return "no brokers in the cluster description"
            listing = admin.list_consumer_groups(request_timeout=self._timeout).result(
                timeout=self._timeout
            )
            if listing.errors:
                return "group listing incomplete"
        except Exception as exc:
            return f"broker unreadable: {type(exc).__name__}"
        return ""


__all__: list[str] = ["KafkaGroupAdmin"]
