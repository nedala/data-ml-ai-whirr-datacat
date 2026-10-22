-- Contact-center performance by month × queue × reason × technology: call
-- volume, repeat-call rate, average handle time, resolution and sentiment.
select
    call_month,
    queue,
    reason,
    technology,
    count(*)                                                as calls,
    count(distinct ban)                                     as accounts,
    sum({{ flag('is_repeat_call_7d') }})                    as repeat_calls,
    {{ safe_divide("sum(" ~ flag('is_repeat_call_7d') ~ ")", 'count(*)') }} as repeat_call_rate,
    avg(handle_time_sec)                                    as avg_handle_time_sec,
    {{ safe_divide('sum(handle_time_sec)', '3600') }}       as handle_hours,
    sum({{ flag("disposition = 'resolved'") }})             as resolved_calls,
    sum({{ flag("disposition = 'transferred'") }})          as transferred_calls,
    sum({{ flag("disposition = 'escalated'") }})            as escalated_calls,
    sum({{ flag("disposition = 'callback'") }})             as callback_calls,
    {{ safe_divide("sum(" ~ flag("disposition = 'resolved'") ~ ")", 'count(*)') }} as resolution_rate,
    avg(sentiment_score)                                    as avg_sentiment,
    sum({{ flag("sentiment_band = 'negative'") }})          as negative_calls,
    sum({{ flag("sentiment_band = 'positive'") }})          as positive_calls,
    {{ safe_divide("sum(" ~ flag("sentiment_band = 'negative'") ~ ")", 'count(*)') }} as negative_share
from {{ ref('fct_customer_call') }}
group by call_month, queue, reason, technology
