"""The infra environment secret store, typed against the current spi protocol.

``ProtocolSecretStore.close`` takes ``timeout_seconds: float | None = None``;
the infra ``AdapterEnvSecretStore`` still declares ``float = 30.0``. The
subclass widens the parameter only: the infra close ignores it, and so does
this one, so behaviour is unchanged.
"""

from omnibase_infra.secret_stores import AdapterEnvSecretStore


class LiteralEnvSecretStore(AdapterEnvSecretStore):
    """``AdapterEnvSecretStore`` whose ``close`` matches ``ProtocolSecretStore``."""

    async def close(self, timeout_seconds: float | None = None) -> None:
        del timeout_seconds


__all__ = ["LiteralEnvSecretStore"]
