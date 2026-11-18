"""Public reference cache. Caller schedules network and owns all SQLite writes."""
from datetime import timedelta
from .contracts import instant


class StockMetadataCache:
    def __init__(self, entries=None, *, ttl_seconds=300):
        self.entries=dict(entries or {})
        self.pending=set()
        self.ttl=ttl_seconds

    def needed(self, codes, *, at):
        now=instant(at)
        result=[]
        for code in sorted(set(codes)-self.pending):
            entry=self.entries.get(code,{})
            expiry=entry.get('metadata_valid_until') if entry.get('metadata_status')=='VERIFIED' else entry.get('retry_at')
            if expiry is None or instant(expiry)<=now:
                result.append(code)
        return result

    def accept(self, codes, response, *, at):
        now=instant(at)
        payload=response.payload.get('result') if response is not None and response.status==200 and isinstance(response.payload,dict) else None
        rows={r['symbol']:r for r in payload if isinstance(r,dict) and r.get('symbol') in codes} if isinstance(payload,list) else {}
        for code in codes:
            row=rows.get(code)
            if row is not None and (not isinstance(row.get('name'),str) or not row['name'] or len(row['name'])>200):
                row=None
            if row is not None:
                detail=row.get('koreanMarketDetail')
                detail=detail if isinstance(detail,dict) else {}
                public={key:row[key] for key in ('name','market','securityType','isCommonShare','leverageFactor',
                    'status','sharesOutstanding','koreanMarketDetail') if key in row}
                self.entries[code]={**public,'metadata_status':'VERIFIED',
                    'metadata_valid_until':(now+timedelta(seconds=self.ttl)).isoformat(),
                    'halted':detail.get('krxTradingSuspended') is True or detail.get('nxtTradingSuspended') is True,
                    'delisting':detail.get('liquidationTrading') is True}
            else:
                delay=60
                if response is not None:
                    retry=next((v for k,v in response.headers.items() if k.lower()=='retry-after'),None)
                    if isinstance(retry,str) and len(retry)<=6 and retry.isdigit():
                        delay=max(delay,min(int(retry),86400))
                self.entries[code]={'metadata_status':'UNAVAILABLE','retry_at':(now+timedelta(seconds=delay)).isoformat()}
        self.pending.difference_update(codes)

    def snapshot(self, codes, *, at):
        now=instant(at)
        result={}
        for code in codes:
            entry=dict(self.entries.get(code,{'metadata_status':'PENDING'}))
            if entry.get('metadata_status')=='VERIFIED' and instant(entry['metadata_valid_until'])<=now:
                entry['metadata_status']='STALE'
            result[code]=entry
        return result
