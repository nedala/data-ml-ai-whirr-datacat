with src as (
    select
        cast(survey_id as {{ dbt.type_string() }})        as survey_id,
        cast(ban as {{ dbt.type_string() }})              as ban,
        cast(survey_date as date)    as survey_date,
        cast(nps_score as {{ dbt.type_int() }})        as nps_score,
        cast(verbatim as {{ dbt.type_string() }})         as verbatim,
        cast(sentiment_label as {{ dbt.type_string() }})  as sentiment_label
    from {{ source('raw', 'nps_surveys') }}
)
select
    src.*,
    {{ month_start('survey_date') }} as survey_month,
    case when nps_score >= 9 then 'promoter'
         when nps_score >= 7 then 'passive'
         else 'detractor' end      as nps_band
from src
