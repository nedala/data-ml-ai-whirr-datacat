-- One row per contact-center call with queue, reason, disposition, handle
-- time, repeat flag, sentiment and the account's dimensions.
select
    k.call_id,
    k.ban,
    k.call_start,
    k.call_date,
    k.call_month,
    k.queue,
    k.reason,
    k.disposition,
    k.handle_time_sec,
    {{ safe_divide('k.handle_time_sec', '60') }}    as handle_time_min,
    k.is_repeat_call_7d,
    k.agent_id,
    k.sentiment_score,
    k.sentiment_band,
    k.disposition = 'resolved'                      as is_resolved,
    c.technology,
    c.plan_id,
    c.segment,
    c.state,
    c.region,
    c.clli
from {{ ref('stg_customer_calls') }} as k
left join {{ ref('int_customer_current_service') }} as c on c.ban = k.ban
