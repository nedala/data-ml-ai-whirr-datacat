-- Month-over-month MRR bridge (recurring service revenue). Every account's
-- change from its previous bill is classified as:
--   new · upgrade_to_fiber · upgrade_plan · downgrade · price_change · churn
-- Σ mrr_change for a month = MRR(month) − MRR(previous month)
-- (tests/assert_mrr_bridge_reconciles.sql).
with inv as (
    select
        i.ban,
        i.bill_month,
        i.service_revenue,
        coalesce(m.plan_id, i.plan_id)        as plan_id,
        coalesce(m.technology, i.technology)  as technology,
        i.state
    from {{ ref('fct_invoice') }} as i
    left join {{ ref('stg_customer_monthly') }} as m
        on m.ban = i.ban and m.snapshot_month = i.bill_month
),

ranked as (
    select
        inv.*,
        p.tier_rank,
        lag(inv.service_revenue) over (partition by inv.ban order by inv.bill_month) as prev_revenue,
        lag(inv.bill_month)      over (partition by inv.ban order by inv.bill_month) as prev_month,
        lag(p.tier_rank)         over (partition by inv.ban order by inv.bill_month) as prev_tier,
        lag(inv.technology)      over (partition by inv.ban order by inv.bill_month) as prev_technology,
        lead(inv.bill_month)     over (partition by inv.ban order by inv.bill_month) as next_month
    from inv
    left join {{ ref('stg_plans') }} as p on p.plan_id = inv.plan_id
),

bounds as (select min(bill_month) as min_month, max(bill_month) as max_month from inv),

movements as (
    select
        r.bill_month as movement_month,
        case
            when r.prev_month is null then 'new'
            when r.tier_rank > r.prev_tier and r.technology = 'fiber' and r.prev_technology <> 'fiber' then 'upgrade_to_fiber'
            when r.tier_rank > r.prev_tier then 'upgrade_plan'
            when r.tier_rank < r.prev_tier then 'downgrade'
            else 'price_change'
        end as movement_type,
        r.technology,
        r.state,
        r.ban,
        r.service_revenue - coalesce(r.prev_revenue, 0) as mrr_change
    from ranked as r
    cross join bounds
    where r.bill_month > bounds.min_month
      and (r.prev_month is null or r.service_revenue <> r.prev_revenue or r.tier_rank <> r.prev_tier)

    union all

    select
        {{ add_months('r.bill_month', 1) }} as movement_month,
        'churn'                              as movement_type,
        r.technology,
        r.state,
        r.ban,
        -1 * r.service_revenue               as mrr_change
    from ranked as r
    cross join bounds
    where r.next_month is null and r.bill_month < bounds.max_month
)

select
    movement_month,
    movement_type,
    case movement_type
        when 'new' then 1 when 'upgrade_to_fiber' then 2 when 'upgrade_plan' then 3
        when 'price_change' then 4 when 'downgrade' then 5 else 6 end as movement_order,
    technology,
    state,
    count(distinct ban) as accounts,
    sum(mrr_change)            as mrr_change
from movements
group by movement_month, movement_type, technology, state
