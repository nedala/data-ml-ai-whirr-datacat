-- Fiber availability, take rate and copper → fiber upgrade opportunity by
-- state × CLLI wire center (current, active accounts), with the wire
-- center's fiber passings (homes passed) and copper lines.
with acct as (
    select
        s.*,
        t.list_price as upgrade_target_price
    from {{ ref('int_customer_current_service') }} as s
    left join {{ ref('stg_plans') }} as t on t.plan_id = s.upgrade_target_plan_id
),

agg as (
    select
        a.clli,
        count(*)                                                                     as accounts_total,
        sum({{ flag('a.is_active') }})                                               as active_accounts,
        sum({{ flag('a.is_active and a.is_fiber_available') }})                      as fiber_passed_active,
        sum({{ flag("a.is_active and a.technology = 'fiber'") }})                    as fiber_subscribers,
        sum({{ flag("a.is_active and a.technology = 'copper'") }})                   as copper_subscribers,
        sum({{ flag("a.is_active and a.technology = 'copper' and a.is_fiber_available") }}) as upgrade_opportunity_accounts,
        sum(case when a.is_active and a.technology = 'copper' and a.is_fiber_available
                 then a.upgrade_target_price - a.list_price else 0 end)              as upgrade_opportunity_mrr,
        sum({{ flag("a.is_active and a.technology = 'copper' and not a.is_fiber_available") }}) as copper_not_passed_active,
        sum({{ flag('a.is_active and a.is_competitor_fiber_available') }})           as competitor_fiber_active,
        sum({{ flag("a.is_active and a.technology = 'copper' and a.is_competitor_fiber_available") }}) as copper_at_risk_competitor,
        sum({{ flag("not a.is_active and a.cancel_reason = 'competitor_fiber'") }})  as lost_to_competitor_fiber,
        sum({{ flag('not a.is_active') }})                                           as churned_accounts
    from acct as a
    group by a.clli
)

select
    w.state,
    r.state_name,
    r.region,
    w.clli,
    w.wire_center_name,
    w.city,
    r.fiber_build_priority,
    w.fiber_build_year,
    w.fiber_passings,
    w.copper_lines,
    coalesce(g.accounts_total, 0)                 as accounts_total,
    coalesce(g.active_accounts, 0)                as active_accounts,
    coalesce(g.fiber_passed_active, 0)            as fiber_passed_active,
    coalesce(g.fiber_subscribers, 0)              as fiber_subscribers,
    coalesce(g.copper_subscribers, 0)             as copper_subscribers,
    coalesce(g.upgrade_opportunity_accounts, 0)   as upgrade_opportunity_accounts,
    coalesce(g.upgrade_opportunity_mrr, 0)        as upgrade_opportunity_mrr,
    coalesce(g.copper_not_passed_active, 0)       as copper_not_passed_active,
    coalesce(g.competitor_fiber_active, 0)        as competitor_fiber_active,
    coalesce(g.copper_at_risk_competitor, 0)      as copper_at_risk_competitor,
    coalesce(g.lost_to_competitor_fiber, 0)       as lost_to_competitor_fiber,
    {{ safe_divide('g.fiber_passed_active', 'g.active_accounts') }}       as fiber_availability_rate,
    {{ safe_divide('g.fiber_subscribers', 'g.fiber_passed_active') }}     as fiber_take_rate,
    {{ safe_divide('g.fiber_subscribers', 'w.fiber_passings') }}          as fiber_penetration_of_passings,
    {{ safe_divide('g.competitor_fiber_active', 'g.active_accounts') }}   as competitor_overlap_rate,
    {{ safe_divide('g.churned_accounts', 'g.accounts_total') }}           as churned_share
from {{ ref('stg_wire_centers') }} as w
left join agg as g on g.clli = w.clli
left join {{ ref('state_region') }} as r on r.state = w.state
