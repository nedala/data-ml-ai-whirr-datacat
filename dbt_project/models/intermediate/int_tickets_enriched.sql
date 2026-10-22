-- Tickets with the account's serving CLLI / technology, the truck roll
-- (dispatch) it triggered and a repeat-contact flag (another ticket by the
-- same account within var('repeat_ticket_days') before this one).
with t as (
    select
        tk.*,
        lag(tk.opened_at) over (partition by tk.ban order by tk.opened_at, tk.ticket_id) as prev_opened_at
    from {{ ref('stg_service_tickets') }} as tk
),

d as (
    select ticket_id, count(*) as dispatches, max({{ flag('is_repeat_dispatch_30d') }}) as repeat_dispatch
    from {{ ref('stg_dispatches') }}
    where ticket_id is not null
    group by ticket_id
)

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
    coalesce(d.dispatches, 0) > 0                    as is_truck_roll,
    coalesce(d.dispatches, 0)                        as dispatches,
    coalesce(d.repeat_dispatch, 0) = 1               as is_repeat_dispatch,
    t.is_first_contact_resolved,
    t.resolution_hours,
    coalesce(t.prev_opened_at is not null
             and {{ hours_between('t.prev_opened_at', 't.opened_at') }} <= {{ var('repeat_ticket_days') }} * 24,
             false) as is_repeat_30d,
    l.clli,
    l.technology,
    c.state,
    c.segment
from t
left join d on d.ticket_id = t.ticket_id
left join {{ ref('stg_service_locations') }} as l on l.ban = t.ban
left join {{ ref('stg_customers') }} as c on c.ban = t.ban
