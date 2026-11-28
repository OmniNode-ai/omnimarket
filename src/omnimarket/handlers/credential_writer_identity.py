# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Applies the BYOK intake's writer identity to its Infisical bootstrap values (OMN-25856).

The intake WRITES a customer key with its own machine identity. The runtime
process identity (``INFISICAL_CLIENT_ID`` / ``INFISICAL_CLIENT_SECRET``) is
read-only on the tenant-credential folder where it serves reads, so a create
with it answers 403. Both writer variables set means the intake uses them;
neither set keeps the process identity; exactly one set is a configuration
error. The module lives here, not in ``projection.credential_publisher``,
because that file is baselined and only shrinks (OMN-20304).
"""

from __future__ import annotations

import os

WRITER_CLIENT_ID_VAR = "INFISICAL_TENANT_CREDENTIAL_WRITER_CLIENT_ID"
WRITER_CLIENT_SECRET_VAR = "INFISICAL_TENANT_CREDENTIAL_WRITER_CLIENT_SECRET"


def apply_writer_identity(values: dict[str, str]) -> dict[str, str]:
    """Swap the writer identity into the bootstrap values when it is configured."""
    # Imported at call time: the publisher imports this module at call time too,
    # and the error type lives with the rest of the publisher's error family.
    from omnimarket.projection.credential_publisher import (
        CredentialStoreConfigurationError,
    )

    writer_id = os.environ.get(WRITER_CLIENT_ID_VAR, "").strip()
    writer_secret = os.environ.get(WRITER_CLIENT_SECRET_VAR, "").strip()
    if bool(writer_id) != bool(writer_secret):
        raise CredentialStoreConfigurationError(
            "BYOK credential intake has half a writer identity: set both "
            f"{WRITER_CLIENT_ID_VAR} and {WRITER_CLIENT_SECRET_VAR}, or neither."
        )
    if writer_id:
        values["INFISICAL_CLIENT_ID"] = writer_id
        values["INFISICAL_CLIENT_SECRET"] = writer_secret
    return values
