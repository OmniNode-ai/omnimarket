# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes and builders for the dev proof tenant token tests: a test key pair, a key reader, a seeder, an overlay."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import SecretStr

from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenStatus,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.overlay import (
    ModelDevProofTenantTokenOverlay,
)

SIGNING_FOLDER = "/test/keys"
SIGNING_NAME = "TEST_LAB_SIGNING_KEY"
H051 = "cli-run-a"
H201 = "cli-run-b"
SECRET_H051 = "TEST_TOKEN_H051"
SECRET_H201 = "TEST_TOKEN_H201"
ID_H051 = "11111111-1111-4111-8111-111111111051"
ID_H201 = "22222222-2222-4222-8222-222222222201"


@cache
def rsa_pair() -> tuple[str, str]:
    """A throwaway RSA test key: the private PEM and its public half."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


@cache
def ec_pair() -> tuple[str, str]:
    """A throwaway P-256 test key: the private PEM and its public half."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


def overlay(
    *, lane: str = "dev", algorithm: str = "RS256", issuer: str | None = None
) -> ModelDevProofTenantTokenOverlay:
    return ModelDevProofTenantTokenOverlay.model_validate(
        {
            "lane": lane,
            "store": {
                "provider": "infisical",
                "address": "https://secrets.example.invalid",
                "project_id": "00000000-0000-4000-8000-000000000000",
                "environment": "dev",
            },
            "signing_key": {
                "secret_folder": SIGNING_FOLDER,
                "secret_key": SIGNING_NAME,
                "algorithm": algorithm,
            },
            "token_folder": "/test/tokens",
            "issuer": issuer,
            "tenants": [
                {"tenant_slug": H051, "tenant_id": ID_H051, "secret_name": SECRET_H051},
                {"tenant_slug": H201, "tenant_id": ID_H201, "secret_name": SECRET_H201},
            ],
        }
    )


class FakeKeyReader:
    """The signing key read by folder and name; every call is recorded."""

    def __init__(self, value: str | None) -> None:
        self._value = value
        self.calls: list[tuple[str, str]] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.calls.append((folder, name))
        if self._value is None or (folder, name) != (SIGNING_FOLDER, SIGNING_NAME):
            return None
        return SecretStr(self._value)


class FakeSeeder:
    """The seed node seam over an in-memory store; a name already held is never overwritten."""

    def __init__(
        self,
        held: Mapping[str, str] | None = None,
        *,
        fail_with: EnumDevProofTokenStatus | None = None,
    ) -> None:
        self.store: dict[str, str] = dict(held or {})
        self.exists_calls: list[str] = []
        self.seed_calls: list[str] = []
        self._fail_with = fail_with

    async def exists(self, name: str) -> bool:
        self.exists_calls.append(name)
        return name in self.store

    async def seed(self, name: str, value: SecretStr) -> EnumDevProofTokenStatus:
        self.seed_calls.append(name)
        if self._fail_with is not None:
            return self._fail_with
        if name in self.store:
            return EnumDevProofTokenStatus.PRESENT
        self.store[name] = value.get_secret_value()
        return EnumDevProofTokenStatus.WRITTEN


class FakeSeedStore:
    """The slice of the infra store the seed handler uses, over a dict; counts every write."""

    def __init__(self, held: Mapping[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(held or {})
        self.set_calls: list[str] = []

    async def set_secret(self, key: str, value: str) -> bool:
        self.set_calls.append(key)
        self.values[key] = value
        return True

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return sorted(self.values)

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


PROJECT_ID = UUID("00000000-0000-4000-8000-000000000000")
