{#- Generic tests used in the schema files. -#}

{% test non_negative(model, column_name) %}
select {{ column_name }} as offending_value
from {{ model }}
where {{ column_name }} < 0
{% endtest %}

{% test between(model, column_name, min_value, max_value) %}
select {{ column_name }} as offending_value
from {{ model }}
where {{ column_name }} < {{ min_value }} or {{ column_name }} > {{ max_value }}
{% endtest %}

{#- one row per combination of the given columns -#}
{% test unique_combination(model, columns) %}
select {{ columns | join(', ') }}, count(*) as n
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
