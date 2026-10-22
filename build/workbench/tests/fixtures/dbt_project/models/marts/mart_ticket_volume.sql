select region, count(*) as tickets from {{ ref('int_tickets') }} group by 1
