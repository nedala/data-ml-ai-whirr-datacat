-- fct_revenue_monthly must carry exactly the billed amount and the recurring
-- revenue of fct_invoice (raw.revenue and raw.invoices cover the same bills).
with a as (select sum(billed_amount) as billed, sum(recurring_revenue) as mrr from {{ ref('fct_revenue_monthly') }}),
     b as (select sum(amount_due) as billed, sum(service_revenue) as mrr from {{ ref('fct_invoice') }})
select a.billed as revenue_monthly_billed, b.billed as invoices_billed, a.mrr as revenue_mrr, b.mrr as invoice_mrr
from a cross join b
where abs(coalesce(a.billed, 0) - coalesce(b.billed, 0)) > 0.01
   or abs(coalesce(a.mrr, 0) - coalesce(b.mrr, 0)) > 0.01
