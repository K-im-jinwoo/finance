import hashlib
import json
from datetime import date,datetime,timezone,timedelta
from decimal import Decimal
from bootstrap import ROOT
from stock_assistant.models import OHLCV
from stock_assistant.indicators import price_features
from stock_assistant.validation import assert_point_in_time

def independent_features(bars):
    closes = [bar.close for bar in bars]
    mean = lambda values: sum(values, Decimal(0)) / Decimal(len(values))
    volumes = [Decimal(bar.volume) for bar in bars[-21:-1]]
    average_volume = mean(volumes)
    # Wilder ATR uses the same documented 61-bar input window, with a separate
    # recurrence rather than calling any original indicator helper.
    window = bars[-61:]
    ranges = [max(current.high-current.low, abs(current.high-previous.close),
                  abs(current.low-previous.close)) for previous,current in zip(window,window[1:])]
    atr = mean(ranges[:14])
    for value in ranges[14:]:
        atr = (atr * Decimal(13) + value) / Decimal(14)
    return {
        'close': closes[-1],
        'sma20': mean(closes[-20:]),
        'sma60': mean(closes[-60:]),
        'return5': closes[-1]/closes[-6]-1,
        'return20': closes[-1]/closes[-21]-1,
        'drawdown60': closes[-1]/max(closes[-60:])-1,
        'volume_ratio20': Decimal(0) if average_volume == 0 else Decimal(bars[-1].volume)/average_volume,
        'average_value20': mean([bar.close * Decimal(bar.volume) for bar in bars[-20:]]),
        'atr14': atr,
    }

def calculate():
    snapshot=json.loads((ROOT/'datasets/observed-public-prices.json').read_text(encoding='utf-8-sig'))
    sample=json.loads((ROOT/'datasets/observed-provider-sample.json').read_text(encoding='utf-8-sig'))
    as_of=datetime.fromisoformat(snapshot['retrieved_at'])
    rows=snapshot['rows']+[r for r in sample['price_rows'] if r['trade_date']=='2026-09-29']
    selected={}
    for row in rows:
        observed=datetime.fromisoformat(row['observed_at'])
        assert_point_in_time(as_of,observed_at=observed,label='public price')
        key=(row['symbol'],row['trade_date'])
        if key not in selected or observed>datetime.fromisoformat(selected[key]['observed_at']):
            selected[key]=row
    results=[]
    for symbol in sorted({key[0] for key in selected}):
        data=sorted([r for (s,_),r in selected.items() if s==symbol],key=lambda r:r['trade_date'])
        bars=[OHLCV(r['symbol'],date.fromisoformat(r['trade_date']),*(Decimal(r[k]) for k in ('open','high','low','close')),int(r['volume']),r['source'],datetime.fromisoformat(r['observed_at'])) for r in data]
        calculations=[]
        for length in range(61,len(bars)+1):
            prefix=bars[:length]
            features=price_features(prefix)
            independent=independent_features(prefix)
            for field,value in independent.items():
                assert getattr(features,field)==value, f'{symbol} {prefix[-1].trade_date} {field} mismatch'
            calculations.append({'calculation_date':prefix[-1].trade_date.isoformat(),
                                 'values':{key:str(value) for key,value in independent.items()},
                                 'all_fields_equal':True})
        if not calculations:
            raise ValueError(f'{symbol}: fewer than 61 trading dates')
        features=price_features(bars)
        # The most recent date exists as a provider sample but is absent from source DB.
        source_latest=max(r['trade_date'] for r in snapshot['rows'] if r['symbol']==symbol)
        results.append({'symbol':symbol,'distinct_trading_dates':len(bars),'start':bars[0].trade_date.isoformat(),'end':bars[-1].trade_date.isoformat(),'source_db_latest':source_latest,
                        'source_features_equal_independent_formulas':True,'latest_close':str(features.close),'sma20':str(features.sma20),'sma60':str(features.sma60),'return5':str(features.return5),'return20':str(features.return20),
                        'calculation_dates_checked':len(calculations),'fields_checked':list(independent),'calculations':calculations,
                        'numeric_price_lt_sma20':features.close<features.sma20,'automatic_invalidation_certified':False,'limitation':'corporate-action basis and real check availability gate not certified'})
    result={'checked_at_kst':datetime.now(timezone(timedelta(hours=9))).isoformat(),'mode':'OBSERVED_PUBLIC_PRICE_CALCULATION_VALIDATION','as_of':as_of.isoformat(),'input_sha256':hashlib.sha256((ROOT/'datasets/observed-public-prices.json').read_bytes()).hexdigest(),
            'observation_times_preserved':True,'support_is_three_years':False,'source_db_changed':False,
            'calculation_dates_total':sum(row['calculation_dates_checked'] for row in results),'historical_decisions_certified':False,'results':results}
    (ROOT/'artifacts/observed-price-calculation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result

if __name__=='__main__':
    print(json.dumps(calculate(),ensure_ascii=False,indent=2))

