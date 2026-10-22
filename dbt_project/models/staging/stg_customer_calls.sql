with src as (
    select
        cast(call_id as {{ dbt.type_string() }})           as call_id,
        cast(ban as {{ dbt.type_string() }})               as ban,
        cast(call_start as {{ dbt.type_timestamp() }})        as call_start,
        cast(duration_sec as {{ dbt.type_int() }})      as handle_time_sec,
        cast(queue as {{ dbt.type_string() }})             as queue,
        cast(reason as {{ dbt.type_string() }})            as reason,
        cast(disposition as {{ dbt.type_string() }})       as disposition,
        cast(repeat_call_7d as {{ dbt.type_boolean() }})    as is_repeat_call_7d,
        cast(agent_id as {{ dbt.type_string() }})          as agent_id,
        cast(sentiment_score as {{ dbt.type_float() }})   as sentiment_score
    from {{ source('raw', 'customer_calls') }}
)
select
    src.*,
    cast(call_start as date)          as call_date,
    {{ month_start('call_start') }}   as call_month,
    case when sentiment_score >= 0.25 then 'positive'
         when sentiment_score <= -0.25 then 'negative'
         else 'neutral' end           as sentiment_band
from src
