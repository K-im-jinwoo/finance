import { useResource } from '../api/client'
import { unavailableReason } from '../api/phaseB'
import { AppLink } from '../components/AppLink'
import { Badge, Empty, Panel, Pnl, ResourceState, Skeleton, Table } from '../components/ui'
import { compactDate, finite, kst, money, monthDay, numeric, percent } from '../utils/formatters'
import { evidenceText, labelForCode } from '../utils/labels'
import type { DaemonSummary, Holdings, NewsBudget, OpsData, OpsVerification } from './opsData'

const labels: Record<string, string> = {
  DRAFT: '초안', APPROVED: '승인', ACTIVE: '유효', REVIEW_REQUIRED: '재검토 필요', INVALIDATED: '무효화',
  FULL_EXIT_REVIEW: '전량 청산 검토', FULL_OR_PARTIAL_EXIT_REVIEW: '전량 또는 일부 청산 검토',
  PARTIAL_PROFIT_REVIEW: '일부 익절 검토', HOLD_REMAINDER_WITH_INVALIDATION: '잔여분 무효화 조건 관리',
  DO_NOT_AVERAGE_DOWN_WITHOUT_REVALIDATION: '재검증 전 추가 매수 보류',
  HOLD_WITH_REVIEW_CONDITIONS: '확인 조건을 지켜보며 보유', NEW_BUY_HOLD: '신규 매수 보류',
  UNKNOWN: '확인 불가', READY: '대기 해제', ACTIVE_COOLDOWN: '쿨다운 중', COOLDOWN: '쿨다운 중',
  WAITING: '대기 중', BLOCKED: '차단', AVAILABLE: '사용 가능', HALTED: '일시 중지',
}

export function statusLabel(value?: string | null) {
  return value ? labels[value] ?? labelForCode(value) : '확인 불가'
}

function yesNo(value?: boolean | null, yes = '예', no = '아니요') {
  return value == null ? '확인 불가' : value ? yes : no
}

function portfolioReturn(value: unknown) {
  const ratio = finite(value)
  return ratio === null ? null : ratio * 100
}

function DetailsList({ title, items }: { title: string; items?: string[] }) {
  return items?.length ? <section className="stack"><h3>{title}</h3><ul className="evidence-list">
    {items.map((item, index) => <li key={index}>{evidenceText(item)}</li>)}
  </ul></section> : null
}

function Portfolio({ value }: { value?: Holdings }) {
  return <Panel title="보유 현황">
    {!value?.present
      ? <Empty>{unavailableReason(value?.reason, '확인할 수 있는 로컬 보유 자료가 없습니다.')}</Empty>
      : <>

        {value.positions?.length ? <Table label="브로커 합산 보유" columns={['종목', '합산 수량', '가중평균 취득가', '브로커']}>
          {value.positions.map(row => <tr key={row.symbol}><th scope="row" className="mono">{row.symbol}</th>
            <td className="mono">{numeric(row.quantity, '주')}</td><td className="mono">{money(row.average_price)}</td>
            <td>{row.brokers.join(' · ') || '확인 불가'}</td></tr>)}
        </Table> : <Empty>등록된 보유 종목 0개</Empty>}
        {!!value.items?.length && <details><summary>브로커별 원본 보유 ({value.items.length}건)</summary>
          <Table label="브로커별 원본 보유" columns={['브로커', '종목', '수량', '평균 취득가', '취득일']}>
            {value.items.map(row => <tr key={row.broker + ':' + row.symbol}>
              <th scope="row">{row.broker}</th><td className="mono">{row.symbol}</td>
              <td className="mono">{numeric(row.quantity, '주')}</td><td className="mono">{money(row.average_price)}</td>
              <td className="mono">{row.acquired_on ?? '확인 불가'}</td></tr>)}
          </Table>
        </details>}
        {!!value.theses?.length && <section className="stack">
          <h3>투자 논리</h3>
          {value.theses.map(row => <details key={row.thesis_id}>
            <summary>{row.symbol} · {statusLabel(row.status)}</summary>
            <p className="muted">{kst(row.created_at)}</p>
            <DetailsList title="확인한 사실" items={row.facts} />
            <DetailsList title="가정" items={row.assumptions} />
            <DetailsList title="무효화 조건" items={row.invalidation_conditions} />
            <DetailsList title="추가 확인 조건" items={row.additional_check_conditions} />
          </details>)}
        </section>}
        {!!value.reviews?.length && <section className="stack">
          <h3>보유 검토</h3><ul className="evidence-list">
            {value.reviews.map(row => <li key={row.symbol} className="stack">
              <div className="cluster"><strong className="mono">{row.symbol}</strong>
                <Badge>{row.present === false ? '검토 보류' : statusLabel(row.thesis_status)}</Badge>
                {row.current_price != null && <span className="mono">{money(row.current_price)}</span>}
                {row.return_rate != null && <Pnl value={portfolioReturn(row.return_rate)} format={percent} />}
                {row.price_date && <span className="muted">가격 기준 {row.price_date}</span>}
              </div>
              {row.present === false && <p className="muted">{unavailableReason(row.reason, '저장된 가격·투자 논리·판정 근거가 부족합니다.')}</p>}
              {row.actions?.length ? <p>{row.actions.map(statusLabel).join(' · ')}</p> : null}
              {row.reasons?.length ? <p className="muted">{row.reasons.map(reason => evidenceText(reason)).join(' · ')}</p> : null}
              {row.additional_checks?.length ? <details><summary>추가 확인</summary>
                <ul>{row.additional_checks.map((item, index) => <li key={index}>{evidenceText(item)}</li>)}</ul>
              </details> : null}
            </li>)}
          </ul>
        </section>}
      </>}
  </Panel>
}

