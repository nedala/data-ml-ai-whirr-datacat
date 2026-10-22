-- Customer dimension: one row per account (BAN) with current service, CLLI
-- wire center, tenure and lifetime activity. PII columns are tagged in
-- _marts.yml (meta.pii).
with inv as (
    select
        ban,
        count(*)                   as invoices_lifetime,
        sum(service_revenue)       as service_revenue_lifetime,
        sum(amount_due)            as billed_lifetime,
        sum({{ flag('is_late') }}) as late_invoices_lifetime,
        max(bill_month)            as last_bill_month
    from {{ ref('int_invoice_settlement') }}
    group by ban
),

tix as (
    select ban, count(*) as tickets_lifetime
    from {{ ref('stg_service_tickets') }}
    group by ban
),

disp as (
    select
        ban,
        count(*)                                    as dispatches_lifetime,
        sum({{ flag('is_repeat_dispatch_30d') }})   as repeat_dispatches_lifetime
    from {{ ref('stg_dispatches') }}
    group by ban
),

calls as (
    select ban, count(*) as calls_lifetime, avg(sentiment_score) as avg_call_sentiment
    from {{ ref('stg_customer_calls') }}
    group by ban
),

dsp as (
    select ban, count(*) as disputes_lifetime, sum(credit_amount) as dispute_credits_lifetime
    from {{ ref('stg_disputes') }}
    group by ban
),

nps as (
    select ban, nps_score as last_nps_score, nps_band as last_nps_band
    from (
        select
            ban, nps_score, nps_band,
            row_number() over (partition by ban order by survey_date desc, survey_id desc) as rn
        from {{ ref('stg_nps_surveys') }}
    ) as ranked
    where rn = 1
)

select
    s.ban,
    s.customer_name,
    s.email,
    s.phone,
    s.segment,
    s.state,
    s.state_name,
    s.region,
    s.city,
    s.zip,
    s.latitude,
    s.longitude,
    s.clli,
    s.wire_center_name,
    s.technology,
    s.is_fiber_available,
    s.is_competitor_fiber_available,
    s.ont_model,
    s.loop_length_kft,
    s.plan_id,
    s.plan_name,
    s.speed_tier,
    s.down_mbps,
    s.list_price,
    s.upgrade_target_plan_id,
    s.contract_term,
    s.credit_band,
    s.is_autopay,
    s.is_paperless,
    s.preferred_channel,
    s.signup_date,
    s.subscription_status,
    s.is_active,
    s.end_date                                     as churn_date,
    s.churn_month,
    s.cancel_reason,
    {{ safe_divide(days_between('s.signup_date', 'coalesce(s.end_date, ' ~ date_literal(var('as_of_date')) ~ ')'), '30.4') }} as tenure_months,
    case
        when {{ days_between('s.signup_date', 'coalesce(s.end_date, ' ~ date_literal(var('as_of_date')) ~ ')') }} < 365  then '1: <1 year'
        when {{ days_between('s.signup_date', 'coalesce(s.end_date, ' ~ date_literal(var('as_of_date')) ~ ')') }} < 730  then '2: 1-2 years'
        when {{ days_between('s.signup_date', 'coalesce(s.end_date, ' ~ date_literal(var('as_of_date')) ~ ')') }} < 1461 then '3: 2-4 years'
        else '4: 4+ years'
    end                                            as tenure_band,
    coalesce(inv.invoices_lifetime, 0)             as invoices_lifetime,
    coalesce(inv.service_revenue_lifetime, 0)      as service_revenue_lifetime,
    coalesce(inv.billed_lifetime, 0)               as billed_lifetime,
    coalesce(inv.late_invoices_lifetime, 0)        as late_invoices_lifetime,
    inv.last_bill_month,
    coalesce(tix.tickets_lifetime, 0)              as tickets_lifetime,
    coalesce(disp.dispatches_lifetime, 0)          as truck_rolls_lifetime,
    coalesce(disp.repeat_dispatches_lifetime, 0)   as repeat_dispatches_lifetime,
    coalesce(calls.calls_lifetime, 0)              as calls_lifetime,
    calls.avg_call_sentiment,
    coalesce(dsp.disputes_lifetime, 0)             as disputes_lifetime,
    coalesce(dsp.dispute_credits_lifetime, 0)      as dispute_credits_lifetime,
    nps.last_nps_score,
    nps.last_nps_band
from {{ ref('int_customer_current_service') }} as s
left join inv on inv.ban = s.ban
left join tix on tix.ban = s.ban
left join disp on disp.ban = s.ban
left join calls on calls.ban = s.ban
left join dsp on dsp.ban = s.ban
left join nps on nps.ban = s.ban
