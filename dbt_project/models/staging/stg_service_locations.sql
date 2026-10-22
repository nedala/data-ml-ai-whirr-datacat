select
    cast(location_id as {{ dbt.type_string() }})                  as location_id,
    cast(ban as {{ dbt.type_string() }})                          as ban,
    cast(clli as {{ dbt.type_string() }})                         as clli,
    cast(technology as {{ dbt.type_string() }})                   as technology,
    cast(fiber_available as {{ dbt.type_boolean() }})              as is_fiber_available,
    cast(competitor_fiber_available as {{ dbt.type_boolean() }})   as is_competitor_fiber_available,
    cast(max_down_mbps as {{ dbt.type_int() }})                as max_down_mbps,
    cast(plant_build_year as {{ dbt.type_int() }})             as plant_build_year,
    cast(ont_model as {{ dbt.type_string() }})                    as ont_model,
    cast(loop_length_kft as {{ dbt.type_float() }})              as loop_length_kft
from {{ source('raw', 'service_locations') }}
