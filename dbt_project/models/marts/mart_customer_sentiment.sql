-- Survey-level NPS with verbatim theme and customer context. Aggregate in
-- the BI tool: NPS = 100 * (sum(is_promoter) - sum(is_detractor)) / count(*).
with s as (
    select
        n.*,
        case
            when {{ ilike_any('n.verbatim', ['outage', 'rock solid']) }}               then 'Reliability'
            when {{ ilike_any('n.verbatim', ['when is fiber', 'dsl is too slow']) }}   then 'Fiber availability'
            when {{ ilike_any('n.verbatim', ['late fee', 'billing', 'charged']) }}     then 'Billing'
            when {{ ilike_any('n.verbatim', ['price', 'bill went up', 'promo']) }}     then 'Price & promotions'
            when {{ ilike_any('n.verbatim', ['technician', 'install']) }}              then 'Install & field service'
            when {{ ilike_any('n.verbatim', ['support', 'first call']) }}              then 'Customer support'
            when {{ ilike_any('n.verbatim', ['wifi', 'wi-fi']) }}                      then 'Wi-Fi coverage'
            when {{ ilike_any('n.verbatim', ['speed', 'gig']) }}                       then 'Speed'
            else 'General'
        end as verbatim_theme
    from {{ ref('stg_nps_surveys') }} as n
),

prior_tickets as (
    select s.survey_id, count(t.ticket_id) as tickets_prior_90d
    from s
    left join {{ ref('stg_service_tickets') }} as t
        on  t.ban = s.ban
        and t.opened_date <= s.survey_date
        and t.opened_date > cast({{ dbt.dateadd('day', -90, 's.survey_date') }} as date)
    group by s.survey_id
),

prior_calls as (
    select s.survey_id, count(k.call_id) as calls_prior_90d, avg(k.sentiment_score) as call_sentiment_prior_90d
    from s
    left join {{ ref('stg_customer_calls') }} as k
        on  k.ban = s.ban
        and k.call_date <= s.survey_date
        and k.call_date > cast({{ dbt.dateadd('day', -90, 's.survey_date') }} as date)
    group by s.survey_id
)

select
    s.survey_id,
    s.ban,
    s.survey_date,
    s.survey_month,
    s.nps_score,
    s.nps_band,
    {{ flag("s.nps_band = 'promoter'") }}  as is_promoter,
    {{ flag("s.nps_band = 'passive'") }}   as is_passive,
    {{ flag("s.nps_band = 'detractor'") }} as is_detractor,
    s.sentiment_label,
    s.verbatim,
    s.verbatim_theme,
    c.technology,
    c.plan_id,
    c.speed_tier,
    c.segment,
    c.state,
    c.region,
    c.clli,
    coalesce(p.tickets_prior_90d, 0)       as tickets_prior_90d,
    coalesce(pc.calls_prior_90d, 0)        as calls_prior_90d,
    pc.call_sentiment_prior_90d,
    c.subscription_status,
    c.churn_month,
    c.churn_month is not null and c.churn_month >= s.survey_month as churned_after_survey
from s
left join prior_tickets as p on p.survey_id = s.survey_id
left join prior_calls as pc on pc.survey_id = s.survey_id
left join {{ ref('int_customer_current_service') }} as c on c.ban = s.ban
