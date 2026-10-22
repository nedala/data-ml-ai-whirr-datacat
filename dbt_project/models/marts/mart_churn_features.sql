-- Model-ready churn training / scoring table: one row per account (BAN) ×
-- snapshot month. Feature names follow raw.customer_monthly exactly (so the
-- ML pipelines can read either) plus serving-location, CLLI wire-center,
-- billing and dispute context.
with wc as (
    select clli, event_month, outage_events, outage_minutes as wire_center_outage_minutes
    from {{ ref('int_wire_center_monthly_events') }}
),

bill as (
    select
        ban,
        bill_month,
        max({{ flag('is_late') }})   as invoice_late,
        sum(dispute_count)           as disputes_month,
        sum(amount_due)              as amount_due
    from {{ ref('int_invoice_settlement') }}
    group by ban, bill_month
)

select
    m.ban,
    m.snapshot_month,
    m.technology,
    m.plan_id,
    c.speed_tier,
    c.contract_term,
    m.segment,
    m.state,
    c.region,
    m.clli,
    c.wire_center_name,
    m.monthly_charge,
    m.tenure_months,
    m.is_autopay                         as autopay,
    c.is_paperless                       as paperless,
    m.credit_band,
    c.preferred_channel,
    c.is_fiber_available                 as fiber_available,
    m.is_competitor_fiber_available      as competitor_fiber_available,
    m.gb_down,
    m.avg_latency_ms,
    m.outage_minutes,
    coalesce(wc.outage_events, 0)              as wire_center_outage_events,
    coalesce(wc.wire_center_outage_minutes, 0) as wire_center_outage_minutes,
    m.tickets_month,
    m.tickets_decayed,
    m.calls_30d,
    m.calls_decayed,
    m.dispatches_90d,
    m.disputes_90d,
    m.late_payments_decayed,
    coalesce(bill.invoice_late, 0)       as invoice_late,
    coalesce(bill.disputes_month, 0)     as disputes_month,
    bill.amount_due,
    m.nps_last,
    m.ont_rx_power_dbm,
    m.ont_los_events,
    m.dsl_snr_margin_db,
    m.dsl_retrains,
    m.had_recent_price_increase          as recent_price_increase,
    m.is_promo_expired                   as promo_expired,
    m.churned_next_90d,
    m.is_label_complete                  as label_complete
from {{ ref('stg_customer_monthly') }} as m
left join {{ ref('int_customer_current_service') }} as c on c.ban = m.ban
left join wc on wc.clli = m.clli and wc.event_month = m.snapshot_month
left join bill on bill.ban = m.ban and bill.bill_month = m.snapshot_month
