-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT

-- OMN-25103: the newest applied floor integration pass, one row. The writer reads
-- the held pass from here, not from MAX(as_of) over floor_integration_rows, so an
-- empty pass (no rows) is still remembered and a late older pass changes nothing.
CREATE TABLE IF NOT EXISTS omninode_internal.floor_integration_pass (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    as_of TEXT NOT NULL,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
