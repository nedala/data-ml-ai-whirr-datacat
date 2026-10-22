-- Rate card enriched with the plan catalogue seed (tier, upgrade path).
select
    cast(p.plan_id as {{ dbt.type_string() }})      as plan_id,
    cast(p.plan_name as {{ dbt.type_string() }})    as plan_name,
    cast(p.technology as {{ dbt.type_string() }})   as technology,
    cast(p.down_mbps as {{ dbt.type_int() }})       as down_mbps,
    cast(p.up_mbps as {{ dbt.type_int() }})         as up_mbps,
    cast(p.monthly_price as {{ dbt.type_float() }}) as list_price,
    cast(p.promo_price as {{ dbt.type_float() }})   as promo_price,
    cast(p.promo_months as {{ dbt.type_int() }})    as promo_months,
    c.speed_tier,
    cast(c.tier_rank as {{ dbt.type_int() }})       as tier_rank,
    cast(c.is_gig_plus as {{ dbt.type_boolean() }}) as is_gig_plus,
    c.upgrade_target_plan_id
from {{ source('raw', 'plans') }} as p
left join {{ ref('plan_catalog') }} as c
    on c.plan_id = p.plan_id
