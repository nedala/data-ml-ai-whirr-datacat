-- One row per field dispatch (truck roll) with cause, outcome, the
-- 30-day repeat flag, and the account's plant quality in the dispatch month.
select
    d.dispatch_id,
    d.ban,
    d.ticket_id,
    d.clli,
    w.wire_center_name,
    d.dispatch_type,
    d.scheduled_at,
    d.scheduled_date,
    d.dispatch_month,
    d.completed_at,
    d.dispatch_status,
    d.dispatch_status = 'completed'                  as is_completed,
    d.onsite_hours,
    d.tech_id,
    d.cause,
    d.cause = 'no_trouble_found'                     as is_no_trouble_found,
    d.is_repeat_dispatch_30d                         as repeat_dispatch_30d,
    t.category                                       as ticket_category,
    t.subcategory                                    as ticket_subcategory,
    c.technology,
    c.plan_id,
    c.segment,
    c.state,
    c.region,
    q.rx_power_dbm,
    q.los_events,
    q.snr_margin_db,
    q.retrains,
    q.is_bad_line
from {{ ref('stg_dispatches') }} as d
left join {{ ref('stg_wire_centers') }} as w on w.clli = d.clli
left join {{ ref('stg_service_tickets') }} as t on t.ticket_id = d.ticket_id
left join {{ ref('int_customer_current_service') }} as c on c.ban = d.ban
left join {{ ref('int_plant_quality_monthly') }} as q
    on q.ban = d.ban and q.metric_month = d.dispatch_month
