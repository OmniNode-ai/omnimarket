-- OMN-19514 (Jev eval plan Task 4): delegation_events.ticket_id
--
-- Every delegation_events row carries the Linear ticket id that its
-- delegate-skill terminal carried, stored in ticket_id TEXT. This allows a
-- delegation run to be joined to the Linear ticket it worked on and to the
-- DoD verdict rows (omninode_internal.dod_verify_runs) recorded for that
-- ticket.
--
-- The column stays NULL on a row whose terminal carried no ticket id and on
-- every row written before this migration.
--
-- The change is additive and nullable: older writers never name the column
-- and keep working unchanged.

BEGIN;

ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS ticket_id TEXT;

CREATE INDEX IF NOT EXISTS idx_delegation_events_ticket_id ON delegation_events (ticket_id) WHERE ticket_id IS NOT NULL;

COMMIT;