function BudgetGauge({ id, label, used, limit }: {
  id: string; label: string; used?: number | null; limit?: number | null
}) {
  const limitKnown = limit != null && limit > 0
  const usageKnown = used != null && used >= 0
  return <div className="stack ops-gauge">
    <div className="section-header">
      <label htmlFor={limitKnown && usageKnown ? id : undefined}>{label} 한도</label>
      <strong className="amount">{numeric(limit, '회')}</strong>
    </div>
    {usageKnown && <p className="muted">사용량 <span className="amount">{numeric(used, '회')}</span></p>}
    {limitKnown && usageKnown && <progress id={id} max={limit} value={Math.min(limit, used)} />}
  </div>
}

function NewsUsage({ value }: { value?: NewsBudget }) {
  const dayBlocked = value?.day_blocked ?? value?.blocked_today ??
    (value?.daily_used != null && value.daily_limit != null && value.daily_limit > 0 ? value.daily_used >= value.daily_limit : null)
  const monthBlocked = value?.month_blocked ?? value?.blocked_month ??
    (value?.monthly_used != null && value.monthly_limit != null && value.monthly_limit > 0 ? value.monthly_used >= value.monthly_limit : null)
  return <Panel title="뉴스 수집 한도">
    {(!value?.present || (value.daily_used == null && value.monthly_used == null)) &&
      <div><Badge>사용량 확인 불가</Badge></div>}
    {(value?.present || value?.daily_limit != null || value?.monthly_limit != null) && <>
      <BudgetGauge id="daily-news-budget" label="일" used={value.daily_used} limit={value.daily_limit} />
      <BudgetGauge id="monthly-news-budget" label="월" used={value.monthly_used} limit={value.monthly_limit} />
    </>}
    {value?.present && (dayBlocked || monthBlocked || value.provider_halted) && <div className="cluster">
      {dayBlocked && <Badge tone="warn">일 한도 도달</Badge>}
      {monthBlocked && <Badge tone="warn">월 한도 도달</Badge>}
      {value.provider_halted && <Badge tone="warn">제공자 차단 있음</Badge>}
    </div>}
    {!!value?.present && !!value.cooldowns && <details><summary>수집 대기 시간</summary>
      <Table label="뉴스 수집 대기 시간" columns={['수집 구분', '상태', '최근 예약', '다음 가능 시각']}>
        {['DEFAULT', 'SWING'].map(scope => {
          const row = value.cooldowns?.[scope]
          return <tr key={scope}><th scope="row">{labelForCode(scope)}</th>
            <td>{row?.status === 'READY' ? '수집 가능' : row?.status === 'COOLDOWN' ? '쿨다운 중' : labelForCode(row?.status)}</td>
            <td className="amount">{kst(row?.last_reserved_at)}</td><td className="amount">{kst(row?.available_at)}</td></tr>
        })}
      </Table>
    </details>}
  </Panel>
}

function Verification({ value }: { value?: OpsVerification }) {
  const tables = value?.oracle_inventory?.tables
  const ohlcv = tables?.ohlcv
  const financial = tables?.financial_snapshots
  const lastCheck = monthDay(value?.checked_at_kst)
  const isHistorical = Boolean(lastCheck && compactDate(value?.checked_at_kst) !== compactDate(new Date().toISOString()))
  const warning = /INCOMPLETE|BLOCKED/.test(value?.data_status ?? '')
  return <Panel title="데이터 수집 현황 및 검산">
    {!value || value.present === false ? <Empty>{unavailableReason(value?.reason, '저장된 검산 자료가 없습니다.')}</Empty> : <>
      <div className="cluster">
        {warning ? <Badge tone="warn">{labelForCode(value.data_status)}</Badge>
          : <strong>{labelForCode(value.data_status, '검산 상태 확인 불가')}</strong>}
        {lastCheck && <span className="muted">마지막 검산 <time dateTime={value.checked_at_kst}>{lastCheck}</time>
          {isHistorical && ' · 과거 기록'}</span>}
      </div>
      <dl className="data-pairs">
        <dt>거래소 일봉</dt><dd className="amount">{numeric(ohlcv?.symbols, '종목')} · {numeric(ohlcv?.distinct_dates, '일')}</dd>
        <dt>일봉 기간</dt><dd className="amount">{ohlcv?.trade_date_range?.every(Boolean) && ohlcv.trade_date_range.length
          ? ohlcv.trade_date_range.join(' ~ ') : '확인 불가'}</dd>
        <dt>공시 재무</dt><dd className="amount">{numeric(financial?.symbols, '종목')}</dd>
      </dl>
    </>}
  </Panel>
}

