"""Seam contract pin: tenant_inference_credentials.api_key_ref <-> OMN-15631's
delegation_routing_tenant_overlay.secret_ref (OMN-14208, OMN-16316 <-> OMN-15631).

Seam decision (recorded on both tickets, comment 62f52dac on OMN-15631 /
the DTO-pin comment 8d708740 on OMN-16316): the overlay's ``secret_ref``
column is populated by COPYING one ``tenant_inference_credentials.api_key_ref``
value at override-creation time -- never a live join. For that copy to be
valid, both sides must agree field-by-field: same wire type (opaque string),
same nullability posture where each is actually used, and the producer side
(this ticket) must never mint a ref shape the consumer side (OMN-15631) can't
store.

Known limitation, stated plainly rather than oversold: OMN-16316 and OMN-15631
are unmerged sibling worktrees on separate branches at the time this test was
written, so this cannot yet be a single test importing both nodes' real code
and driving one message through both handlers end-to-end (the strongest form
CLAUDE.md's "define and match seams" rule asks for). This test pins the
CONTRACT both sides must satisfy, sourced from a live read of OMN-15631's
landed migration (``omni_worktrees/OMN-15631/omnimarket/src/omnimarket/nodes/
node_delegation_routing_reducer/migrations/
0001_create_delegation_routing_tenant_overlay.sql``, read 2026-08-20):
``secret_ref TEXT`` (nullable, no length/format constraint, no FK). Once both
branches land on a shared base, promote this to a real cross-import test that
constructs a row via ``credential_publisher.mint_api_key_ref`` and asserts
``tenant_overlay_resolver`` accepts it as a ``secret_ref`` value without
transformation.
"""

from __future__ import annotations

from omnimarket.projection.credential_publisher import mint_api_key_ref

# OMN-15631's delegation_routing_tenant_overlay.secret_ref column contract,
# pinned from the live migration file (see module docstring). No length cap,
# no format/regex constraint, nullable, plain TEXT -- any non-empty str this
# side mints is a valid value for that column.
OVERLAY_SECRET_REF_MAX_LENGTH: int | None = None  # TEXT -- unbounded
OVERLAY_SECRET_REF_NULLABLE = True


class TestApiKeyRefIsAValidOverlaySecretRef:
    def test_minted_ref_is_a_plain_nonempty_string(self) -> None:
        ref = mint_api_key_ref(tenant_id="omninode", provider="openrouter")

        assert isinstance(ref, str)
        assert ref  # non-empty
        assert OVERLAY_SECRET_REF_MAX_LENGTH is None  # TEXT column -- no cap to violate

    def test_minted_ref_contains_no_whitespace_or_null_bytes(self) -> None:
        """A TEXT column accepts any string, but a ref that isn't a clean
        opaque token would be a bad citizen for both sides (logs, URLs,
        Kafka message keys). Pin the actually-produced shape, not just the
        column's permissiveness."""
        ref = mint_api_key_ref(tenant_id="omninode", provider="openrouter")

        assert " " not in ref
        assert "\x00" not in ref
        assert "\n" not in ref

    def test_minted_ref_is_deterministically_prefixed_for_debuggability(self) -> None:
        """Not a hard requirement of the overlay column, but a documented
        producer-side convention (credential_publisher.mint_api_key_ref) that
        OMN-15631's override-creation UI/CLI can rely on when validating a
        copied ref looks like a real credential ref, not a typo'd literal."""
        ref = mint_api_key_ref(tenant_id="t1", provider="openai")

        assert ref.startswith("cred_t1_openai_")

    def test_tenant_id_round_trips_through_the_ref_for_tenant_scoping_audit(
        self,
    ) -> None:
        """Both this table's tenant_id column and the overlay's tenant_id
        column must agree on which tenant a copied secret_ref belongs to --
        the ref itself carries the tenant_id it was minted for, so a copy
        into a mismatched tenant's overlay row is at least auditable even
        though this table does not enforce it at write time (OMN-16316 has
        no cross-tenant read/write check yet, matching OMN-15631's own v1(a)
        no-RLS posture)."""
        ref = mint_api_key_ref(tenant_id="acme-corp", provider="anthropic")

        assert "acme-corp" in ref
