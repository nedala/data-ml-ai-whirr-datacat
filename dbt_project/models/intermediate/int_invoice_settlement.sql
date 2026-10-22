-- Invoice ⨝ ledger (transactions) ⨝ disputes: how and when each bill was
-- settled. Ledger payments and credits are negative amounts.
with pay as (
    select
        invoice_id,
        min(txn_date)        as paid_date,
        -1 * sum(amount)     as paid_amount,
        min(payment_method)  as payment_method,
        count(*)             as payment_count
    from {{ ref('stg_transactions') }}
    where txn_type = 'payment'
    group by invoice_id
),

ledger as (
    select
        invoice_id,
        sum(case when txn_type = 'late_fee' then amount else 0 end)   as ledger_late_fees,
        -1 * sum(case when txn_type = 'credit' then amount else 0 end) as ledger_credits,
        count(*)                                                       as txn_count
    from {{ ref('stg_transactions') }}
    group by invoice_id
),

dsp as (
    select
        invoice_id,
        count(*)            as dispute_count,
        sum(disputed_amount) as disputed_amount
    from {{ ref('stg_disputes') }}
    group by invoice_id
)

select
    i.invoice_id,
    i.ban,
    i.bill_cycle,
    i.bill_month,
    i.invoice_date,
    i.due_date,
    i.recurring_charges,
    i.one_time_charges,
    i.taxes_fees,
    i.credit_amount,
    i.late_fee_amount,
    i.amount_due,
    i.service_revenue,
    i.invoice_status,
    pay.paid_date,
    coalesce(pay.paid_amount, 0)                       as paid_amount,
    pay.payment_method,
    case when pay.paid_date is not null
         then {{ days_between('i.invoice_date', 'pay.paid_date') }} end as days_to_pay,
    case when pay.paid_date is not null
         then {{ days_between('i.due_date', 'pay.paid_date') }} end     as days_past_due,
    i.invoice_status in ('late', 'written_off', 'open') or i.late_fee_amount > 0 as is_late,
    coalesce(ledger.txn_count, 0)                      as txn_count,
    coalesce(ledger.ledger_credits, 0)                 as ledger_credits,
    coalesce(dsp.dispute_count, 0)                     as dispute_count,
    coalesce(dsp.disputed_amount, 0)                   as disputed_amount,
    i.amount_due - coalesce(pay.paid_amount, 0)        as open_balance
from {{ ref('stg_invoices') }} as i
left join pay on pay.invoice_id = i.invoice_id
left join ledger on ledger.invoice_id = i.invoice_id
left join dsp on dsp.invoice_id = i.invoice_id
