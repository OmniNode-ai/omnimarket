-- =============================================================================
-- MIGRATION: automation-liveness projection (state, run history, alarm episodes)
-- =============================================================================
-- Owner:   omnimarket.nodes.node_projection_automation_liveness
-- Version: 1.0.0
--
-- WHY THREE RELATIONS
--   automation_liveness_state    one row per declared process and host, present
--                                from the moment the overlay declares it, so a
--                                process that has never emitted is a row, not an
--                                absence.
--   automation_run_history       append-only: one row per run phase event, keyed
--                                by the run's stable id so a re-read never
--                                double-counts.
--   automation_alarm_episodes    one row per alarm episode with its delivery and
--                                ledger-recording receipts.
--
-- WHY THE *_key COLUMNS
--   Each exposure walks its rows with a cursor that must be unique per row. The
--   natural keys are composite, so each relation carries one text key derived
--   from them (process@host, process@host#run#phase, the episode id).
--
-- WHY MOST EPISODE COLUMNS ARE NULLABLE
--   The raise, the delivery attempts and the ledger record are produced by
--   different components and travel on different topics, so a receipt can be
--   folded before its raise. The receipt creates a stub the raise completes.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   The application SQL ownership gate rejects unqualified relations.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.automation_liveness_state (
    process_key                  TEXT        NOT NULL,
    process_id                   TEXT        NOT NULL,
    host                         TEXT        NOT NULL,
    declared_at                  TIMESTAMPTZ,
    process_state                TEXT,
    contract_digest              TEXT,
    last_run_at                  TIMESTAMPTZ,
    last_outcome                 TEXT,
    last_work_at                 TIMESTAMPTZ,
    last_did_work_count          INTEGER,
    last_demand_count            INTEGER,
    failures_in_window           INTEGER     NOT NULL DEFAULT 0,
    consecutive_idle_with_demand INTEGER     NOT NULL DEFAULT 0,
    last_heartbeat_at            TIMESTAMPTZ,
    last_progress_at             TIMESTAMPTZ,
    progress_counter             BIGINT,
    open_run_started_at          TIMESTAMPTZ,
    verdict                      TEXT,
    verdict_reason               TEXT,
    verdict_state                TEXT,
    verdict_since                TIMESTAMPTZ,
    verdict_evaluated_at         TIMESTAMPTZ,
    open_episode_id              TEXT,
    projected_at                 TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (process_key)
);

-- SHAPE reconciliation: CREATE TABLE IF NOT EXISTS leaves a drifted relation as it was,
-- so every declared column is added when absent before an index reads it.
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS process_key TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS process_id TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS host TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS declared_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS process_state TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS contract_digest TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS last_run_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS last_outcome TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS last_work_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS last_did_work_count INTEGER;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS last_demand_count INTEGER;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS failures_in_window INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS consecutive_idle_with_demand INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS progress_counter BIGINT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS open_run_started_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS verdict_reason TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS verdict_state TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS verdict_since TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS verdict_evaluated_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS open_episode_id TEXT;
ALTER TABLE omninode_internal.automation_liveness_state
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_liveness_state_process_host
    ON omninode_internal.automation_liveness_state (process_id, host);
CREATE INDEX IF NOT EXISTS idx_automation_liveness_state_verdict
    ON omninode_internal.automation_liveness_state (verdict, verdict_since);

CREATE TABLE IF NOT EXISTS omninode_internal.automation_run_history (
    run_key         TEXT        NOT NULL,
    process_id      TEXT        NOT NULL,
    host            TEXT        NOT NULL,
    run_id          TEXT        NOT NULL,
    phase           TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    outcome         TEXT,
    exit_code       INTEGER,
    did_work_count  INTEGER,
    demand_count    INTEGER,
    unseen_runs     INTEGER     NOT NULL DEFAULT 0,
    work_unit       TEXT,
    evidence_ref    TEXT        NOT NULL,
    emitter         TEXT        NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    contract_digest TEXT        NOT NULL,
    projected_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_key)
);

-- SHAPE reconciliation: CREATE TABLE IF NOT EXISTS leaves a drifted relation as it was,
-- so every declared column is added when absent before an index reads it.
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS run_key TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS process_id TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS host TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS phase TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS exit_code INTEGER;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS did_work_count INTEGER;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS demand_count INTEGER;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS unseen_runs INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS work_unit TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS evidence_ref TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS emitter TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS contract_digest TEXT;
ALTER TABLE omninode_internal.automation_run_history
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_run_history_run
    ON omninode_internal.automation_run_history (process_id, host, run_id, phase);
CREATE INDEX IF NOT EXISTS idx_automation_run_history_process_time
    ON omninode_internal.automation_run_history (process_id, host, observed_at DESC);

CREATE TABLE IF NOT EXISTS omninode_internal.automation_alarm_episodes (
    episode_id             TEXT        NOT NULL,
    process_id             TEXT,
    host                   TEXT,
    verdict                TEXT,
    state                  TEXT,
    reason                 TEXT,
    severity               TEXT,
    opened_at              TIMESTAMPTZ,
    delivery_due_at        TIMESTAMPTZ,
    evidence_ref           TEXT,
    action                 TEXT,
    cleared_at             TIMESTAMPTZ,
    last_attempt_at        TIMESTAMPTZ,
    last_attempt_route     TEXT,
    last_attempt_delivered BOOLEAN,
    last_attempt_failure   TEXT,
    delivered_at           TIMESTAMPTZ,
    delivery_route         TEXT,
    delivery_ref           TEXT,
    recorded_at            TIMESTAMPTZ,
    ledger_line            TEXT,
    recorded_by            TEXT,
    last_recorded_at       TIMESTAMPTZ,
    last_ledger_line       TEXT,
    projected_at           TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (episode_id)
);

-- SHAPE reconciliation: CREATE TABLE IF NOT EXISTS leaves a drifted relation as it was,
-- so every declared column is added when absent before an index reads it.
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS episode_id TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS process_id TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS host TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS severity TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS delivery_due_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS evidence_ref TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS action TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS cleared_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS last_attempt_route TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS last_attempt_delivered BOOLEAN;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS last_attempt_failure TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS delivery_route TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS delivery_ref TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS ledger_line TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS recorded_by TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS last_recorded_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS last_ledger_line TEXT;
ALTER TABLE omninode_internal.automation_alarm_episodes
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_automation_alarm_episodes_process
    ON omninode_internal.automation_alarm_episodes (process_id, host, opened_at DESC);
