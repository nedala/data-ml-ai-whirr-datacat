-- Revenue (raw.revenue), billing, collections, subscribers, ARPU and churn by
-- month × state × technology × segment.
with rev as (
    select
        r.revenue_month,
        r.state,
        sr.region,
        r.technology,
        c.segment,
        count(distinct r.ban)               as subscribers,
        sum(r.recurring_revenue)            as recurring_revenue,
        sum(r.one_time_revenue)             as one_time_revenue,
        sum(r.late_fee_revenue)             as late_fee_revenue,
        sum(r.credits)                      as credits,
        sum(r.net_revenue)                  as net_revenue,
        sum(i.amount_due)                   as billed_amount,
        sum(i.taxes_fees)                   as taxes_fees,
        sum(i.paid_amount)                  as collected_amount,
        sum({{ flag('i.is_late') }})        as late_invoices,
        sum({{ flag("i.invoice_status = 'written_off'") }}) as written_off_invoices,
        sum(i.dispute_count)                as disputes
    from {{ ref('stg_revenue') }} as r
    left join {{ ref('stg_customers') }} as c on c.ban = r.ban
    left join {{ ref('state_region') }} as sr on sr.state = r.state
    left join {{ ref('int_invoice_settlement') }} as i
        on i.ban = r.ban and i.bill_month = r.revenue_month
    group by r.revenue_month, r.state, sr.region, r.technology, c.segment
),

churned as (
    select
        churn_month as revenue_month,
        state,
        technology,
        segment,
        count(*)    as churned_subscribers
    from {{ ref('int_customer_current_service') }}
    where churn_month is not null
    group by churn_month, state, technology, segment
)

select
    b.revenue_month,
    b.state,
    b.region,
    b.technology,
    b.segment,
    b.subscribers,
    coalesce(ch.churned_subscribers, 0)                                as churned_subscribers,
    {{ safe_divide('coalesce(ch.churned_subscribers, 0)', 'b.subscribers') }} as churn_rate,
    b.recurring_revenue,
    b.recurring_revenue                                                as service_revenue,
    b.one_time_revenue,
    b.late_fee_revenue,
    b.credits,
    b.net_revenue,
    b.billed_amount,
    b.taxes_fees,
    b.collected_amount,
    {{ safe_divide('b.recurring_revenue', 'b.subscribers') }}          as arpu,
    {{ safe_divide('b.collected_amount', 'b.billed_amount') }}         as collection_rate,
    b.late_invoices,
    b.written_off_invoices,
    b.disputes
from rev as b
left join churned as ch
    on  ch.revenue_month = b.revenue_month
    and ch.state = b.state
    and ch.technology = b.technology
    and ch.segment = b.segment
