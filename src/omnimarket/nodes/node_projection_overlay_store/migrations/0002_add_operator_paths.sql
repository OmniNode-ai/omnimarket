-- =============================================================================
-- MIGRATION: which settings of a stored overlay an operator set through the store
-- =============================================================================
-- Ticket: OMN-26495. The overlay home is a bootstrap: a setting an operator set through the store keeps
-- its live value when the loader runs again, and every other setting takes the overlay home's value. This
-- column lists the setting paths of the row's document that an operator set, as a JSON array; a row loaded
-- before this column reads as '[]', so every setting of it is the overlay home's.
-- =============================================================================

ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS operator_paths JSON NOT NULL DEFAULT '[]';

SELECT 1 / count(*) AS overlay_store_operator_paths_assertion
FROM information_schema.columns
WHERE table_schema = 'omninode_internal'
  AND table_name = 'overlay_store'
  AND column_name = 'operator_paths'
