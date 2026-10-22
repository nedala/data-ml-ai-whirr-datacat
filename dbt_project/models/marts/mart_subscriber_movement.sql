-- Monthly subscriber movement by technology: billed base, gross adds (first
-- bill after the window start), disconnects, net adds and MRR lost to churn.
-- (The synthetic book has no new connects inside the window, so gross adds
-- are 0 there — the model is ready for a live feed.)
with acct as (
    select
        ban,
        technology,
        min(bill_month) as first_month,
        max(bill_month) as last_month
    from {{ ref('fct_invoice') }}
    group by ban, technology
),

bounds as (select min(bill_month) as min_month, max(bill_month) as max_month from {{ ref('fct_invoice') }}),

last_rev as (
    select i.ban, i.service_revenue as last_mrr
    from {{ ref('fct_invoice') }} as i
    join acct on acct.ban = i.ban and acct.last_month = i.bill_month
),

months as (
    select bill_month as report_month, technology, count(distinct ban) as subscribers,
           sum(service_revenue) as mrr
    from {{ ref('fct_invoice') }}
    group by bill_month, technology
),

adds as (
    select a.first_month as report_month, a.technology, count(*) as gross_adds
    from acct as a cross join bounds
    where a.first_month > bounds.min_month
    group by a.first_month, a.technology
),

disc as (
    select c.churn_month as report_month, c.technology, count(*) as disconnects,
           sum(coalesce(l.last_mrr, 0)) as mrr_churned
    from {{ ref('dim_customer') }} as c
    left join last_rev as l on l.ban = c.ban
    where c.churn_month is not null
    group by c.churn_month, c.technology
)

select
    m.report_month,
    m.technology,
    m.subscribers,
    m.mrr,
    coalesce(a.gross_adds, 0)                                  as gross_adds,
    coalesce(d.disconnects, 0)                                 as disconnects,
    coalesce(a.gross_adds, 0) - coalesce(d.disconnects, 0)     as net_adds,
    coalesce(d.mrr_churned, 0)                                 as mrr_churned
from months as m
left join adds as a on a.report_month = m.report_month and a.technology = m.technology
left join disc as d on d.report_month = m.report_month and d.technology = m.technology
