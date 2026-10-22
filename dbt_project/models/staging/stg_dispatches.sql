with src as (
    select
        cast(dispatch_id as {{ dbt.type_string() }})          as dispatch_id,
        cast(ban as {{ dbt.type_string() }})                  as ban,
        cast(ticket_id as {{ dbt.type_string() }})            as ticket_id,
        cast(clli as {{ dbt.type_string() }})                 as clli,
        cast(dispatch_type as {{ dbt.type_string() }})        as dispatch_type,
        cast(scheduled_at as {{ dbt.type_timestamp() }})         as scheduled_at,
        cast(completed_at as {{ dbt.type_timestamp() }})         as completed_at,
        cast(status as {{ dbt.type_string() }})               as dispatch_status,
        cast(tech_id as {{ dbt.type_string() }})              as tech_id,
        cast(cause as {{ dbt.type_string() }})                as cause,
        cast(repeat_dispatch_30d as {{ dbt.type_boolean() }})  as is_repeat_dispatch_30d
    from {{ source('raw', 'dispatches') }}
)
select
    src.*,
    cast(scheduled_at as date)                          as scheduled_date,
    {{ month_start('scheduled_at') }}                   as dispatch_month,
    {{ hours_between('scheduled_at', 'completed_at') }} as onsite_hours
from src
