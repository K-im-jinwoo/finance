"""Historical material-risk queries when a broad issuer query exceeds provider limits."""
from datetime import date, datetime, timedelta, timezone
from .dart_research import _year_start
from .models import Evidence,to_json_value
from .news_policy import RISK_QUERY_PLAN, RISK_QUERY_VERSION, news_windows
from .unified_research import KST,financial_universe
from .validation import validate_analysis_time

PLAN=(('횡령','MANAGEMENT'),('배임','MANAGEMENT'),('유상증자','DILUTION'),('전환사채','DILUTION'),('계약해지','BUSINESS_DAMAGE'))


def collect_material_archive(repository,store,client,*,symbols=None,pages=5,clock=None,split_policy=False):
    if type(pages) is not int or not 1<=pages<=5:raise ValueError('material archive pages must be 1..5')
    if type(split_policy) is not bool:raise ValueError('split_policy must be boolean')
    clock=clock or (lambda:datetime.now(timezone.utc));now=validate_analysis_time(clock())
    end=now.astimezone(KST).date()-timedelta(days=1);start=_year_start(end,3)
    eligible=[(s,f) for s,f,_ in financial_universe(repository,store,as_of=now) if f.gate=='PASS' and f.financial_score>=70
              and (symbols is None or s.symbol in symbols)]
    cursor_kind = 'risk_archive_cursor' if split_policy else 'material_archive_cursor'
    article_kind = 'risk_archive_article' if split_policy else 'material_archive_article'
    plan = RISK_QUERY_PLAN if split_policy else PLAN
    states=store.latest_by_symbol(cursor_kind,as_of=now)
    def priority(sf):
        record = states.get(sf[0].symbol)
        group = 0 if split_policy and record and record['payload'].get('status') == 'PARTIAL' else 1
        return group,(record or {}).get('observed_at',''),-sf[1].financial_score,sf[0].symbol
    eligible.sort(key=priority)
    def due(symbol):
        record = states.get(symbol)
        if record is None:return True
        state = record['payload']
        if split_policy:
            if state.get('query_version') != RISK_QUERY_VERSION:return True
            if state.get('status') == 'PROVIDER_LIMIT':
                return now-datetime.fromisoformat(record['observed_at']) >= timedelta(days=7)
            if state.get('status') == 'COMPLETE' and state['start'] <= start.isoformat():
                # Current recent news can bridge this completed baseline; do not
                # repeat all historical keyword queries on every new date.
                return date.fromisoformat(state['end']) < news_windows(now)['recent_start']-timedelta(days=1)
        return state.get('end')!=end.isoformat() or state.get('status')=='PARTIAL'
    security=next((s for s,_ in eligible if due(s.symbol)),None)
    if security is None:return {'status':'CURRENT','actual_calls':0}
    old=(states.get(security.symbol) or {}).get('payload',{})
    state=dict(old) if old.get('status')=='PARTIAL' and (not split_policy or old.get('query_version')==RISK_QUERY_VERSION) else {'start':start.isoformat(),'end':end.isoformat(),
          'queries':[{ 'keyword':word,'category':cat,'next_start':1,'anchor_url':None,'status':'PARTIAL'} for word,cat in plan]}
    if split_policy:state['query_version']=RISK_QUERY_VERSION
    client.archive_batch(pages);used=0
    for _ in range(pages):
        query=next((q for q in state['queries'] if q['status']=='PARTIAL'),None)
        if query is None:break
        page=client.archive_page(query=security.name+' '+query['keyword'],start=query['next_start']);used+=1
        if query['anchor_url'] and query['anchor_url'] not in {a.url for a in page.articles}:
            query.update(next_start=1,anchor_url=None)
            state['status']='PARTIAL'
            store.put(cursor_kind,security.symbol,state,observed_at=clock())
            continue
        rows=[]
        for article in page.articles:
            # Search association remains unverified. Keep every returned claim for due diligence.
            evidence=Evidence('NEWS',article.title,article.url,article.published_at,article.observed_at,False,
                (f'ISSUER:{security.symbol}',f"MATERIAL_QUERY:{query['category']}", 'SEARCH_ASSOCIATION_UNVERIFIED'))
            rows.append((article_kind,security.symbol,{'evidence':to_json_value(evidence),'description':article.description,
                         'keyword':query['keyword'],'category':query['category']}))
        store.put_many(rows,observed_at=page.observed_at)
        oldest=page.articles[-1].published_at if page.articles else None
        exhausted=page.raw_count<100 and query['next_start']+page.raw_count-1>=page.total
        if exhausted or (oldest and oldest.astimezone(KST).date().isoformat()<state['start']):query['status']='COMPLETE'
        elif page.raw_count<2 or query['next_start']+page.raw_count-1>=1000:query['status']='PROVIDER_LIMIT'
        else:query.update(next_start=query['next_start']+page.raw_count-1,anchor_url=page.articles[-1].url)
        query['oldest_seen']=oldest.isoformat() if oldest else None
        state['status']='PARTIAL' if any(q['status']=='PARTIAL' for q in state['queries']) else 'PROVIDER_LIMIT' if any(q['status']=='PROVIDER_LIMIT' for q in state['queries']) else 'COMPLETE'
        key=store.put(cursor_kind,security.symbol,state,observed_at=clock())
    if state.get('status')=='COMPLETE' and not split_policy:
        observed=clock();dart=store.latest('dart_cursor',security.symbol,as_of=observed)
        if dart and dart['payload']['start']<=_year_start(end,10).isoformat() and dart['payload']['end']>=state['end']:
            store.put('material_coverage',security.symbol,{'symbol':security.symbol,'start':state['start'],'end':state['end'],
                'observed_at':observed.isoformat(),'reviewed_at':observed.isoformat(),'status':'AUTO_SEARCH_VERIFIED',
                'scope':'NAVER_MATERIAL_EVENT_QUERIES','sources':['NEWS_ARCHIVE','DART_HISTORY'],
                'source_refs':[key,dart['input_id']],'reviewer':'material-query-traversal-v1',
                'rationale':'five specified historical risk searches exhausted or reached cutoff; claims remain unverified'},observed_at=observed)
    return {'symbol':security.symbol,'status':state.get('status','PARTIAL'),'actual_calls':used,'reserved_calls':pages,
            'scope':'NAVER_MATERIAL_EVENT_QUERIES','query_states':[{k:q.get(k) for k in ('keyword','status','next_start','oldest_seen')} for q in state['queries']]}

