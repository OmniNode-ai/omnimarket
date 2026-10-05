-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
--
-- OMN-19968: llm_call_metrics.usage_source speaks the shared vocabulary.
--
-- 0001 created usage_source_type as ('API', 'ESTIMATED', 'MISSING'). omnibase_infra
-- migration 077 (OMN-10382) moved the shared type to ('measured', 'estimated',
-- 'unknown'), the EnumUsageSource values, and the writer now produces those. A
-- database built from omnimarket's migrations alone still carries the old labels;
-- this renames them in place. RENAME VALUE keeps every stored row and needs no
-- table rewrite.
--
-- The type is resolved through search_path (to_regtype), never by name across
-- every schema: 0001's typname guard matched a same-named type in any schema,
-- which is how a database migrated by infra ended up rejecting this writer.
--
-- Idempotent: each label is renamed only while it is still present, so on a
-- database that already carries the shared vocabulary (every infra-migrated one)
-- the block changes nothing. Forward-only: applied migration bytes are immutable.

DO $$
DECLARE
    usage_type regtype := to_regtype('usage_source_type');
BEGIN
    IF usage_type IS NULL THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_enum WHERE enumtypid = usage_type AND enumlabel = 'API') THEN
        ALTER TYPE usage_source_type RENAME VALUE 'API' TO 'measured';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_enum WHERE enumtypid = usage_type AND enumlabel = 'ESTIMATED') THEN
        ALTER TYPE usage_source_type RENAME VALUE 'ESTIMATED' TO 'estimated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_enum WHERE enumtypid = usage_type AND enumlabel = 'MISSING') THEN
        ALTER TYPE usage_source_type RENAME VALUE 'MISSING' TO 'unknown';
    END IF;
END$$;

ALTER TABLE llm_call_metrics ALTER COLUMN usage_source SET DEFAULT 'unknown';
