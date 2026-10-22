-- A wire center can never have more repeat dispatches than dispatches.
select clli, report_month, dispatches, repeat_dispatches
from {{ ref('mart_network_reliability') }}
where repeat_dispatches > dispatches
