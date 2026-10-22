-- One row per account (BAN): who they are, where they are served (CLLI wire
-- center, copper or fiber plant), what they buy.
select
    c.ban,
    c.customer_name,
    c.segment,
    c.state,
    r.state_name,
    r.region,
    c.city,
    c.zip,
    c.census_block,
    c.latitude,
    c.longitude,
    c.signup_date,
    c.credit_band,
    c.is_autopay,
    c.is_paperless,
    c.email,
    c.phone,
    c.preferred_channel,
    l.location_id,
    coalesce(l.clli, c.clli) as clli,
    w.wire_center_name,
    l.technology,
    l.is_fiber_available,
    l.is_competitor_fiber_available,
    l.max_down_mbps,
    l.plant_build_year,
    l.ont_model,
    l.loop_length_kft,
    s.subscription_id,
    s.plan_id,
    p.plan_name,
    p.speed_tier,
    p.tier_rank,
    p.down_mbps,
    p.up_mbps,
    p.list_price,
    p.upgrade_target_plan_id,
    s.contract_term,
    s.subscription_status,
    s.start_date,
    s.end_date,
    s.churn_month,
    s.cancel_reason,
    s.subscription_status = 'active' as is_active
from {{ ref('stg_customers') }} as c
left join {{ ref('stg_service_locations') }} as l on l.ban = c.ban
left join {{ ref('stg_wire_centers') }} as w on w.clli = coalesce(l.clli, c.clli)
left join {{ ref('stg_subscriptions') }} as s on s.ban = c.ban
left join {{ ref('stg_plans') }} as p on p.plan_id = s.plan_id
left join {{ ref('state_region') }} as r on r.state = c.state
