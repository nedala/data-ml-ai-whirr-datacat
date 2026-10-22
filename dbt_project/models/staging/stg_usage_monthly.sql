select
    cast(ban as {{ dbt.type_string() }})               as ban,
    cast(usage_month as date)     as usage_month,
    cast(gb_down as {{ dbt.type_float() }})           as gb_down,
    cast(gb_up as {{ dbt.type_float() }})             as gb_up,
    cast(avg_latency_ms as {{ dbt.type_float() }})    as avg_latency_ms,
    cast(outage_minutes as {{ dbt.type_float() }})    as outage_minutes,
    cast(wifi_devices as {{ dbt.type_int() }})      as wifi_devices
from {{ source('raw', 'usage_monthly') }}
