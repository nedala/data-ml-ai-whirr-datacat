-- One row per ledger transaction (payment, late fee, credit, …) with the
-- invoice's due date and lateness. Payments / credits are negative amounts;
-- `abs_amount` is the unsigned value for charts.
select
    t.txn_id,
    t.ban,
    t.invoice_id,
    t.txn_date,
    t.txn_month,
    t.txn_type,
    t.amount,
    abs(t.amount)                                        as abs_amount,
    t.payment_method,
    t.reason,
    i.bill_month,
    i.due_date,
    case when t.txn_type = 'payment'
         then {{ days_between('i.due_date', 't.txn_date') }} end as days_after_due,
    t.txn_type = 'payment' and t.txn_date > i.due_date   as is_paid_late,
    c.state,
    c.region,
    c.technology,
    c.segment,
    c.clli
from {{ ref('stg_transactions') }} as t
left join {{ ref('stg_invoices') }} as i on i.invoice_id = t.invoice_id
left join {{ ref('int_customer_current_service') }} as c on c.ban = t.ban
