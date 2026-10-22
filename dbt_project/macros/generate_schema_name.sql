{#- Layer names verbatim (staging / intermediate / marts / reference /
    snapshots) instead of dbt's "<target_schema>_<custom>" concatenation, so
    Postgres schemas and BigQuery datasets carry the same names.

    DBT_SCHEMA_PREFIX prefixes every layer (e.g. "dev_seshu_" for a personal
    sandbox). On the trino target the default prefix is "trino_": the
    `warehouse` catalog IS the local Postgres, so building through Trino must
    not overwrite the tables the postgres target owns. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set default_prefix = 'trino_' if target.type == 'trino' and target.database == 'warehouse' else '' -%}
    {%- set prefix = env_var('DBT_SCHEMA_PREFIX', default_prefix) -%}
    {%- if custom_schema_name is none -%}{{ prefix ~ (target.schema | trim) }}
    {%- else -%}{{ prefix ~ (custom_schema_name | trim) }}{%- endif -%}
{%- endmacro %}
