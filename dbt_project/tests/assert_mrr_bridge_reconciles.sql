-- Σ bridge movements in a month must equal the change in total MRR.
with mrr as (
    select bill_month, sum(service_revenue) as mrr from {{ ref('fct_invoice') }} group by bill_month
),
delta as (
    select bill_month, mrr - lag(mrr) over (order by bill_month) as mrr_delta from mrr
),
bridge as (
    select movement_month, sum(mrr_change) as moved from {{ ref('mart_mrr_bridge') }} group by movement_month
)
select d.bill_month, d.mrr_delta, b.moved
from delta as d
left join bridge as b on b.movement_month = d.bill_month
where d.mrr_delta is not null
  and abs(d.mrr_delta - coalesce(b.moved, 0)) > 0.01
