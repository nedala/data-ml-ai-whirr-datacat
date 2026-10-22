with src as (
    select
        cast(ticket_id as {{ dbt.type_string() }})                as ticket_id,
        cast(ban as {{ dbt.type_string() }})                      as ban,
        cast(opened_at as {{ dbt.type_timestamp() }})                as opened_at,
        cast(closed_at as {{ dbt.type_timestamp() }})                as closed_at,
        cast(category as {{ dbt.type_string() }})                 as category,
        cast(subcategory as {{ dbt.type_string() }})              as subcategory,
        cast(channel as {{ dbt.type_string() }})                  as channel,
        cast(first_contact_resolved as {{ dbt.type_boolean() }})   as is_first_contact_resolved
    from {{ source('raw', 'service_tickets') }}
)
select
    src.*,
    cast(opened_at as date)                       as opened_date,
    {{ month_start('opened_at') }}                as opened_month,
    {{ hours_between('opened_at', 'closed_at') }} as resolution_hours
from src
