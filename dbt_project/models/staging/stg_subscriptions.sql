select
    cast(subscription_id as {{ dbt.type_string() }})  as subscription_id,
    cast(ban as {{ dbt.type_string() }})              as ban,
    cast(plan_id as {{ dbt.type_string() }})          as plan_id,
    cast(start_date as date)     as start_date,
    cast(end_date as date)       as end_date,
    cast(status as {{ dbt.type_string() }})           as subscription_status,
    cast(cancel_reason as {{ dbt.type_string() }})    as cancel_reason,
    cast(contract_term as {{ dbt.type_string() }})    as contract_term,
    case when end_date is not null then {{ month_start('end_date') }} end as churn_month
from {{ source('raw', 'subscriptions') }}
