-- OMN-20032 false-pass growth: candidate identities plus content-free metadata, house tenant, 2026-08-25 to the stated window end.
select correlation_id,
       greatest(coalesce(jsonb_array_length(case when jsonb_typeof(attempt_history)='array' then attempt_history end),1)-1,0) as attempt_index,
       tenant_id::text, task_type,
       case when quality_gate_passed then 'accepted' else 'refused' end as gate_outcome,
       coalesce(delegated_to,'') as delegated_to, coalesce(model_name,'') as model_name,
       coalesce(terminal_ok::text,'') as terminal_ok, coalesce(ticket_id,'') as ticket_id,
       to_char(created_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"') as created_at,
       length(prompt_text) as prompt_chars, length(response_text) as response_chars,
       left(md5(prompt_text),12) as prompt_hash
from public.delegation_events
where created_at >= timestamptz '2026-08-25T00:00:00Z' and created_at < timestamptz '__WINDOW_END__'
  and coalesce(is_shadow,false) = false
  and quality_gate_passed is not null
  and coalesce(prompt_text,'') <> '' and coalesce(response_text,'') <> ''
  and task_type in ('code_generation','code_review','document','planning','reasoning','research','review','summarization','test')
order by correlation_id;
