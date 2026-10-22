-- Network reliability, servicing and plant quality by CLLI wire center ×
-- month: outages, tickets, dispatches / repeat dispatches, first-contact
-- resolution, fiber ONT rx power & LOS, copper DSL SNR & retrains.
with subs as (
    select clli, revenue_month as report_month,
           count(distinct ban)                     as subscribers,
           sum({{ flag("technology = 'fiber'") }})  as fiber_subscribers,
           sum({{ flag("technology = 'copper'") }}) as copper_subscribers
    from {{ ref('stg_revenue') }}
    group by clli, revenue_month
),

usage_m as (
    select l.clli, u.usage_month as report_month,
           sum(u.outage_minutes)  as customer_outage_minutes,
           avg(u.avg_latency_ms)  as avg_latency_ms
    from {{ ref('stg_usage_monthly') }} as u
    join {{ ref('stg_service_locations') }} as l on l.ban = u.ban
    group by l.clli, u.usage_month
),

tix as (
    select clli, opened_month as report_month,
           count(*)                                          as tickets,
           sum({{ flag("category = 'technical'") }})         as technical_tickets,
           sum({{ flag('is_first_contact_resolved') }})      as first_contact_resolved,
           sum({{ flag('is_repeat_30d') }})                  as repeat_tickets,
           avg(resolution_hours)                             as avg_resolution_hours
    from {{ ref('int_tickets_enriched') }}
    group by clli, opened_month
),

disp as (
    select clli, dispatch_month as report_month,
           count(*)                                          as dispatches,
           sum({{ flag("dispatch_type = 'repair'") }})       as repair_dispatches,
           sum({{ flag('is_repeat_dispatch_30d') }})         as repeat_dispatches,
           sum({{ flag("cause = 'no_trouble_found'") }})     as no_trouble_found
    from {{ ref('stg_dispatches') }}
    group by clli, dispatch_month
),

plant as (
    select l.clli, q.metric_month as report_month,
           avg(q.rx_power_dbm)                               as avg_ont_rx_power_dbm,
           min(q.rx_power_dbm)                               as min_ont_rx_power_dbm,
           sum(q.los_events)                                 as ont_los_events,
           sum({{ flag('q.is_weak_optics') }})               as weak_optics_onts,
           sum({{ flag("q.technology = 'fiber'") }})         as onts_reporting,
           avg(q.snr_margin_db)                              as avg_dsl_snr_margin_db,
           sum(q.retrains)                                   as dsl_retrains,
           sum({{ flag('q.is_marginal_line') }})             as marginal_dsl_lines,
           sum({{ flag("q.technology = 'copper'") }})        as dsl_lines_reporting
    from {{ ref('int_plant_quality_monthly') }} as q
    join {{ ref('stg_service_locations') }} as l on l.ban = q.ban
    group by l.clli, q.metric_month
)

select
    s.clli,
    w.wire_center_name,
    s.report_month,
    w.state,
    r.region,
    s.subscribers,
    s.fiber_subscribers,
    s.copper_subscribers,
    {{ safe_divide('s.fiber_subscribers', 's.subscribers') }}            as fiber_share,
    coalesce(e.events, 0)                   as network_events,
    coalesce(e.outage_events, 0)            as outage_events,
    coalesce(e.outage_minutes, 0)           as outage_minutes,
    coalesce(e.degradation_minutes, 0)      as degradation_minutes,
    coalesce(e.maintenance_minutes, 0)      as maintenance_minutes,
    e.longest_unplanned_minutes,
    coalesce(u.customer_outage_minutes, 0)  as customer_outage_minutes,
    u.avg_latency_ms,
    coalesce(t.tickets, 0)                  as tickets,
    coalesce(t.technical_tickets, 0)        as technical_tickets,
    coalesce(t.first_contact_resolved, 0)   as first_contact_resolved,
    coalesce(t.repeat_tickets, 0)           as repeat_tickets,
    t.avg_resolution_hours,
    coalesce(d.dispatches, 0)               as dispatches,
    coalesce(d.dispatches, 0)               as truck_rolls,
    coalesce(d.repair_dispatches, 0)        as repair_dispatches,
    coalesce(d.repeat_dispatches, 0)        as repeat_dispatches,
    coalesce(d.no_trouble_found, 0)         as no_trouble_found,
    {{ safe_divide('d.repeat_dispatches', 'd.dispatches') }}             as repeat_dispatch_rate,
    p.avg_ont_rx_power_dbm,
    p.min_ont_rx_power_dbm,
    coalesce(p.ont_los_events, 0)           as ont_los_events,
    coalesce(p.weak_optics_onts, 0)         as weak_optics_onts,
    {{ safe_divide('p.weak_optics_onts', 'p.onts_reporting') }}          as weak_optics_share,
    p.avg_dsl_snr_margin_db,
    coalesce(p.dsl_retrains, 0)             as dsl_retrains,
    coalesce(p.marginal_dsl_lines, 0)       as marginal_dsl_lines,
    {{ safe_divide('p.marginal_dsl_lines', 'p.dsl_lines_reporting') }}   as marginal_line_share,
    {{ safe_divide('t.first_contact_resolved', 't.tickets') }}           as fcr_rate,
    {{ safe_divide('1000 * coalesce(t.tickets, 0)', 's.subscribers') }}  as tickets_per_1k_subs,
    {{ safe_divide('1000 * coalesce(d.dispatches, 0)', 's.subscribers') }} as dispatches_per_1k_subs,
    {{ safe_divide('1000 * coalesce(d.dispatches, 0)', 's.subscribers') }} as truck_rolls_per_1k_subs,
    {{ safe_divide('coalesce(u.customer_outage_minutes, 0)', 's.subscribers') }} as outage_minutes_per_sub
from subs as s
left join {{ ref('stg_wire_centers') }} as w on w.clli = s.clli
left join {{ ref('state_region') }} as r on r.state = w.state
left join {{ ref('int_wire_center_monthly_events') }} as e
    on e.clli = s.clli and e.event_month = s.report_month
left join usage_m as u on u.clli = s.clli and u.report_month = s.report_month
left join tix as t on t.clli = s.clli and t.report_month = s.report_month
left join disp as d on d.clli = s.clli and d.report_month = s.report_month
left join plant as p on p.clli = s.clli and p.report_month = s.report_month
