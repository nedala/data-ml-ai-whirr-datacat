select
    cast(ban as {{ dbt.type_string() }})                  as ban,
    cast(revenue_month as date)      as revenue_month,
    cast(technology as {{ dbt.type_string() }})           as technology,
    cast(plan_id as {{ dbt.type_string() }})              as plan_id,
    cast(clli as {{ dbt.type_string() }})                 as clli,
    cast(state as {{ dbt.type_string() }})                as state,
    cast(recurring_revenue as {{ dbt.type_float() }})    as recurring_revenue,
    cast(one_time_revenue as {{ dbt.type_float() }})     as one_time_revenue,
    cast(late_fee_revenue as {{ dbt.type_float() }})     as late_fee_revenue,
    cast(credits as {{ dbt.type_float() }})              as credits,
    cast(net_revenue as {{ dbt.type_float() }})          as net_revenue
from {{ source('raw', 'revenue') }}
