'use strict';

const byId = id => document.getElementById(id);
const labels = {
  BUY: '매수', SELL: '매도', HOLD: '대기', CLOSED: '마감 · 보유 없음',
  UNFLATTENED: '미청산 보유', OBSERVING: '관측 중', OPEN: '신규 진입 가능 시간',
  ENTRY_CLOSED: '신규 진입 마감', TIME_EXITING: '시간 청산 중',
  LOSS_LOCKED: '일손실 한도 잠금', DATA_DEGRADED: '데이터 품질 잠금',
  RATE_LIMIT_DEGRADED: '호출 제한 잠금', PRIOR_DAY_UNFLATTENED: '전일 미청산 잠금',
  TIME_EXIT: '시간 청산', LOSS_EXIT: '일손실 한도 청산', TAKE_PROFIT: '익절', TRAILING_STOP: '트레일링 익절',
  STOP_LOSS: '손절', MOMENTUM_WEAK: '모멘텀 약화', VWAP_BREAKOUT: '분봉 근사 VWAP 돌파',
  RISING_CLOSE: '완료 분봉 상승', VOLUME_SPIKE: '완료 분봉 거래량 급증', BID_DEPTH: '매수호가 잔량 충족',
  HALTED: '거래정지', DELISTING: '상장폐지 위험', WARNING_OR_CAUTION: '경고·주의',
  MAJOR_NEGATIVE_DISCLOSURE: '중대 악재 공시', LOW_TURNOVER: '거래대금 부족', LOW_PRICE: '최소 가격 미달',
  WIDE_SPREAD: '매수·매도 호가차 초과', WATCH_LIMIT: '순위 외 제외 (상위 감시 정원 초과)',
  TURNOVER_ELIGIBLE: '거래대금 조건 충족', TURNOVER_RANKED: '거래대금 순위 편입',
  VOLUME_RANKED: '거래량 순위 편입', QUOTE_ELIGIBLE: '호가 조건 충족',
  QUOTE_UNAVAILABLE_WATCH_ONLY: '호가 미확보 · 감시만 가능', RISK_METADATA_UNAVAILABLE: '위험 메타 미확보',
  RISK_METADATA_PROVIDED: '위험 메타 제공', ENTRY_NOT_ALLOWED: '시간·위험 상태로 신규 매수 차단',
  STALE_RANKINGS: '랭킹 지연', STALE_ORDERBOOK: '호가 지연', STALE_CANDLES: '분봉 지연',
  STALE_TRADE: '체결 시세 지연', SOURCE_TIMESTAMP_UNAVAILABLE: '원천 시각 미제공',
  EMPTY_ORDERBOOK: '호가 없음', CROSSED_ORDERBOOK: '호가 역전', ORDERBOOK_UNAVAILABLE: '호가 미확보',
  POSITION_HELD: '기존 보유 유지', COST_BASIS_UNAVAILABLE: '매입 원가 미확보',
  CANDLES_UNAVAILABLE: '분봉 미확보', INSUFFICIENT_CANDLES: '완료 분봉 부족',
  VOLUME_BASELINE_UNAVAILABLE: '비교 거래량 미확보', FRESHNESS_POLICY_UNSET: '시세 신선도 기준 미설정'
};
const textFor = value => labels[value] || String(value ?? '확인 불가');
const reasonsFor = values => (values || []).map(textFor).join(' · ') || '없음';
const numberFormat = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 4});
const dateFormat = new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'medium', hour12: false});
let current = null;
let busy = false;
let symbolNames = {};
const expanded = new Map();
const integerFormat = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 0});

