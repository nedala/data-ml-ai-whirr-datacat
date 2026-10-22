-- Plant quality per account × month (fiber ONT optics or copper DSL line)
-- with CLLI, state and plan — the base for optics / SNR distributions.
select
    q.ban,
    q.metric_month,
    q.technology,
    c.clli,
    c.wire_center_name,
    c.state,
    c.region,
    c.plan_id,
    c.ont_model,
    c.loop_length_kft,
    q.rx_power_dbm,
    q.los_events,
    q.bip_errors,
    q.reboots,
    q.uptime_pct,
    q.is_weak_optics,
    q.snr_margin_db,
    q.attenuation_db,
    q.sync_rate_down_mbps,
    q.retrains,
    q.crc_errors,
    q.is_marginal_line,
    q.is_bad_line
from {{ ref('int_plant_quality_monthly') }} as q
left join {{ ref('int_customer_current_service') }} as c on c.ban = q.ban
