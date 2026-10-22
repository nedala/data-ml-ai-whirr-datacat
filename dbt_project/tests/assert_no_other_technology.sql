-- DataPlatform sells copper and fiber only: no other technology anywhere.
select 'plans' as src, technology from {{ ref('stg_plans') }} where technology not in ('copper', 'fiber')
union all
select 'locations', technology from {{ ref('stg_service_locations') }} where technology not in ('copper', 'fiber')
union all
select 'revenue', technology from {{ ref('stg_revenue') }} where technology not in ('copper', 'fiber')
