-- Fiber subscribers must sit at fiber-passed locations.
select clli, fiber_subscribers, fiber_passed_active
from {{ ref('mart_fiber_penetration') }}
where fiber_subscribers > fiber_passed_active
