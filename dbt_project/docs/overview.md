{% docs __overview__ %}
# DataPlatform EDW (`bsp_edw`)

Analytics models for a US broadband provider selling **copper (DSL)** and
**fiber (FTTH)** internet only — covering **billing, ledger & disputes**,
**revenue**, **servicing** (tickets, first-contact resolution), **field ops**
(dispatches, repeat dispatches), **plant quality** (fiber ONT optics, copper
DSL lines), **network reliability** (outages by CLLI wire center), the
**contact center** (calls, handle time, sentiment), **customer sentiment**
(relationship NPS) and **churn**. Account-level tables are keyed by `ban`;
wire centers by `clli`.

| layer | schema / dataset | materialisation | purpose |
|---|---|---|---|
| sources | `raw` | — | landed by `python -m bsp_seed` (synthetic, deterministic) |
| seeds | `reference` | table | plan catalogue, state → region |
| staging | `staging` | view | `stg_*`: renamed, typed, light derivations |
| intermediate | `intermediate` | view | `int_*`: joined business entities |
| marts | `marts` | table | `dim_*`, `fct_*`, `mart_*` for Superset, Power BI, notebooks, ML |
| snapshots | `snapshots` | SCD2 | `snap_subscriptions` — plan / status history |

Targets: `postgres` (default, local warehouse), `bigquery` (enterprise EDW,
project `GCP_PROJECT_ID`), `trino` (federated; writes `trino_*` schemas in
the `warehouse` catalog).

The data is synthetic; PII-shaped columns (names, e-mail, phone, address
keys) exist only in `stg_customers` / `dim_customer` and are tagged `pii`.
{% enddocs %}

{% docs ban %}
Billing account number (BAN), a 10-digit string (e.g. `3100000008`). The
conformed key across billing, ledger, disputes, servicing, dispatches, calls,
surveys, churn features, dispute e-mails and CCAI call logs.
{% enddocs %}

{% docs invoice_id %}
Invoice identifier `<ban>-<yymm>` (e.g. `3100000008-2609`) — one invoice per
account per bill month.
{% enddocs %}

{% docs plan_id %}
Rate-card plan: copper `CU-25`, `CU-80`, `CU-140`; fiber `FB-300`, `FB-500`,
`FB-1G`, `FB-2G`.
{% enddocs %}

{% docs technology %}
Access technology of the service location: `copper` (DSL) or `fiber` (FTTH).
DataPlatform sells no other technology.
{% enddocs %}

{% docs clli %}
CLLI code of the serving wire center (central office), 8 characters: 4-letter
place + 2-letter state + 2-character building code (e.g. `RLGHNCXA`). Network
events, dispatches and plant inventory are recorded per CLLI; it is the unit
for reliability and fiber build analysis.
{% enddocs %}

{% docs fiber_passings %}
Homes and businesses passed by fiber in the wire center (serviceable fiber
locations), whether or not they subscribe.
{% enddocs %}

{% docs ont_rx_power %}
Fiber ONT optical receive power in dBm (higher is better; typical −8 to −27).
Below −25 dBm is "weak optics" (dirty connector, bend, damaged drop) and
drives LOS events, tickets and repeat dispatches.
{% enddocs %}

{% docs dsl_snr_margin %}
Copper DSL downstream SNR margin in dB. Below 6 dB the line is marginal —
retrains, CRC errors and speed loss; long loops and corroded drops are the
usual causes.
{% enddocs %}

{% docs repeat_dispatch %}
Repeat dispatch: a field dispatch (truck roll) to the same BAN within 30 days
of the previous one — the field-quality KPI (first fix did not hold).
{% enddocs %}

{% docs repeat_call %}
Repeat call: the account called the contact center again within 7 days.
{% enddocs %}

{% docs churned_next_90d %}
Churn label: the account cancels within the 90 days (three bill months)
starting at the snapshot month. Only trust it where `is_label_complete` is
true — the last two months are not fully observed yet.
{% enddocs %}

{% docs arpu %}
Average revenue per user: recurring service revenue (recurring charges net of
promo discount) divided by billed subscribers in the period.
{% enddocs %}

{% docs churn_rate %}
Monthly churn rate: accounts whose subscription ended in the month divided by
accounts billed in the month.
{% enddocs %}

{% docs fiber_take_rate %}
Fiber penetration (take rate): active fiber subscribers ÷ active accounts at
fiber-passed locations.
{% enddocs %}

{% docs upgrade_opportunity %}
Active copper accounts at locations where fiber is already available — the
addressable base for a copper → fiber upgrade campaign.
{% enddocs %}

{% docs fcr %}
First-contact resolution: share of tickets resolved without a follow-up contact.
{% enddocs %}

{% docs nps %}
Net Promoter Score = % promoters (9–10) − % detractors (0–6), from −100 to 100.
{% enddocs %}

{% docs truck_roll %}
A field technician dispatch (row in raw.dispatches) raised for the ticket.
{% enddocs %}
