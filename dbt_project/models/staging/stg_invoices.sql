-- amount_due = recurring + one-time + taxes & fees + late fee - credits.
select
    cast(invoice_id as {{ dbt.type_string() }})           as invoice_id,
    cast(ban as {{ dbt.type_string() }})                  as ban,
    cast(bill_cycle as {{ dbt.type_string() }})           as bill_cycle,
    {{ month_start('invoice_date') }} as bill_month,
    cast(invoice_date as date)       as invoice_date,
    cast(due_date as date)           as due_date,
    cast(recurring_charges as {{ dbt.type_float() }})    as recurring_charges,
    cast(one_time_charges as {{ dbt.type_float() }})     as one_time_charges,
    cast(taxes_fees as {{ dbt.type_float() }})           as taxes_fees,
    cast(credits as {{ dbt.type_float() }})              as credit_amount,
    cast(late_fee as {{ dbt.type_float() }})             as late_fee_amount,
    cast(amount_due as {{ dbt.type_float() }})           as amount_due,
    -- recurring service revenue (MRR) = recurring charges net of promo discount
    cast(recurring_charges as {{ dbt.type_float() }})    as service_revenue,
    cast(status as {{ dbt.type_string() }})               as invoice_status
from {{ source('raw', 'invoices') }}
