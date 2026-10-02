'use strict';
const $=id=>document.getElementById(id);
const params=new URLSearchParams(location.search);
let account=params.get('account'), reportKey=params.get('report'), busy=false;
const esc=v=>String(v??'확인 불가').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=v=>v==null?'확인 불가':Math.round(Number(v)).toLocaleString('ko-KR')+'원';
const pct=v=>v==null?'확인 불가':(Number(v)*100).toFixed(2)+'%';
const at=v=>v?new Date(v).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'확인 불가';
const freshness={FRESH:'최근 시세',DELAYED:'지연',STALE:'오래된 시세',MISSING:'시세 없음',INVALID:'시세 확인 불가'};
async function api(path,body){const res=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Paper-Action':'1'},body:JSON.stringify(body)});const data=await res.json();if(!res.ok)throw new Error(data.error||'요청 실패');return data;}
function table(headers,rows){return rows.length?'<table><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>':'<p class="empty">아직 가상 체결이 없습니다.</p>';}
function metric(label,value,note){return `<article class="metric"><small>${esc(label)}</small><strong>${esc(value)}</strong><span>${esc(note)}</span></article>`;}
function render(r,source){
 const s=r.state,meta=r.meta,latest=r.reports[0],pick=latest?.selected;
 $('connection').textContent=(source.state==='CONNECTED'?'Hermes 실제 추천 연결됨':'Hermes 연결 상태: '+source.state)+(meta.feed.mode==='OPERATING_STORE_READ_ONLY'?' · 운영 시세 저장소 읽기 전용':'');
 $('feed-note').textContent=`대시보드 자료 확인 30초 간격 · ${meta.feed.mode==='OPERATING_STORE_READ_ONLY'?'원본 시세 수집 약 5분 간격 · 전용 시세는 별도 인증 등록·연결 검증이 필요합니다.':meta.feed.error?'전용 시세 연결 확인 필요 · 현재가·시가 확보 대기':'전용 공개 시세 REST 30초 간격 조회 · 거래일 시가 확인'} 마지막 자료 확인 ${at(meta.feed.polled_at)} KST`;
 if(meta.feed.error==='SUPPLIED_ACCESS_TOKEN_REJECTED_OR_EXPIRED')$('feed-note').textContent='입력한 Open API Access Token이 거부되었거나 만료됐습니다. 자동 재발급 없이 시세 조회를 중단했습니다. 토큰은 입력 도구에서 교체해 주세요.';
 else if(meta.feed.auth_mode==='SUPPLIED_ACCESS_TOKEN')$('feed-note').textContent+=' · 사용자가 입력한 Access Token 사용 · 자동 재발급 없음';
 else if(meta.feed.auth_mode==='SHARED_CLIENT_TOKEN_CACHE')$('feed-note').textContent+=' · 기존 토스 인증 연결됨';
 if(meta.feed.execution_gate==='OPEN_CAPTURE_POLICY_APPROVAL_REQUIRED')$('feed-note').textContent+=' · 시가 관측·체결 정책 확인 전 자동 체결 보류';
 $('live-metrics').innerHTML=metric('총자산',won(r.current_assets),'현금 '+won(s.cash))+metric('계좌 수익률',pct(r.current_return),s.trades.length?'매수비용 반영':'가상 체결 전 · 현금 유지')+metric('보유 종목',Object.keys(s.positions).length+'개',`편입 대기 ${Object.keys(s.pending).length}개 · 최대 5개`)+metric('가상 체결',s.trades.length+'건','실현손익 '+won(s.realized_pnl));
 $('live-pause').disabled=s.status!=='RUNNING';$('live-resume').disabled=s.status!=='PAUSED';
 let top='<p class="empty">명시적인 1순위가 확인되지 않아 자동 편입을 보류합니다. 추천 원문은 아래에서 확인할 수 있습니다.</p>';
 if(pick){const q=r.watch_quotes[pick.symbol];top=`<div class="pick-head"><h3>${esc(pick.name)} <small>${esc(pick.symbol)}</small></h3><span class="pill">Hermes 명시 순위 ${pick.rank}</span><span class="pill warning">원본 판정 ${esc(pick.original_decision)}</span></div><p>${esc(pick.reason)}</p><p class="meta">추가 확인: ${esc(pick.conditions)}</p><p class="pick-price">최근 가격 <strong>${won(q?.price)}</strong> <span class="meta">${esc(freshness[q?.freshness]||'시세 없음')} · 시세 시각 ${at(q?.source_timestamp)} KST</span></p>`;}
 $('top-pick').innerHTML=top;
 $('account-status').textContent=`${s.status==='PAUSED'?'일시중지 · 대기 거래 취소 · 시세 평가는 계속':'추천·시세 확인 중'} · 시작 ${at(r.created_at)} KST · 1회 예산 총자산의 20%에 매수비용 포함 · 매수·매도 각 15bps · 정수 주식`;
 $('live-positions').innerHTML=table(['종목','수량','가상 매수가','현재가','평가손익','비용 반영 수익률','시세 시각 / 상태'],Object.entries(s.positions).map(([symbol,p])=>{const q=r.position_quotes[symbol];return [esc(r.securities[symbol]?.name)+'<small class="block">'+esc(symbol)+'</small>',p.quantity+'주',won(p.entry_price),won(q.price),won(q.pnl),pct(q.return),at(q.source_timestamp)+'<small class="block">'+esc(freshness[q.freshness])+' · '+esc(q.source)+'</small>'];}));
 $('live-pending').innerHTML=Object.entries(s.pending).map(([symbol,p])=>`<div class="event"><span class="pill warning">${p.side==='BUY'?'편입':'매도'} 대기</span><strong>${esc(r.securities[symbol]?.name)} (${esc(symbol)})</strong><p class="meta">신호 확보 ${at(p.created_at)} KST · 현재 수익률: 체결 후 계산</p><p class="meta">${meta.calendar?.next_regular_open?'다음 확인된 정규장 시가 '+at(meta.calendar.next_regular_open)+' KST':'정규장 거래일·수정되지 않은 실제 시가 연결 확인 대기'} · 이미 지난 시가로 소급 체결하지 않습니다.</p></div>`).join('');
 $('live-report-select').innerHTML=r.reports.map(p=>`<option value="${esc(p.key)}">${esc(p.report_id||p.key)} · ${at(p.published_at)}</option>`).join('');
 const chosen=reportKey?r.reports.find(p=>p.key===reportKey):latest;
 $('live-report-select').value=chosen?.key||'';
 if(chosen){$('report-lineage').innerHTML=`<p><span class="pill">${esc(chosen.author)}</span> 최종판정 · ${esc(chosen.report_id||'보고서 ID 미확인')}</p><p class="meta">Hermes 생성 ${at(chosen.published_at)} KST · 최초 확보 ${at(chosen.retrieved_at)} KST · ${chosen.selection_status==='EXPLICIT_RANK_1'?'명시 순위 확인':'순위·형식 확인 필요 · 자동 편입 보류'}</p>`;
 $('candidate-cards').innerHTML=chosen.candidates.map(c=>`<article class="candidate"><span class="pill ${c.rank===1?'':'warning'}">${c.rank}순위 ${c.rank===1?'· 모의 편입 검토':'· 보고서 열람'}</span><h3>${esc(c.name)} <small>${esc(c.symbol)}</small></h3><p class="meta">${esc(c.original_review_tier)} · ${esc(c.original_decision)}</p><p>${esc(c.reason)}</p><p class="meta">확인 조건: ${esc(c.conditions)}</p><p class="meta">원문 무효화 조건: ${esc(c.invalidation)}</p></article>`).join('');
 $('report-original').textContent=chosen.content;
 }else{$('report-lineage').textContent='선택한 원본 보고서를 찾을 수 없습니다. 최신 보고서로 대체하지 않습니다.';$('candidate-cards').replaceChildren();$('report-original').textContent='';}
 $('live-trades').innerHTML=table(['체결시각 KST','종목','구분','수량','가격','비용','보고서'],s.trades.map(t=>[at(t.filled_at),esc(t.symbol),t.side==='BUY'?'가상 매수':'가상 매도',t.quantity+'주',won(t.price),won(t.fee),esc(t.report_id)]));
 const labels={PRICE_LT_SMA20:'가격 · 20일 평균',GENERAL_FINANCIAL:'재무 · 이익/OCF',DISCLOSURE:'공시'};
 $('live-checks').innerHTML=Object.entries(s.checks).map(([symbol,checks])=>`<div class="event"><strong>${esc(r.securities[symbol]?.name)} · 근거 점검</strong><p class="meta">${checks.map(c=>esc(labels[c.code]||c.code)+': '+esc(c.status)).join(' / ')}</p><p class="meta">검증된 가격·재무 근거가 없으면 UNKNOWN을 유지합니다. 추천 문장을 수치 검증으로 대체하지 않습니다.</p></div>`).join('');
 $('live-settings').textContent=JSON.stringify({account_id:r.account_id,created_at:r.created_at,selection:meta.selection,policy:r.policy,execution_gate:meta.feed.execution_gate,feed:meta.feed,source,limitations:r.coverage.limitations},null,2);
}
async function refresh(){if(busy)return;busy=true;try{const list=await api('/api/paper-accounts');if(!account&&!params.has('account'))account=list.accounts[0];if(!account){$('connection').textContent='Hermes 최종보고서 연결 '+list.source.state;return;}const r=await api('/api/paper-accounts/'+account);render(r,list.source);$('live-error').textContent='';if(!params.has('account')){params.set('account',account);history.replaceState(null,'','/hermes?'+params);}}catch(e){$('live-error').textContent=e.message==='NOT_FOUND'?'선택한 계좌를 찾을 수 없습니다. 과거 링크를 새 계좌로 대체하지 않습니다.':e.message;}finally{busy=false;}}
for(const name of ['pause','resume'])$('live-'+name).addEventListener('click',async()=>{try{await api(`/api/paper-accounts/${account}/${name}`,{});await refresh();}catch(e){$('live-error').textContent=e.message;}});
$('live-report-select').addEventListener('change',()=>{reportKey=$('live-report-select').value;params.set('report',reportKey);history.replaceState(null,'','/hermes?'+params);refresh();});
refresh();setInterval(refresh,5000);
