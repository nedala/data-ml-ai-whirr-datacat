-- Network events rolled up to CLLI wire center × month.
select
    clli,
    event_month,
    count(*)                                                                    as events,
    sum({{ flag("event_type = 'outage'") }})                                    as outage_events,
    sum({{ flag("event_type = 'degradation'") }})                               as degradation_events,
    sum({{ flag("event_type = 'maintenance'") }})                               as maintenance_events,
    sum(case when event_type = 'outage' then duration_minutes else 0 end)       as outage_minutes,
    sum(case when event_type = 'degradation' then duration_minutes else 0 end)  as degradation_minutes,
    sum(case when event_type = 'maintenance' then duration_minutes else 0 end)  as maintenance_minutes,
    max(case when is_unplanned then duration_minutes end)                       as longest_unplanned_minutes
from {{ ref('stg_network_events') }}
group by clli, event_month
