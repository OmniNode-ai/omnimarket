-- OMN-19861. Projection writer's exact read and write grants.
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.demo_readiness_latest
    TO omninode_runtime;
GRANT USAGE ON SEQUENCE omninode_internal.demo_readiness_latest_projection_cursor_seq
    TO omninode_runtime;
