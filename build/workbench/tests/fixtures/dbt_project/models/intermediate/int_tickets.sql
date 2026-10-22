select
    ticket_id,
    ban,
    region,
    minutes_open,
    is_outage
from {{ ref('stg_service_tickets') }}
