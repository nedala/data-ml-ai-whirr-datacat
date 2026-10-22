-- Fiber ONT optics per account × month. Rx power below -25 dBm is the
-- "weak optics" threshold (dirty connector, macro-bend, damaged drop).
select
    cast(ban as {{ dbt.type_string() }})                 as ban,
    cast(metric_month as date)      as metric_month,
    cast(ont_model as {{ dbt.type_string() }})           as ont_model,
    cast(rx_power_dbm as {{ dbt.type_float() }})        as rx_power_dbm,
    cast(rx_power_min_dbm as {{ dbt.type_float() }})    as rx_power_min_dbm,
    cast(tx_power_dbm as {{ dbt.type_float() }})        as tx_power_dbm,
    cast(los_events as {{ dbt.type_int() }})          as los_events,
    cast(bip_errors as {{ dbt.type_int() }})          as bip_errors,
    cast(reboots as {{ dbt.type_int() }})             as reboots,
    cast(uptime_pct as {{ dbt.type_float() }})          as uptime_pct,
    cast(rx_power_dbm as {{ dbt.type_float() }}) < -25  as is_weak_optics
from {{ source('raw', 'ont_metrics') }}