function DaytradeStatus({ value }: { value?: DaemonSummary }) {
  return <Panel title="단타 자동 수집기" actions={<AppLink className="action-link" href="/daytrade">당일 단타 보기</AppLink>}>
    {!value?.present ? <Empty>{unavailableReason(value?.reason, '저장된 단타 상태가 없습니다.')}</Empty> : <>
      <div className="cluster">
        <strong className="ops-lead" data-active={value.status === 'OBSERVING'}>{labelForCode(value.status)}</strong>
        {value.at && <time className="muted" dateTime={value.at}>{kst(value.at)}</time>}
      </div>
      <div><Badge tone={value.entry_blocked === true ? 'warn' : 'unknown'}>
        {value.entry_blocked == null ? '신규 진입 여부 확인 불가'
          : yesNo(value.entry_blocked, '신규 진입 차단', '신규 진입 차단 표시 없음')}
      </Badge></div>
      {value.unclosed_positions != null && <p className="muted">미청산 보유 <span className="amount">{numeric(value.unclosed_positions, '종목')}</span></p>}
    </>}
  </Panel>
}

function SystemDetails({ daytrade, verification }: { daytrade?: DaemonSummary; verification?: OpsVerification }) {
  const prices = verification?.actual_price_validation?.results
  const financial = verification?.actual_financial_validation
  if (!daytrade?.present && !prices?.length && !financial) return null

  return <details className="system-details">
    <summary>시스템 상세</summary>
    <div className="stack">
      {daytrade?.present && <>
        <h3>단타 수집 기록</h3>
        <dl className="data-pairs">
          <dt>시세 연결 · 수신 건수</dt><dd className="amount">{numeric(daytrade.active_sockets)} · {numeric(daytrade.ws_frames)}</dd>
          <dt>오류 기록</dt><dd>{labelForCode(daytrade.error_code, '표시된 오류 없음')}</dd>
          <dt>알림 상태</dt><dd>{labelForCode(daytrade.notification_status)}</dd>
          <dt>시세 인증 상태</dt><dd>{labelForCode(daytrade.token_status)}
            {daytrade.token_valid_until && ' · ' + kst(daytrade.token_valid_until)}</dd>
        </dl>
      </>}
      {prices?.length ? <Table label="보관 가격 검산" columns={['종목', '자료 기간', '계산 기준일', '독립 계산 일치']}>
        {prices.map((entry, index) => <tr key={index}><th scope="row" className="mono">{entry.symbol}</th>
          <td className="amount">{entry.start} ~ {entry.end}</td><td>{numeric(entry.calculation_dates_checked, '일')}</td>
          <td>{yesNo(entry.source_features_equal_independent_formulas, '일치', '불일치')}</td></tr>)}
      </Table> : null}
      {financial && <details><summary>재무 검산 {financial.symbol} · {yesNo(financial.all_equal, '일치', '불일치')}</summary>
        <p className="muted">{kst(financial.observed_at)} · {labelForCode(financial.division)}</p>
        <Table label="재무 검산 항목" columns={['검산 항목', '일치 여부']}>
          {Object.entries(financial.checks ?? {}).map(([name, match]) => <tr key={name}>
            <th scope="row">{labelForCode(name, '확인 항목')}</th><td>{yesNo(match, '일치', '불일치')}</td>
          </tr>)}
        </Table>
      </details>}
    </div>
  </details>
}

export default function Ops() {
  const { data, loading, error } = useResource<OpsData>('/api/ops/overview')
  return <div className="stack page-content ops-page">
    <header className="page-heading"><h1>운영·포트폴리오</h1></header>
    <ResourceState loading={false} error={error} />
    {loading && <Skeleton label="운영 상태 불러오는 중" />}
    {data && <>
      {!data.present && <p className="notice">{unavailableReason(data.reason, '조회할 로컬 운영 자료가 없습니다.')}</p>}
      <DaytradeStatus value={data.daytrade} />
      <div className="workspace-grid ops-grid"><Portfolio value={data.holdings} /><NewsUsage value={data.news_budget} /></div>
      <Verification value={data.verification} />
      <SystemDetails daytrade={data.daytrade} verification={data.verification} />
    </>}
  </div>
}
