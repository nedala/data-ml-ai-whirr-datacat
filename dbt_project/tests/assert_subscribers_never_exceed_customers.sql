-- A month can never bill more distinct subscribers than there are accounts.
with m as (
    select revenue_month, sum(subscribers) as subs
    from {{ ref('fct_revenue_monthly') }}
    group by revenue_month
)
select m.*
from m
where m.subs > (select count(*) from {{ ref('dim_customer') }})
