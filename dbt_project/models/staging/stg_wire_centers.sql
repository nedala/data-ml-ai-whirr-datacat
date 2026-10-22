-- Wire centers (central offices), keyed by CLLI.
select
    cast(clli as {{ dbt.type_string() }})               as clli,
    cast(wire_center_name as {{ dbt.type_string() }})   as wire_center_name,
    cast(state as {{ dbt.type_string() }})              as state,
    cast(city as {{ dbt.type_string() }})               as city,
    cast(fiber_passings as {{ dbt.type_int() }})     as fiber_passings,
    cast(copper_lines as {{ dbt.type_int() }})       as copper_lines,
    cast(olt_count as {{ dbt.type_int() }})          as olt_count,
    cast(dslam_count as {{ dbt.type_int() }})        as dslam_count,
    cast(fiber_build_year as {{ dbt.type_int() }})   as fiber_build_year,
    cast(latitude as {{ dbt.type_float() }})           as latitude,
    cast(longitude as {{ dbt.type_float() }})          as longitude
from {{ source('raw', 'wire_centers') }}