function wonValue(value) {
  if (value == null || value === '' || typeof value === 'boolean' || !Number.isFinite(Number(value))) return null;
  const n = Number(value), rounded = Math.round(Math.abs(n));
  return rounded === 0 ? 0 : n < 0 ? -rounded : rounded;
}
function signFor(value, signed) { return value < 0 ? '-' : signed && value > 0 ? '+' : ''; }
function money(value, signed = false) {
  const n = wonValue(value);
  return n == null ? '확인 불가' : signFor(n, signed) + integerFormat.format(Math.abs(n)) + '원';
}
function koreanUnit(value, signed = false) {
  const n = wonValue(value);
  if (n == null) return '확인 불가';
  let remaining = Math.abs(n);
  if (remaining < 10000) return money(n, signed);
  const parts = [];
  for (const [size, suffix] of [[1e12, '조'], [1e8, '억'], [1e4, '만']]) {
    const amount = Math.floor(remaining / size);
    if (amount) parts.push(integerFormat.format(amount) + suffix);
    remaining %= size;
  }
  if (remaining) parts.push(integerFormat.format(remaining));
  return signFor(n, signed) + parts.join(' ') + (remaining ? '원' : ' 원');
}
function turnoverText(value) {
  const n = wonValue(value);
  if (n == null) return '확인 불가';
  const abs = Math.abs(n), size = abs >= 1e8 ? 1e8 : abs >= 1e4 ? 1e4 : 1;
  return signFor(n, false) + integerFormat.format(Math.floor(abs / size)) + (size === 1e8 ? '억 원' : size === 1e4 ? '만 원' : '원');
}
function spreadText(bps, entryBlocked) {
  if (bps == null || bps === '') return '호가 수신 대기';
  const n = Number(bps);
  return Number.isFinite(n) && n >= 0 ? `${(n / 100).toFixed(2)}% (${n.toFixed(1)}bps)` : '확인 불가';
}
function symbolLabel(code, provided) {
  if (!code || code === 'ALL') return '시장 전체';
  const name = symbolNames[code] || (provided && provided !== code ? provided : null);
  return name ? `${name} (${code})` : `종목명 확인 중 (${code})`;
}

