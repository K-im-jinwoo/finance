"""Explicit user-approved Phase1 experimental paper settings, not fee-policy changes."""


def live_settings():
    return {'quality':{'ranking_max_age_seconds':90,'quote_max_age_seconds':5},
        'scanner_config': {'max_watch':8, 'require_common_stock':True,'ranking_max_age_seconds':90,'quote_max_age_seconds':5,
        'max_price':'200000', 'min_market_cap':'50000000000','max_market_cap':'10000000000000'},
        'strategy_config': {'min_bid_volume':'1','min_bid_notional':'5000000','quote_max_age_seconds':5,
            'entry_start_hhmm':'09:05','min_hold_seconds_for_weak_exit':180,
            'volume_spike_ratio':'1.8','max_ask_bid_volume_ratio':'1.5','breakout_lookback_bars':10,
            'take_profit_bps':'200','stop_loss_bps':'150','weak_exit_min_vwap_gap_bps':'50',
            'trailing_stop_activation_bps':'90','trailing_stop_retrace_bps':'40'},
        'max_daily_entries':5,'reentry_cooldown_seconds':1200}


def merge_live_settings(saved, overrides):
    result={**saved,**(overrides or {})}
    for key in ('scanner_config','strategy_config','quality'):
        result[key]={**saved.get(key,{}),**(overrides or {}).get(key,{})}
    return result
