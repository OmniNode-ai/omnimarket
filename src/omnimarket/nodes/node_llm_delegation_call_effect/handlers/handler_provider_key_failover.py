# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Move a house provider call to the next declared key when one key is refused (OMN-19432).

Operator ruling 2026-11-23T16:14:40Z: "we have a bunch of different keys. If one
fails, it should be moved automatically to the next key."

A house backend declares one ``secret_ref`` in the bifrost delegation config
(deep-merged with the settings-store overlay). The keys declared for one
provider are the backends of that provider on the same endpoint; no second
store is read. When a call ends with an auth class (401, 403,
``provider_auth_failed``, ``provider_billing``, a missing credential), the key
is held out of use for a cooldown and the same call is made once with the next
declared key. When every key fails the last typed failure stands, and the
caller's chain moves on to the next model.

Customer boundary (check C29, ``is_tenant_credential_ref``): a call whose own
reference is tenant-shaped, or is not one of the declared house references, is
made once on that reference and never on a house key. Candidates are house
references only, so a customer key is never listed as one.

Only key NAMES are carried or logged; no value is read here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final, Protocol, TypeVar

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_provider_key_attempt import (
    ModelProviderKeyAttempt,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

logger = logging.getLogger(__name__)

DEFAULT_KEY_COOLDOWN_SECONDS: Final[float] = 300.0

#: The classes that mean "this key is refused", so the next key may answer.
AUTH_FAILURE_CLASSES: Final[frozenset[EnumDelegationFailureClass]] = frozenset(
    {
        EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
        EnumDelegationFailureClass.PROVIDER_BILLING,
        EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING,
    }
)

T = TypeVar("T")


class ProtocolHouseKeyPool(Protocol):
    """The house key references declared for one provider endpoint, in order."""

    def refs_for(self, *, provider: str, endpoint_url: str) -> tuple[str, ...]:
        """Return the declared house references for this provider endpoint."""
        ...


def _declared_ref(backend: Mapping[str, Any]) -> str | None:
    for field in ("secret_ref", "api_key_ref", "api_key_env"):
        value = backend.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class DeclaredBackendKeyPool:
    """House keys as the bifrost config declares them: one ``secret_ref`` per backend.

    Backends of the same endpoint URL are the keys of one provider. A backend
    with no ``secret_ref``, with a tenant-shaped one, or a duplicate of an
    earlier one is not a candidate.
    """

    def __init__(
        self, backends: Callable[[], Iterable[Mapping[str, Any]]] | None = None
    ) -> None:
        self._backends = backends

    def _load(self) -> Iterable[Mapping[str, Any]]:
        if self._backends is not None:
            return self._backends()
        from omnimarket.handlers.routing.delegation_backend_resolution import (
            load_bifrost_backends,
        )

        return load_bifrost_backends()

    def refs_for(self, *, provider: str, endpoint_url: str) -> tuple[str, ...]:
        wanted = endpoint_url.strip()
        refs: list[str] = []
        for backend in self._load():
            if str(backend.get("endpoint_url") or "").strip() != wanted:
                continue
            declared = backend.get("provider")
            if declared and provider not in ("", "unknown") and declared != provider:
                continue
            ref = _declared_ref(backend)
            if ref and not is_tenant_credential_ref(ref) and ref not in refs:
                refs.append(ref)
        return tuple(refs)


class ProviderKeyFailover:
    """Hold refused house keys out of use for a cooldown and walk the declared keys.

    ``clock`` is injected so a test moves time; the default is monotonic.
    """

    def __init__(
        self,
        *,
        pool: ProtocolHouseKeyPool | None = None,
        cooldown_seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool if pool is not None else DeclaredBackendKeyPool()
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._bad_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_cooling(self, key_name: str) -> bool:
        with self._lock:
            until = self._bad_until.get(key_name)
            if until is None:
                return False
            if self._clock() >= until:
                del self._bad_until[key_name]
                return False
            return True

    def mark_bad(self, key_name: str) -> None:
        with self._lock:
            self._bad_until[key_name] = self._clock() + self._cooldown

    def candidates(
        self, *, own_ref: str | None, provider: str, endpoint_url: str
    ) -> tuple[str, ...]:
        """The own reference first, then the other declared house keys.

        Empty-handed means single call on ``own_ref``: no reference, a tenant
        reference, or a reference no house backend declares never fails over.
        """
        if not own_ref or is_tenant_credential_ref(own_ref):
            return ()
        declared = self._pool.refs_for(provider=provider, endpoint_url=endpoint_url)
        if own_ref not in declared:
            return ()
        return (own_ref, *(ref for ref in declared if ref != own_ref))

    def run(
        self,
        *,
        own_ref: str | None,
        provider: str,
        endpoint_url: str,
        attempt: Callable[[str | None], T],
        failure_of: Callable[[T], tuple[EnumDelegationFailureClass | None, int | None]],
        all_cooling: Callable[[str], T],
    ) -> tuple[T, tuple[ModelProviderKeyAttempt, ...]]:
        """Call once per candidate key until one is not refused.

        ``attempt(ref)`` makes the provider call on that reference.
        ``failure_of(result)`` returns the result's failure class and HTTP
        status. ``all_cooling(own_ref)`` builds the typed failure when every
        key is held out, so a cooling key is not retried on every call.
        """
        keys = self.candidates(
            own_ref=own_ref, provider=provider, endpoint_url=endpoint_url
        )
        if not keys:
            return attempt(own_ref), ()
        record: list[ModelProviderKeyAttempt] = []
        result: T | None = None
        for key in keys:
            if self.is_cooling(key):
                record.append(
                    ModelProviderKeyAttempt(key_name=key, skipped_cooling=True)
                )
                continue
            result = attempt(key)
            failure_class, status = failure_of(result)
            record.append(
                ModelProviderKeyAttempt(
                    key_name=key, failure_class=failure_class, http_status=status
                )
            )
            if failure_class not in AUTH_FAILURE_CLASSES:
                return result, tuple(record)
            self.mark_bad(key)
            logger.warning(
                "provider key refused, held out for %.0fs: key=%s class=%s status=%s",
                self._cooldown,
                key,
                failure_class.value if failure_class else None,
                status,
            )
        if result is None:
            return all_cooling(keys[0]), tuple(record)
        return result, tuple(record)


#: The process-wide instance the call effect uses; cooldown state lives here.
DEFAULT_PROVIDER_KEY_FAILOVER: Final[ProviderKeyFailover] = ProviderKeyFailover()
