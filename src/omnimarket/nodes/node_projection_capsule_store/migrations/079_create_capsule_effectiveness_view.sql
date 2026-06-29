-- OMN-12842 (M2): capsule effectiveness read view with staleness decay.
--
-- Applies staleness decay to the immutable raw scores. Raw scored values stay
-- immutable in capsule_store; effective_* are computed at read time as
-- raw * max(floor, 0.5 ** (age_days / half_life_days)). Decay parameters mirror
-- contract.yaml config.decay (half_life_days=30.0, floor=0.25); the contract is
-- the authority, this view keeps the dashboard read surface aligned and ranks
-- by effective_first_pass_rate DESC per (factor, validity_scope).
--
-- The base table capsule_store is created in 078; ordering this view in a later
-- migration satisfies the base-table-before-view gate
-- (scripts/ci/check_migration_base_before_view.py, OMN-12942).

CREATE OR REPLACE VIEW projection_capsule_effectiveness AS
WITH decay_params AS (
    SELECT 30.0::float AS half_life_days, 0.25::float AS floor
),
scored AS (
    SELECT
        c.capsule_id,
        c.capsule_hash,
        c.factor,
        c.validity_scope,
        c.source_commit,
        c.source_artifact,
        c.schema_version,
        c.success_rate::float AS success_rate,
        c.first_pass_rate::float AS first_pass_rate,
        c.cost_per_success::float AS cost_per_success,
        c.hit_count,
        c.last_scored,
        c.created_at,
        c.updated_at,
        GREATEST(
            d.floor,
            POWER(
                0.5,
                GREATEST(
                    0.0,
                    EXTRACT(EPOCH FROM (NOW() - c.last_scored)) / 86400.0
                ) / d.half_life_days
            )
        )::float AS decay_multiplier
    FROM capsule_store c
    CROSS JOIN decay_params d
)
SELECT
    capsule_id,
    capsule_hash,
    factor,
    validity_scope,
    source_commit,
    source_artifact,
    schema_version,
    success_rate,
    first_pass_rate,
    cost_per_success,
    (success_rate * decay_multiplier)::float AS effective_success_rate,
    (first_pass_rate * decay_multiplier)::float AS effective_first_pass_rate,
    hit_count,
    last_scored,
    created_at,
    updated_at
FROM scored
ORDER BY factor, validity_scope, effective_first_pass_rate DESC;
