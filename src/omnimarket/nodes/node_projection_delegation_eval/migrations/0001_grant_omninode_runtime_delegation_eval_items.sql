-- OMN-19790: a BIGSERIAL sequence needs its own grant in addition to INSERT.
GRANT USAGE ON SCHEMA public TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE
    ON public.delegation_eval_items
    TO omninode_runtime;
GRANT USAGE
    ON SEQUENCE public.delegation_eval_items_projection_cursor_seq
    TO omninode_runtime;
