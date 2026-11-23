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

-- CREATE TABLE IF NOT EXISTS is a no-op against a pre-existing table, so each
-- declared column is reconciled on its own.
ALTER TABLE omninode_internal.floor_integration_pass
    ADD COLUMN IF NOT EXISTS id SMALLINT DEFAULT 1;
ALTER TABLE omninode_internal.floor_integration_pass
    ADD COLUMN IF NOT EXISTS as_of TEXT;
ALTER TABLE omninode_internal.floor_integration_pass
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();
