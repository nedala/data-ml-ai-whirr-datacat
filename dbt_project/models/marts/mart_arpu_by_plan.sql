-- ARPU, promo-discount depth and subscribers by month × plan (raw.revenue).
-- discount_amount = list price less recurring revenue where positive (promo).
select
    r.revenue_month                                                   as bill_month,
    r.plan_id,
    p.plan_name,
    p.technology,
    p.speed_tier,
    p.list_price,
    count(distinct r.ban)                                             as subscribers,
    sum(r.recurring_revenue)                                          as service_revenue,
    sum(r.net_revenue)                                                as net_revenue,
    sum(case when p.list_price > r.recurring_revenue
             then p.list_price - r.recurring_revenue else 0 end)      as discount_amount,
    {{ safe_divide('sum(r.recurring_revenue)', 'count(distinct r.ban)') }}   as arpu,
    {{ safe_divide("sum(case when p.list_price > r.recurring_revenue then p.list_price - r.recurring_revenue else 0 end)", 'count(distinct r.ban) * max(p.list_price)') }} as discount_share,
    {{ safe_divide('sum(r.recurring_revenue)', 'count(distinct r.ban) * max(p.list_price)') }} as realised_price_ratio
from {{ ref('stg_revenue') }} as r
left join {{ ref('stg_plans') }} as p on p.plan_id = r.plan_id
group by r.revenue_month, r.plan_id, p.plan_name, p.technology, p.speed_tier, p.list_price
