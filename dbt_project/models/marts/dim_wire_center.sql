-- Wire center dimension: one row per CLLI with plant inventory (fiber
-- passings, copper lines, OLTs, DSLAMs), region and the current customer base.
with base as (
    select
        clli,
        count(*)                                                    as accounts_total,
        sum({{ flag('is_active') }})                                as active_accounts,
        sum({{ flag("is_active and technology = 'fiber'") }})      as fiber_subscribers,
        sum({{ flag("is_active and technology = 'copper'") }})     as copper_subscribers
    from {{ ref('int_customer_current_service') }}
    group by clli
)

select
    w.clli,
    w.wire_center_name,
    w.city,
    w.state,
    r.state_name,
    r.region,
    r.fiber_build_priority,
    w.fiber_passings,
    w.copper_lines,
    w.olt_count,
    w.dslam_count,
    w.fiber_build_year,
    w.latitude,
    w.longitude,
    coalesce(b.accounts_total, 0)       as accounts_total,
    coalesce(b.active_accounts, 0)      as active_accounts,
    coalesce(b.fiber_subscribers, 0)    as fiber_subscribers,
    coalesce(b.copper_subscribers, 0)   as copper_subscribers,
    {{ safe_divide('coalesce(b.fiber_subscribers, 0)', 'b.active_accounts') }} as fiber_share
from {{ ref('stg_wire_centers') }} as w
left join base as b on b.clli = w.clli
left join {{ ref('state_region') }} as r on r.state = w.state
