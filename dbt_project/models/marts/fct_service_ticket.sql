-- One row per care / repair ticket.
select
    t.ticket_id,
    t.ban,
    t.opened_at,
    t.closed_at,
    t.opened_date,
    t.opened_month,
    t.category,
    t.subcategory,
    t.channel,
    t.is_truck_roll,
    t.dispatches,
    t.is_repeat_dispatch,
    t.is_first_contact_resolved,
    t.is_repeat_30d,
    t.resolution_hours,
    t.clli,
    t.technology,
    t.state,
    r.region,
    t.segment
from {{ ref('int_tickets_enriched') }} as t
left join {{ ref('state_region') }} as r on r.state = t.state
