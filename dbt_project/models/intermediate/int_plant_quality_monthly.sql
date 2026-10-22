-- Plant quality per account × month: fiber ONT optics or copper DSL line
-- metrics in one shape (the other technology's columns are NULL).
select
    o.ban,
    o.metric_month,
    'fiber'                      as technology,
    o.rx_power_dbm,
    o.los_events,
    o.bip_errors,
    o.reboots,
    o.uptime_pct,
    o.is_weak_optics,
    cast(null as {{ dbt.type_float() }})   as snr_margin_db,
    cast(null as {{ dbt.type_float() }})   as attenuation_db,
    cast(null as {{ dbt.type_float() }})   as sync_rate_down_mbps,
    cast(null as {{ dbt.type_int() }})     as retrains,
    cast(null as {{ dbt.type_int() }})     as crc_errors,
    cast(null as {{ dbt.type_boolean() }}) as is_marginal_line,
    o.is_weak_optics             as is_bad_line
from {{ ref('stg_ont_metrics') }} as o

union all

select
    d.ban,
    d.metric_month,
    'copper'                     as technology,
    cast(null as {{ dbt.type_float() }})   as rx_power_dbm,
    cast(null as {{ dbt.type_int() }})     as los_events,
    cast(null as {{ dbt.type_int() }})     as bip_errors,
    cast(null as {{ dbt.type_int() }})     as reboots,
    cast(null as {{ dbt.type_float() }})   as uptime_pct,
    cast(null as {{ dbt.type_boolean() }}) as is_weak_optics,
    d.snr_margin_db,
    d.attenuation_db,
    d.sync_rate_down_mbps,
    d.retrains,
    d.crc_errors,
    d.is_marginal_line,
    d.is_marginal_line           as is_bad_line
from {{ ref('stg_copper_line_metrics') }} as d
