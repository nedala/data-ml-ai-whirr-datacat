with src as (
    select
        cast(event_id as {{ dbt.type_string() }})      as event_id,
        cast(clli as {{ dbt.type_string() }})          as clli,
        cast(started_at as {{ dbt.type_timestamp() }})    as started_at,
        cast(ended_at as {{ dbt.type_timestamp() }})      as ended_at,
        cast(event_type as {{ dbt.type_string() }})    as event_type,
        cast(duration_min as {{ dbt.type_float() }})  as duration_minutes
    from {{ source('raw', 'network_events') }}
)
select
    src.*,
    {{ month_start('started_at') }}          as event_month,
    event_type in ('outage', 'degradation')  as is_unplanned
from src
