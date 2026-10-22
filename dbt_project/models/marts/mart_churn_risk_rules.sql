-- Rules-based churn risk for the latest snapshot month — the fallback the
-- "high-risk accounts" view uses until the ML model writes
-- mlops.scored_customers. Points mirror the drivers built into the data
-- (copper plant, weak optics / marginal DSL lines, competitor fiber,
-- price increase, late payments, tickets, repeat dispatches, disputes,
-- NPS, outages). Not a model; a transparent triage list.
with latest as (
    select max(snapshot_month) as snapshot_month from {{ ref('mart_churn_features') }}
),

f as (
    select m.*
    from {{ ref('mart_churn_features') }} as m
    join latest on latest.snapshot_month = m.snapshot_month
),

pts as (
    select
        f.*,
        case when f.technology = 'copper' then 15 else 0 end         as p_tech,
        case when f.ont_rx_power_dbm < -25 or f.dsl_snr_margin_db < 6 then 10 else 0 end as p_plant,
        case when f.dispatches_90d >= 0.7 then 10 else 0 end        as p_dispatch,
        case when f.disputes_90d >= 0.5 then 10 else 0 end          as p_dispute,
        case when f.competitor_fiber_available then 15 else 0 end   as p_comp,
        case when f.recent_price_increase then 20 else 0 end        as p_price,
        case when f.promo_expired then 10 else 0 end                as p_promo,
        case when f.late_payments_decayed >= 0.5 then 10 else 0 end as p_late,
        case when f.tickets_decayed >= 1 then 10 else 0 end         as p_tix,
        case when f.nps_last <= 6 then 15 else 0 end                as p_nps,
        case when f.outage_minutes > 120 then 10 else 0 end         as p_outage,
        case when not f.autopay then 5 else 0 end                   as p_autopay
    from f
),

scored as (
    select
        pts.*,
        least(p_tech + p_plant + p_dispatch + p_dispute + p_comp + p_price + p_promo + p_late + p_tix + p_nps + p_outage + p_autopay, 100) as risk_points
    from pts
)

select
    ban,
    snapshot_month,
    technology,
    plan_id,
    segment,
    state,
    region,
    clli,
    monthly_charge,
    tenure_months,
    risk_points,
    {{ safe_divide('risk_points', '100') }}                     as risk_score,
    ntile(10) over (order by risk_points, ban)           as risk_decile,
    case when risk_points >= 50 then 'high' when risk_points >= 30 then 'medium' else 'low' end as risk_band,
    {{ dbt.concat([
        "case when p_price > 0 then 'recent price increase; ' else '' end",
        "case when p_comp > 0 then 'competitor fiber; ' else '' end",
        "case when p_nps > 0 then 'detractor NPS; ' else '' end",
        "case when p_tech > 0 then 'copper plant; ' else '' end",
        "case when p_plant > 0 then 'weak optics / marginal line; ' else '' end",
        "case when p_dispatch > 0 then 'repeat dispatches; ' else '' end",
        "case when p_dispute > 0 then 'billing disputes; ' else '' end",
        "case when p_late > 0 then 'late payments; ' else '' end",
        "case when p_tix > 0 then 'repeat tickets; ' else '' end",
        "case when p_outage > 0 then 'outages; ' else '' end",
        "case when p_promo > 0 then 'promo expired; ' else '' end",
        "case when p_autopay > 0 then 'no autopay; ' else '' end"
    ]) }}                                                       as risk_reasons,
    'rules-v2'                                                  as model_version
from scored
