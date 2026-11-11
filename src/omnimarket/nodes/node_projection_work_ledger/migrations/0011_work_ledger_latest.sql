-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- =============================================================================
-- MIGRATION: omninode_internal.work_ledger_latest (OMN-23606)
-- =============================================================================
-- One row per (subject_kind, subject, row_type): the newest row of a tracked
-- row type for a lane or a ticket, with the cells a reader decides on. State,
-- not history: a board or a lane placement reads one small page of this table
-- instead of paging a window of raw rows through the bus.
--
-- The writer maintains it from each ledger row event, each write guarded on
-- (at, row_id) so a redelivered or reordered row cannot move an entry
-- backwards. The backfill below folds the rows already in work_ledger_rows
-- with the same rule. Its row type list is the contract's
-- work_ledger.latest_row_types (a test pins the two equal).
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.work_ledger_latest (
    subject_kind  TEXT        NOT NULL,
    subject       TEXT        NOT NULL,
    row_type      TEXT        NOT NULL,
    row_id        TEXT        NOT NULL,
    at            TIMESTAMPTZ NOT NULL,
    lane          TEXT,
    ticket        TEXT,
    pr            TEXT,
    outcome       TEXT,
    to_cell       TEXT,
    projected_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_kind, subject, row_type),
    CONSTRAINT work_ledger_latest_subject_kind CHECK (subject_kind IN ('lane', 'ticket'))
);

CREATE INDEX IF NOT EXISTS work_ledger_latest_at_idx
    ON omninode_internal.work_ledger_latest (at);

INSERT INTO omninode_internal.work_ledger_latest
    (subject_kind, subject, row_type, row_id, at, lane, ticket, pr, outcome, to_cell, projected_at)
SELECT DISTINCT ON (s.subject_kind, s.subject, r.row_type)
       s.subject_kind, s.subject, r.row_type, r.row_id, r.row_ts, r.row_lane,
       r.tickets ->> 0,
       NULLIF(btrim(substring(split_part(r.raw_row, E'\n', 1) FROM '(?:^|\|)\s*pr=([^|]*)')), ''),
       NULLIF(btrim(substring(split_part(r.raw_row, E'\n', 1) FROM '(?:^|\|)\s*outcome=([^|]*)')), ''),
       NULLIF(btrim(substring(split_part(r.raw_row, E'\n', 1) FROM '(?:^|\|)\s*to=([^|]*)')), ''),
       clock_timestamp()
FROM omninode_internal.work_ledger_rows r
CROSS JOIN LATERAL (
    SELECT 'lane'::text AS subject_kind, r.row_lane AS subject
    WHERE r.row_lane IS NOT NULL
    UNION ALL
    SELECT 'ticket'::text, t.value
    FROM jsonb_array_elements_text(COALESCE(r.tickets, '[]'::jsonb)) AS t(value)
) s
WHERE r.row_type IN ('CLAIM', 'TERMINAL', 'STATUS', 'MSG', 'HOLD')
  AND r.row_ts IS NOT NULL
ORDER BY s.subject_kind, s.subject, r.row_type, r.row_ts DESC, r.row_id DESC
ON CONFLICT (subject_kind, subject, row_type) DO UPDATE SET
    row_id = EXCLUDED.row_id, at = EXCLUDED.at, lane = EXCLUDED.lane,
    ticket = EXCLUDED.ticket, pr = EXCLUDED.pr, outcome = EXCLUDED.outcome,
    to_cell = EXCLUDED.to_cell, projected_at = EXCLUDED.projected_at
WHERE (omninode_internal.work_ledger_latest.at, omninode_internal.work_ledger_latest.row_id)
      <= (EXCLUDED.at, EXCLUDED.row_id);
