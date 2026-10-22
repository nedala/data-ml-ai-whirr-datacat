-- Compile with `dbt compile --select churn_rate_by_technology` and run the
-- SQL in SQL Lab / a notebook: monthly churn rate by access technology.
select
    revenue_month,
    technology,
    sum(subscribers)                                            as subscribers,
    sum(churned_subscribers)                                    as churned,
    {{ safe_divide('sum(churned_subscribers)', 'sum(subscribers)') }} as churn_rate
from {{ ref('fct_revenue_monthly') }}
group by revenue_month, technology
order by revenue_month, technology
