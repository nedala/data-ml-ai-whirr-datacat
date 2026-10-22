-- Billing disputes (raw.disputes) with outcome, invoice, account context and
-- the billing tickets / calls around them, plus whether the account churned.
with tix as (
    select d.dispute_id, count(t.ticket_id) as billing_tickets_30d
    from {{ ref('stg_disputes') }} as d
    left join {{ ref('stg_service_tickets') }} as t
        on  t.ban = d.ban
        and t.category = 'billing'
        and t.opened_date >= cast({{ dbt.dateadd('day', -30, 'd.opened_date') }} as date)
        and t.opened_date <= cast({{ dbt.dateadd('day', 30, 'd.opened_date') }} as date)
    group by d.dispute_id
),

calls as (
    select d.dispute_id, count(k.call_id) as billing_calls_30d
    from {{ ref('stg_disputes') }} as d
    left join {{ ref('stg_customer_calls') }} as k
        on  k.ban = d.ban
        and k.queue = 'billing'
        and k.call_date >= cast({{ dbt.dateadd('day', -30, 'd.opened_date') }} as date)
        and k.call_date <= cast({{ dbt.dateadd('day', 30, 'd.opened_date') }} as date)
    group by d.dispute_id
)

select
    d.dispute_id,
    d.ban,
    d.invoice_id,
    i.bill_month,
    i.invoice_date,
    d.opened_date,
    d.opened_month,
    d.channel,
    d.category,
    d.disputed_amount,
    d.dispute_status,
    d.resolved_date,
    d.days_to_resolve,
    d.credit_amount,
    d.dispute_status in ('approved', 'partial')   as is_credited,
    {{ safe_divide('d.credit_amount', 'd.disputed_amount') }} as credit_ratio,
    i.amount_due                                  as invoice_amount_due,
    i.late_fee_amount                             as invoice_late_fee,
    i.invoice_status,
    c.is_autopay,
    c.technology,
    c.plan_id,
    c.segment,
    c.state,
    c.region,
    c.clli,
    coalesce(tix.billing_tickets_30d, 0)           as billing_tickets_30d,
    coalesce(calls.billing_calls_30d, 0)             as billing_calls_30d,
    c.subscription_status,
    c.cancel_reason,
    c.churn_month is not null and c.churn_month >= d.opened_month as churned_after_dispute
from {{ ref('stg_disputes') }} as d
left join {{ ref('stg_invoices') }} as i on i.invoice_id = d.invoice_id
left join {{ ref('int_customer_current_service') }} as c on c.ban = d.ban
left join tix on tix.dispute_id = d.dispute_id
left join calls on calls.dispute_id = d.dispute_id
