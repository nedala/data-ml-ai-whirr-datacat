-- One row per billing account (BAN). PII columns (customer_name, email,
-- phone, zip, census_block, latitude, longitude) are kept here and in
-- dim_customer only; downstream facts carry ban.
select
    cast(ban as {{ dbt.type_string() }})                 as ban,
    cast(customer_name as {{ dbt.type_string() }})       as customer_name,
    cast(segment as {{ dbt.type_string() }})             as segment,
    cast(state as {{ dbt.type_string() }})               as state,
    cast(city as {{ dbt.type_string() }})                as city,
    cast(clli as {{ dbt.type_string() }})                as clli,
    cast(zip as {{ dbt.type_string() }})                 as zip,
    cast(census_block as {{ dbt.type_string() }})        as census_block,
    cast(latitude as {{ dbt.type_float() }})            as latitude,
    cast(longitude as {{ dbt.type_float() }})           as longitude,
    cast(signup_date as date)       as signup_date,
    cast(credit_band as {{ dbt.type_string() }})         as credit_band,
    cast(autopay as {{ dbt.type_boolean() }})             as is_autopay,
    cast(paperless as {{ dbt.type_boolean() }})           as is_paperless,
    cast(email as {{ dbt.type_string() }})               as email,
    cast(phone as {{ dbt.type_string() }})               as phone,
    cast(preferred_channel as {{ dbt.type_string() }})   as preferred_channel
from {{ source('raw', 'customers') }}
