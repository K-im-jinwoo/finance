'use strict';
const $=id=>document.getElementById(id);
const params=new URLSearchParams(location.search);
let selected=params.get('experiment'), result, refreshing=false;
const esc=value=>String(value??'확인 불가').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=value=>value==null?'확인 불가':Math.round(Number(value)).toLocaleString('ko-KR')+'원';
const pct=value=>value==null?'확인 불가':(Number(value)*100).toFixed(2)+'%';
const at=value=>value?new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'확인 불가';
const day=value=>value?new Date(value).toLocaleDateString('en-CA',{timeZone:'Asia/Seoul'}):'';
const names={READY:'실행 대기',RUNNING:'계산 중',PAUSED:'일시중지 · 평가는 계속',COMPLETED:'완료',FAILED:'실패',BUY:'가상 매수',SELL:'가상 매도',INVALIDATION:'무효화 확인',EVENING_SUMMARY:'거래일 저녁 요약',ERROR:'오류'};
const link=query=>'/?'+new URLSearchParams({experiment:selected,...query}).toString();
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Paper-Action':'1'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error||'요청 실패');return data;}
function table(headers,rows){return rows.length?'<table><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(cell=>'<td>'+cell+'</td>').join('')+'</tr>').join('')+'</tbody></table>':'<p class="empty">아직 기록이 없습니다.</p>';}
function render(r){
 result=r;const s=r.state,m=r.metrics,last=s.equity.at(-1);
 $('status').textContent=`${names[s.status]||s.status} · ${r.cursor}/${r.total_events} 시점 · 기준 ${at(s.clock)} KST · ${r.coverage.mode==='SYNTHETIC'?'합성 자료':'관측 자료 검증'} · 왕복 ${r.policy.round_trip_bps}bps`;
 $('metrics').innerHTML=[['총자산',won(last?.total_assets),'현금 '+won(s.cash)],['계좌 수익률',pct(last?.return),'종목별 평균수익률과 구분'],['최대낙폭',pct(m.max_drawdown),'평가 관측시점 기준'],['가상 체결',s.trades.length+'건',`완료 보유 구간 ${m.completed_round_trips} · 열린 보유 ${m.open_positions}`]].map(([a,b,c])=>`<article class="metric"><small>${a}</small><strong>${b}</strong><span>${c}</span></article>`).join('');
 $('start').disabled=s.status!=='READY';$('pause').disabled=s.status!=='RUNNING';$('resume').disabled=s.status!=='PAUSED';
 const valid=s.equity.filter(e=>e.total_assets!=null);if(valid.length>1){const values=valid.map(e=>Number(e.total_assets));const min=Math.min(10000000,...values),max=Math.max(10000000,...values),range=max-min||1;const points=values.map((v,i)=>`${15+i*970/(values.length-1)},${155-(v-min)*130/range}`).join(' ');$('chart').innerHTML=`<svg viewBox="0 0 1000 180" role="img" aria-label="합성 데이터 모의계좌 자산 추이"><path d="M15 155 H985 M15 90 H985 M15 25 H985" stroke="#e4eae3" fill="none"/><polyline points="${points}" stroke="#185a48" stroke-width="3" fill="none"/></svg>`;}else $('chart').innerHTML='<p class="empty">평가 자료가 확보되면 자산 추이를 표시합니다.</p>';
 $('basis').textContent=`기본 자금 10,000,000원 · 비용 ${won(m.total_cost)} · 실현손익 ${won(m.realized_pnl)} · 현금 비중 ${pct(m.cash_ratio)} · 완료 구간 승률 ${pct(m.win_rate)} · 벤치마크 미선정`;
 $('changes').innerHTML=s.outbox.filter(e=>day(e.at)===day(s.clock)).reverse().map(e=>`<a class="event" href="${esc(link({event:e.event_id}))}"><span class="pill">${esc(names[e.kind]||e.kind)}</span><strong>${esc(e.symbol||'계좌')}</strong><small>${at(e.at)} KST</small></a>`).join('')||'<p class="empty">기준일에 새 변화가 없습니다.</p>';
 $('positions').innerHTML=table(['종목','수량','진입가','원본 판정','무효화 점검'],Object.entries(s.positions).map(([symbol,p])=>[esc(symbol),p.quantity+'주',won(p.entry_price),esc(p.original_decision),esc((s.checks[symbol]||[]).map(c=>c.code+': '+c.status).join(' / ')||'확인 불가')]));
 $('trades').innerHTML=table(['체결시각 KST','종목','구분','수량','가격','비용','상세'],s.trades.map(t=>[at(t.filled_at),esc(t.symbol),esc(names[t.side]),t.quantity+'주',won(t.price),won(t.fee),`<a href="${esc(link({trade:t.trade_id,report:t.report_id}))}">거래 근거 →</a>`]));
 $('reports').innerHTML=s.reports.slice().reverse().map(report=>`<a class="event" href="${esc(link({report:report.report_id}))}"><strong>${at(report.at)} KST</strong><small>${esc(report.candidates.map(c=>c.symbol+' · '+c.decision+' / '+c.review_tier).join('　|　'))}</small></a>`).join('')||'<p class="empty">보고서가 없습니다.</p>';
 $('settings').textContent=JSON.stringify({coverage:r.coverage,policy:r.policy,metrics:m,pending:s.pending},null,2);
 const trade=s.trades.find(t=>t.trade_id===params.get('trade')),event=s.outbox.find(e=>e.event_id===params.get('event')),report=s.reports.find(p=>p.report_id===params.get('report'));
 if(params.has('trade')||params.has('event')||params.has('report')){const validDetail=(!params.has('trade')||trade)&&(!params.has('event')||event)&&(!params.has('report')||report)&&(!trade||!report||trade.report_id===report.report_id)&&(!trade||!event||event.detail?.trade_id===trade.trade_id)&&(!event||!report||event.detail?.report_id===report.report_id);let summary='';if(trade)summary+=`<p><span class="pill">${esc(names[trade.side])}</span><strong>${esc(trade.symbol)} · ${trade.quantity}주</strong></p><p>${won(trade.price)} · 체결비용 ${won(trade.fee)}</p><p class="meta">신호 ${at(trade.signal_at)} → 체결 ${at(trade.filled_at)} KST</p>`;if(event)summary+=`<p>${esc(names[event.kind])} · ${at(event.at)} KST</p>`;if(report)summary+=`<p class="meta">보고서 생성 ${at(report.at)} KST · 원본 판정 보존</p>`;$('detail').innerHTML=validDetail?summary+'<details><summary>전체 근거와 시점 기록</summary><pre>'+esc(JSON.stringify({experiment:r.experiment_id,trade,event,report},null,2))+'</pre></details>':'<p class="empty">해당 실험에 상세 기록이 없습니다. 최신 보고서로 대체하지 않습니다.</p>';}
}
async function refresh(){if(refreshing)return;refreshing=true;try{const list=await api('/api/experiments');const options=list.map(e=>`<option value="${esc(e.experiment_id)}">${esc(e.experiment_id.slice(0,12))} · ${esc(e.cost_bps)}bps · ${esc(names[e.state])}</option>`).join('');$('experiments').innerHTML=options;if(!selected&&!params.has('experiment'))selected=list.find(e=>e.cost_bps==='30')?.experiment_id||list[0]?.experiment_id;$('experiments').value=selected||'';if(selected){const r=await api('/api/experiments/'+selected);render(r);const list=await api('/api/experiments/'+selected+'/drafts');$('drafts').innerHTML=list.slice().reverse().map(d=>`<a class="event" href="${esc(d.detail_path)}"><span class="pill warning">발송 대기 초안</span><strong>${esc(names[d.kind])} ${esc(d.symbol||'')}</strong><small>${at(d.at)} KST · 과거 상세 →</small></a>`).join('')||'<p class="empty">발송 초안이 없습니다.</p>';}}catch(e){$('error').textContent=e.message==='NOT_FOUND'?'선택한 실험을 찾을 수 없습니다. 과거 링크를 최신 실험으로 대체하지 않습니다.':e.message;}finally{refreshing=false;}}
async function action(name){try{await api(`/api/experiments/${selected}/${name}`,{});$('error').textContent='';await refresh();}catch(e){$('error').textContent=e.message;}}
$('experiments').addEventListener('change',()=>location.href=link({experiment:$('experiments').value}));
$('create').addEventListener('click',async()=>{try{const created=await api('/api/experiments',{cost_bps:$('cost').value});location.href='/?experiment='+created.experiment_id;}catch(e){$('error').textContent=e.message;}});
['start','pause','resume'].forEach(name=>$(name).addEventListener('click',()=>action(name)));
refresh();setInterval(refresh,1400);
api('/api/status').then(data=>{
 const db=data.oracle_inventory?.tables;
 if(!db){$('data-status').innerHTML='<p class="empty">실제 자료 위치 확인 대기</p>';return;}
 let html=`<p class="meta">읽기 전용 조회 ${at(data.checked_at_kst)} KST · 운영 DB 변경 없음</p><p>가격 ${esc(db.ohlcv.distinct_dates)}개 거래 날짜 / ${esc(db.ohlcv.symbols)}종목 / ${esc(db.ohlcv.trade_date_range.join(' ~ '))}</p><p>재무 ${esc(db.financial_snapshots.symbols)}종목 · 당시 관측/정정 이력 미인증</p><p class="meta">기존 Oracle 공급자 키 확인 · 실제 3년 시점별 자료는 아직 미확보</p>`;
 const rows=data.actual_price_validation?.results||[];
 if(rows.length)html+='<div class="table-wrap">'+table(['실제 검산 종목','자료 기간','거래 날짜','독립 산식'],rows.map(r=>[esc(r.symbol),esc(r.start+' ~ '+r.end),esc(r.distinct_trading_dates),r.source_features_equal_independent_formulas?`${esc(r.calculation_dates_checked)}개 계산 날짜 · ${esc(r.fields_checked.length)}개 지표 일치`:'확인 불가']))+'</div>';
 const financial=data.actual_financial_validation;
 if(financial)html+=`<p>실제 재무 검산 ${esc(financial.symbol)} · ${esc(financial.division)} · 연간/두 반기 3기간 · ${esc(Object.keys(financial.checks).length)}개 대조 ${financial.all_equal?'일치':'불일치'}</p><p class="meta">TTM 영업이익 ${won(financial.values.ttm_operating_income)} · OCF는 원본 연간 기준 · 관측 ${at(financial.observed_at)} KST · 과거 시점 인증 전</p>`;
 $('data-status').innerHTML=html;
}).catch(e=>{$('data-status').textContent='확인 불가: '+e.message;});