function numeric(value, unit = '') {
  return value == null || !Number.isFinite(Number(value)) ? '확인 불가' : numberFormat.format(Number(value)) + unit;
}
function at(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? dateFormat.format(date) + ' KST' : '시각 확인 불가';
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function empty(target, message) {
  target.replaceChildren(node('p', message, 'empty'));
}
function table(target, headers, rows, message) {
  if (!rows.length) return empty(target, message);
  const element = node('table');
  const head = node('thead');
  const titles = node('tr');
  for (const label of headers) {
    const cell = node('th', label);
    cell.scope = 'col';
    titles.append(cell);
  }
  head.append(titles);
  const body = node('tbody');
  for (const row of rows) {
    const line = node('tr');
    for (const value of row) line.append(node('td', value ?? '확인 불가'));
    body.append(line);
  }
  element.append(head, body);
  target.replaceChildren(element);
}
function metric(label, value, note) {
  const card = node('article', undefined, 'metric');
  card.append(node('small', label), node('strong', value), node('span', note));
  return card;
}
function event(target, title, detail) {
  const item = node('div', undefined, 'event');
  item.append(node('strong', title), node('p', detail, 'meta'));
  target.append(item);
}
function disclosure(id, title, build) {
  const details = node('details', undefined, 'daytrade-details');
  details.id = id;
  details.append(node('summary', title));
  details.open = expanded.get(id) === true;
  let content = null;
  function populate() {
    if (content) return;
    content = node('div', undefined, 'history-content');
    build(content);
    details.append(content);
  }
  details.addEventListener('toggle', () => {
    expanded.set(id, details.open);
    if (details.open) populate();
  });
  if (details.open) populate();
  return details;
}
function newest(rows) { return [...rows].sort((a, b) => new Date(b.at) - new Date(a.at)); }
function boundedEvents(target, rows, limit, id, title) {
  const ordered = newest(rows);
  for (const row of ordered.slice(0, limit)) event(target, row.title, row.detail);
  if (ordered.length > limit) target.append(disclosure(id, `${title} (${ordered.length - limit}건)`, content => {
    for (const row of ordered.slice(limit)) event(content, row.title, row.detail);
  }));
}
function setBusy(value) {
  busy = value;
  byId('daytrade-view').setAttribute('aria-busy', String(value));
  byId('daytrade-refresh').disabled = value;
  byId('daytrade-sample').disabled = value || !current || current.present || !current.sample_available;
}

function renderScans(scans) {
  const target = byId('daytrade-scans');
  target.replaceChildren();
  if (!scans.length) return empty(target, '기록된 탐색 결과가 없습니다.');
  const scan = scans[scans.length - 1];
    const section = node('section', undefined, 'daytrade-scan');
    section.append(node('h3', `최신 탐색 기준: ${at(scan.at)} · 총 ${scans.length}회 탐색 수행`));
    section.append(node('p', `선정 ${scan.selected.length}종목 · 제외 ${scan.rejected.length}종목`, 'meta'));
    const selected = node('div', undefined, 'table-wrap');
    table(selected, ['선정 종목', '거래대금 / 거래량 순위', '거래대금', '매수·매도 호가차', '선정 사유'],
      scan.selected.map(row => [symbolLabel(row.symbol, row.name), `${row.rank_turnover ?? '—'} / ${row.rank_volume ?? '—'}`,
        turnoverText(row.turnover), spreadText(row.spread_bps, row.entry_blocked), reasonsFor(row.selection_reasons)]), '선정된 종목이 없습니다.');
    const reasons = new Map();
    for (const row of scan.rejected) {
      const reason = row.reason || row.reasons?.[0] || '확인 불가';
      reasons.set(reason, (reasons.get(reason) || 0) + 1);
    }
    section.append(selected, node('p', '매도1호가와 매수1호가의 가격 차이 비율 · 낮을수록 거래 비용에 유리합니다.', 'meta'),
      node('p', [...reasons].map(([reason, count]) => `${textFor(reason)} ${count}종목`).join(' · ') || '제외 종목 없음', 'meta'));
    section.append(disclosure('daytrade-exclusions', `제외 종목 상세 보기 (${scan.rejected.length}종목)`, content => {
      content.className += ' table-wrap';
      table(content, ['제외 종목', '제외 사유'], scan.rejected.map(row => [symbolLabel(row.symbol, row.name), reasonsFor(row.reasons || [row.reason])]), '제외된 종목이 없습니다.');
    }));
    target.append(section);
  const previous = scans.slice(0, -1).slice(-10).reverse();
  if (previous.length) target.append(disclosure('daytrade-prior-scans', `이전 탐색 이력 보기 (최근 ${previous.length}건 / 전체 ${scans.length}회)`, content => {
    content.className += ' table-wrap';
    table(content, ['탐색 시각', '선정 종목', '제외 수'], previous.map(item => [at(item.at), item.selected.map(row => symbolLabel(row.symbol, row.name)).join(' · '), `${item.rejected.length}종목`]), '이전 탐색 없음');
  }));
}

function render(data) {
  current = data;
  const status = byId('daytrade-status');
  byId('daytrade-error').textContent = '';
  if (!data.present) {
    byId('daytrade-flags').textContent = 'orders_enabled=false · PAPER_READY=false · KRX+NXT 통합 시세';
    byId('daytrade-cost-policy').textContent = '적용 비용은 샘플 생성 후 표시합니다. 공식 요율 확정 전 개발·검증 가정입니다.';
    status.textContent = data.sample_available ? '아직 단타 원장이 없습니다. 샘플을 한 번 실행하면 결과를 확인할 수 있습니다.' : '아직 단타 원장이 없습니다. 실행할 샘플 데이터 파일도 찾을 수 없습니다.';
    byId('daytrade-metrics').replaceChildren();
    empty(byId('daytrade-scans'), '원장이 생성되면 선정·제외 사유를 표시합니다.');
    empty(byId('daytrade-fills'), '아직 모의 체결이 없습니다.');
    empty(byId('daytrade-signals'), '아직 기록된 신호가 없습니다.');
    empty(byId('daytrade-risk'), '아직 기록된 위험 상태가 없습니다.');
    byId('daytrade-signal-summary').textContent = '';
    byId('daytrade-markdown').textContent = '아직 생성된 리포트가 없습니다.';
    return;
  }
  const report = data.report;
  symbolNames = report.symbol_names || {};
  byId('daytrade-flags').textContent = `orders_enabled=false · PAPER_READY=${report.paper_ready === true} · KRX+NXT 통합 시세`;
  const account = report.account;
  const sessions = Object.entries(report.sessions);
  const sessionText = sessions.map(([, value]) => textFor(value)).join(' · ') || '관측 중';
  const sessionDates = sessions.map(([day]) => day).join(' · ');
  byId('daytrade-mode').textContent = `${report.profile} · ${report.dataset_kind} · ${report.mode}`;
  byId('daytrade-cost-policy').textContent = `${report.cost_policy_status || '비용 정책 미확인'} · ${report.cost_policy_notice || '비용 기준 확인 불가'}`;
  status.textContent = sessions.some(([, value]) => value === 'UNFLATTENED') ?
    '미청산 보유 종목이 남아 있습니다. 실제로 관측된 체결만 반영하며 청산을 가정하지 않습니다.' :
    '저장된 로컬 결과를 표시합니다. 샘플 원장은 보존되며 다시 실행해 덮어쓸 수 없습니다.';
  byId('daytrade-metrics').replaceChildren(
    metric('초기 자본', koreanUnit(account.initial_capital), money(account.initial_capital) + ' · 단타 전용 모의 자본'),
    metric('최종 현금', koreanUnit(account.cash), money(account.cash) + ' · 매수·매도 비용 반영'),
    metric('최종 평가자산', koreanUnit(account.final_equity), money(account.final_equity) + ' · ' + (account.valuation_status === 'MARK_UNCERTAIN' ? 'MARK_UNCERTAIN · 평가 확인 불가' : '매수호가·예상 청산 비용 기준')),
    metric('총 순손익', koreanUnit(account.net_pnl, true), money(account.net_pnl, true) + ' · 비용 차감 실현 손익 ' + money(account.realized_pnl_net, true)),
    metric('수익률', numeric(account.return_pct, '%'), '초기 자본 대비 순손익'),
    metric('완결 거래', numeric(account.completed_trades, '회'), '매수부터 전량 매도까지'),
    metric('승률', numeric(account.win_rate_pct, '%'), '완결 거래만 집계'),
    metric('관측 최대 낙폭', numeric(account.mdd_pct, '%'), account.mdd_uncertain ? '평가 누락으로 MDD 확인 불가' : '기록된 평가 시점 기준'),
    metric('세션 상태', sessionText, sessionDates)
  );
  renderScans(report.scan_history || []);
  table(byId('daytrade-fills'), ['시각', '종목', '매매', '수량', '평균 체결가', '수수료 / 세금', '실현 순손익', '신호·청산 사유'],
    report.fills.map(fill => [at(fill.at), symbolLabel(fill.symbol, fill.name), textFor(fill.side), numeric(fill.quantity, '주'), money(fill.price),
      `${money(fill.fee)} / ${money(fill.tax)}`, fill.side === 'BUY' ? '매도 후 계산' : money(fill.realized_pnl, true),
      fill.signal?.exit_reason ? textFor(fill.signal.exit_reason) : reasonsFor(fill.signal?.reasons)]), '아직 모의 체결이 없습니다.');
  const signals = report.signals || [];
  byId('daytrade-signal-summary').textContent = ['BUY', 'SELL', 'HOLD'].map(action => `${textFor(action)} ${signals.filter(s => s.action === action).length}건`).join(' · ') + ' · 조건 충족률은 수익 확률이 아닙니다.';
  const signalTarget = byId('daytrade-signals');
  signalTarget.replaceChildren();
  boundedEvents(signalTarget, signals.map(signal => ({at: signal.at,
    title: `${symbolLabel(signal.symbol, signal.name)} · ${textFor(signal.action)} · 조건 충족률 ${signal.score}%`,
    detail: `${at(signal.at)} · ${signal.exit_reason ? textFor(signal.exit_reason) : reasonsFor(signal.reasons)}`})), 30, 'daytrade-prior-signals', '이전 신호 더 보기');
  if (!signals.length) empty(signalTarget, '기록된 신호가 없습니다.');
  const riskTarget = byId('daytrade-risk');
  riskTarget.replaceChildren();
  const risk = report.current_risk;
  const riskText = !risk ? '현재 상태: 확인 중' : risk.locks.length ? `현재 활성 잠금: ${reasonsFor(risk.locks)}` :
    risk.entry_allowed ? '현재 상태: 정상 (신규 진입 가능 · 종목별 조건 적용)' : `현재 상태: ${textFor(risk.phase)} · 신규 진입 조건 확인 중`;
  riskTarget.append(node('p', riskText, risk?.locks.length ? 'current-risk is-blocked' : 'current-risk'));
  const history = [
    ...(report.risk_events || []).map(item => ({at:item.at, title:textFor(item.phase), detail:`${at(item.at)} · ${item.locks.length ? reasonsFor(item.locks) : '위험 잠금 없음'}`})),
    ...(report.quality_events || []).map(item => ({at:item.at, title:`${symbolLabel(item.symbol, item.name)} · 데이터 품질 확인`, detail:`${at(item.at)} · ${reasonsFor(item.issues)}`})),
    ...(report.decisions || []).filter(item => ['SKIPPED', 'REJECTED'].includes(item.status)).map(item => ({at:item.at, title:`${symbolLabel(item.symbol, item.name)} · 실행 보류`, detail:`${at(item.at)} · ${textFor(item.reason)}`}))
  ];
  boundedEvents(riskTarget, history, 15, 'daytrade-prior-risk', '과거 품질·보류 기록 더 보기');
  if (!history.length) riskTarget.append(node('p', '기록된 위험·품질 이벤트가 없습니다.', 'empty'));
  byId('daytrade-markdown').textContent = data.markdown || '리포트 원문이 없습니다.';
}

const errors = {
  LOCAL_SESSION_REQUIRED: '세션이 만료됐습니다. 페이지를 다시 열어 주세요.',
  SESSION_AND_SAME_ORIGIN_REQUIRED: '요청의 세션과 출처를 확인할 수 없습니다. 페이지를 새로고침해 주세요.',
  DAYTRADE_SAMPLE_UNAVAILABLE: '실행할 샘플 데이터 파일을 찾을 수 없습니다.',
  DAYTRADE_REPORT_UNAVAILABLE: '단타 원장을 읽을 수 없습니다. 기존 파일을 보존하고 상태를 확인해 주세요.',
  DAYTRADE_SIMULATION_UNAVAILABLE: '샘플 실행을 완료하지 못했습니다. 기존 파일을 보존하고 상태를 확인해 주세요.',
  INVALID_INPUT_OR_STATE: '샘플 데이터 또는 요청 형식이 올바르지 않습니다.'
};
async function api(path, create = false) {
  const response = await fetch(path, create ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Paper-Action': '1'}, body: '{}'} : {});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(errors[data.error] || '요청을 완료하지 못했습니다. 잠시 후 새로고침해 주세요.');
    error.status = response.status;
    throw error;
  }
  return data;
}
async function refresh() {
  if (busy) return;
  setBusy(true);
  try { render(await api('/api/daytrade/report')); }
  catch (error) { byId('daytrade-error').textContent = error.message; }
  finally { setBusy(false); }
}
byId('daytrade-refresh').addEventListener('click', refresh);
byId('daytrade-sample').addEventListener('click', async () => {
  if (busy || !current || current.present || !current.sample_available) return;
  setBusy(true);
  byId('daytrade-status').textContent = '오프라인 샘플을 계산하고 있습니다.';
  try { render(await api('/api/daytrade/simulate-sample', true)); }
  catch (error) {
    if (error.status === 409) {
      try { render(await api('/api/daytrade/report')); }
      catch (readError) { byId('daytrade-error').textContent = readError.message; }
    } else byId('daytrade-error').textContent = error.message;
  } finally { setBusy(false); }
});
refresh();
setInterval(() => {
  if (current?.report && Object.values(current.report.sessions || {}).includes('OBSERVING')) refresh();
}, 5000);
