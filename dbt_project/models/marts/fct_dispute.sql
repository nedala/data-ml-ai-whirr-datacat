-- One row per billing dispute with outcome, credit and account dimensions.
select
    d.dispute_id,
    d.ban,
    d.invoice_id,
    i.bill_month,
    d.opened_date,
    d.opened_month,
    d.channel,
    d.category,
    d.disputed_amount,
    d.dispute_status,
    d.resolved_date,
    d.days_to_resolve,
    d.credit_amount,
    d.dispute_status in ('approved', 'partial')          as is_credited,
    d.dispute_status = 'open'                            as is_open,
    c.technology,
    c.plan_id,
    c.segment,
    c.state,
    c.region,
    c.clli,
    c.is_autopay
from {{ ref('stg_disputes') }} as d
left join {{ ref('stg_invoices') }} as i on i.invoice_id = d.invoice_id
left join {{ ref('int_customer_current_service') }} as c on c.ban = d.ban
