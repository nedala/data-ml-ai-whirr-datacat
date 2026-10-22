-- Copper DSL line quality per account × month. SNR margin below 6 dB is the
-- "marginal line" threshold (retrains, CRC errors, speed loss).
select
    cast(ban as {{ dbt.type_string() }})                    as ban,
    cast(metric_month as date)         as metric_month,
    cast(snr_margin_db as {{ dbt.type_float() }})          as snr_margin_db,
    cast(attenuation_db as {{ dbt.type_float() }})         as attenuation_db,
    cast(sync_rate_down_mbps as {{ dbt.type_float() }})    as sync_rate_down_mbps,
    cast(retrains as {{ dbt.type_int() }})               as retrains,
    cast(crc_errors as {{ dbt.type_int() }})             as crc_errors,
    cast(snr_margin_db as {{ dbt.type_float() }}) < 6      as is_marginal_line
from {{ source('raw', 'copper_line_metrics') }}
