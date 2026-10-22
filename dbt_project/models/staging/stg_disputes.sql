select
    cast(dispute_id as {{ dbt.type_string() }})         as dispute_id,
    cast(ban as {{ dbt.type_string() }})                as ban,
    cast(invoice_id as {{ dbt.type_string() }})         as invoice_id,
    cast(opened_date as date)      as opened_date,
    {{ month_start('opened_date') }} as opened_month,
    cast(channel as {{ dbt.type_string() }})            as channel,
    cast(category as {{ dbt.type_string() }})           as category,
    cast(disputed_amount as {{ dbt.type_float() }})    as disputed_amount,
    cast(status as {{ dbt.type_string() }})             as dispute_status,
    cast(resolved_date as date)    as resolved_date,
    cast(credit_amount as {{ dbt.type_float() }})      as credit_amount,
    case when resolved_date is not null
         then {{ days_between('cast(opened_date as date)', 'cast(resolved_date as date)') }} end as days_to_resolve
from {{ source('raw', 'disputes') }}
