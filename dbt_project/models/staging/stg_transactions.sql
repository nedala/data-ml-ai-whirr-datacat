-- Account ledger. Sign convention: payments and credits are negative
-- (reduce the balance), late fees positive.
select
    cast(txn_id as {{ dbt.type_string() }})           as txn_id,
    cast(ban as {{ dbt.type_string() }})              as ban,
    cast(invoice_id as {{ dbt.type_string() }})       as invoice_id,
    cast(txn_date as date)       as txn_date,
    {{ month_start('txn_date') }} as txn_month,
    cast(txn_type as {{ dbt.type_string() }})         as txn_type,
    cast(amount as {{ dbt.type_float() }})           as amount,
    cast(payment_method as {{ dbt.type_string() }})   as payment_method,
    cast(reason as {{ dbt.type_string() }})           as reason
from {{ source('raw', 'transactions') }}
