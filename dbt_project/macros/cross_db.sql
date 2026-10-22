{#- Cross-database helpers (Postgres · BigQuery · Trino) via adapter.dispatch.
    Prefer dbt's built-in cross-db macros (dbt.date_trunc, dbt.datediff,
    dbt.dateadd, dbt.type_*) and add here only what they do not cover. -#}

{#- numerator / denominator, NULL when the denominator is 0 or NULL, always
    floating point (Postgres and Trino would otherwise do integer division). -#}
{% macro safe_divide(numerator, denominator) -%}
    {{ return(adapter.dispatch('safe_divide', 'bsp_edw')(numerator, denominator)) }}
{%- endmacro %}

{% macro default__safe_divide(numerator, denominator) -%}
    (cast({{ numerator }} as {{ dbt.type_float() }}) / nullif(cast({{ denominator }} as {{ dbt.type_float() }}), 0))
{%- endmacro %}

{% macro bigquery__safe_divide(numerator, denominator) -%}
    safe_divide(cast({{ numerator }} as float64), cast({{ denominator }} as float64))
{%- endmacro %}


{#- First day of the month as a DATE. -#}
{% macro month_start(expr) -%}
    {{ return(adapter.dispatch('month_start', 'bsp_edw')(expr)) }}
{%- endmacro %}

{% macro default__month_start(expr) -%}
    cast({{ dbt.date_trunc('month', expr) }} as date)
{%- endmacro %}

{% macro bigquery__month_start(expr) -%}
    date_trunc(cast({{ expr }} as date), month)
{%- endmacro %}


{#- Elapsed hours between two timestamps, fractional. -#}
{% macro hours_between(start_ts, end_ts) -%}
    {{ return(adapter.dispatch('hours_between', 'bsp_edw')(start_ts, end_ts)) }}
{%- endmacro %}

{% macro default__hours_between(start_ts, end_ts) -%}
    (extract(epoch from ({{ end_ts }} - {{ start_ts }})) / 3600.0)
{%- endmacro %}

{% macro bigquery__hours_between(start_ts, end_ts) -%}
    (timestamp_diff(cast({{ end_ts }} as timestamp), cast({{ start_ts }} as timestamp), second) / 3600.0)
{%- endmacro %}

{% macro trino__hours_between(start_ts, end_ts) -%}
    (cast(date_diff('second', {{ start_ts }}, {{ end_ts }}) as double) / 3600.0)
{%- endmacro %}


{#- Whole days from start_date to end_date (dates). -#}
{% macro days_between(start_date, end_date) -%}
    {{ dbt.datediff(start_date, end_date, 'day') }}
{%- endmacro %}


{#- Add N months to a DATE and return a DATE. -#}
{% macro add_months(expr, n) -%}
    cast({{ dbt.dateadd('month', n, expr) }} as date)
{%- endmacro %}


{#- 1/0 from a boolean expression — portable SUM()/AVG() of flags. -#}
{% macro flag(expr) -%}
    case when {{ expr }} then 1 else 0 end
{%- endmacro %}


{#- A date literal. -#}
{% macro date_literal(iso) -%}
    {{ return(adapter.dispatch('date_literal', 'bsp_edw')(iso)) }}
{%- endmacro %}

{% macro default__date_literal(iso) -%}
    cast('{{ iso }}' as date)
{%- endmacro %}

{% macro trino__date_literal(iso) -%}
    date '{{ iso }}'
{%- endmacro %}


{#- Case-insensitive "contains" for keyword theming. -#}
{% macro ilike_any(expr, words) -%}
    ({%- for w in words %}lower({{ expr }}) like '%{{ w | lower }}%'{% if not loop.last %} or {% endif %}{% endfor -%})
{%- endmacro %}
