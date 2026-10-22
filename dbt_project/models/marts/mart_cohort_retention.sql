-- Retention by signup-year cohort: share of each cohort's accounts billed in
-- the first month of the window that are still billed N months later.
with billed as (
    select i.ban, i.bill_month, c.signup_date
    from {{ ref('fct_invoice') }} as i
    join {{ ref('dim_customer') }} as c on c.ban = i.ban
),

bounds as (select min(bill_month) as first_month from billed),

base as (
    select
        b.ban,
        cast(extract(year from b.signup_date) as {{ dbt.type_int() }}) as signup_year
    from billed as b
    join bounds on b.bill_month = bounds.first_month
),

cohort as (
    select signup_year, count(*) as cohort_size from base group by signup_year
)

select
    cast(base.signup_year as {{ dbt.type_string() }})                        as signup_cohort,
    cohort.cohort_size,
    b.bill_month                                                             as report_month,
    {{ dbt.datediff('bounds.first_month', 'b.bill_month', 'month') }}        as months_since_start,
    count(distinct b.ban)                                             as retained_accounts,
    {{ safe_divide('count(distinct b.ban)', 'cohort.cohort_size') }}  as retention_rate
from billed as b
cross join bounds
join base on base.ban = b.ban
join cohort on cohort.signup_year = base.signup_year
group by base.signup_year, cohort.cohort_size, b.bill_month, bounds.first_month
