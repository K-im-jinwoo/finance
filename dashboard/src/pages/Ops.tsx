import { useResource } from '../api/client'
import { unavailableReason } from '../api/phaseB'
import { AppLink } from '../components/AppLink'
import { Badge, Empty, Panel, Pnl, ResourceState, Table } from '../components/ui'
import { finite, kst, money, numeric, percent } from '../utils/formatters'
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
  return value ? labels[value] ?? value : '확인 불가'
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
    {items.map((item, index) => <li key={index}>{item}</li>)}
  </ul></section> : null
}

function Portfolio({ value }: { value?: Holdings }) {
  return <Panel title="수동 보유·투자 논리">
    {!value?.present
      ? <Empty>{unavailableReason(value?.reason, '확인할 수 있는 로컬 보유 자료가 없습니다.')}</Empty>
      : <>
        <p className="muted">저장된 수동 보유와 투자 논리입니다. 검토 의견은 주문으로 연결되지 않습니다.</p>
        {value.positions?.length ? <Table label="브로커 합산 보유" columns={['종목', '합산 수량', '가중평균 취득가', '브로커']}>
          {value.positions.map(row => <tr key={row.symbol}><th scope="row" className="mono">{row.symbol}</th>
            <td className="mono">{numeric(row.quantity, '주')}</td><td className="mono">{money(row.average_price)}</td>
            <td>{row.brokers.join(' · ') || '확인 불가'}</td></tr>)}
        </Table> : <Empty>저장된 수동 보유 종목이 없습니다.</Empty>}
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
            <p className="muted">{kst(row.created_at)} · {row.thesis_id}</p>
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
              {row.reasons?.length ? <p className="muted">{row.reasons.join(' · ')}</p> : null}
              {row.additional_checks?.length ? <details><summary>추가 확인</summary>
                <ul>{row.additional_checks.map((item, index) => <li key={index}>{item}</li>)}</ul>
              </details> : null}
            </li>)}
          </ul>
        </section>}
      </>}
  </Panel>
}

function BudgetGauge({ id, label, used, limit, period }: {
  id: string; label: string; used?: number | null; limit?: number | null; period?: string
}) {
  const limitKnown = limit != null && limit > 0
  const usageKnown = used != null && used >= 0
  return <div className="stack ops-gauge">
    <div className="section-header">
      <label htmlFor={limitKnown && usageKnown ? id : undefined}>{label}{period ? ' · ' + period : ''}</label>
      <strong className="mono">{numeric(used)} / {numeric(limit, '회')}</strong>
    </div>
    {limitKnown && usageKnown && <progress id={id} max={limit} value={Math.min(limit, used)} />}
  </div>
}

function NewsUsage({ value }: { value?: NewsBudget }) {
  const dayBlocked = value?.day_blocked ?? value?.blocked_today ??
    (value?.daily_used != null && value.daily_limit != null ? value.daily_used >= value.daily_limit : null)
  const monthBlocked = value?.month_blocked ?? value?.blocked_month ??
    (value?.monthly_used != null && value.monthly_limit != null ? value.monthly_used >= value.monthly_limit : null)
  return <Panel title="네이버 뉴스 API 예산">
    {!value?.present ? <Empty>{unavailableReason(value?.reason, '저장된 뉴스 사용량을 확인할 수 없습니다.')}</Empty> : <>
      <BudgetGauge id="daily-news-budget" label="일 사용량" used={value.daily_used} limit={value.daily_limit} period={value.day} />
      <BudgetGauge id="monthly-news-budget" label="월 사용량" used={value.monthly_used} limit={value.monthly_limit} period={value.month} />
      <div className="cluster">
        <Badge tone={dayBlocked === true ? 'danger' : 'unknown'}>일 한도 {yesNo(dayBlocked, '도달', '이내')}</Badge>
        <Badge tone={monthBlocked === true ? 'danger' : 'unknown'}>월 한도 {yesNo(monthBlocked, '도달', '이내')}</Badge>
        <Badge tone={value.provider_halted === true ? 'danger' : 'unknown'}>제공자 차단 {yesNo(value.provider_halted, '있음', '없음')}</Badge>
        <Badge>무료 정책 {statusLabel(value.policy_status)}</Badge>
      </div>
      <Table label="뉴스 수집 쿨다운" columns={['수집 트랙', '상태', '최근 예약', '다음 가능 시각']}>
        {['DEFAULT', 'SWING'].map(scope => {
          const row = value.cooldowns?.[scope]
          return <tr key={scope}><th scope="row" className="mono">{scope}</th><td>{statusLabel(row?.status)}</td>
            <td>{kst(row?.last_reserved_at)}</td><td>{kst(row?.available_at)}</td></tr>
        })}
      </Table>
    </>}
  </Panel>
}

function Verification({ value }: { value?: OpsVerification }) {
  const tables = value?.oracle_inventory?.tables
  const ohlcv = tables?.ohlcv
  const financial = tables?.financial_snapshots
  const financialCheck = value?.actual_financial_validation
  const prices = value?.actual_price_validation?.results
  return <Panel title="Oracle 데이터 커버리지·검산">
    {!value || value.present === false ? <Empty>{unavailableReason(value?.reason, '저장된 커버리지 자료가 없습니다.')}</Empty> : <>
      <div className="cluster">
        <Badge tone={value.data_status && /INCOMPLETE|BLOCKED/.test(value.data_status) ? 'warn' : 'unknown'}>
          {value.data_status ?? '커버리지 확인 불가'}
        </Badge>
        {value.mode === 'SYNTHETIC' && <Badge>감사용 합성 자료</Badge>}
        <span className="muted">{kst(value.checked_at_kst)}</span>
      </div>
      {/INCOMPLETE|BLOCKED/.test(value.data_status ?? '') && <p>3년 시점 일치 자료를 확보하지 못했습니다.</p>}
      <dl className="data-pairs">
        <dt>KRX 일봉</dt><dd className="mono">{numeric(ohlcv?.symbols, '종목')} · {numeric(ohlcv?.distinct_dates, '일')}</dd>
        <dt>일봉 기간</dt><dd className="mono">{ohlcv?.trade_date_range?.every(Boolean) && ohlcv.trade_date_range.length
          ? ohlcv.trade_date_range.join(' ~ ') : '확인 불가'}</dd>
        <dt>DART 재무</dt><dd className="mono">{numeric(financial?.symbols, '종목')}</dd>
      </dl>
      {prices?.length ? <Table label="보관 가격 검산" columns={['종목', '자료 기간', '계산 기준일', '독립 계산 일치']}>
        {prices.map((entry, index) => <tr key={index}><th scope="row" className="mono">{entry.symbol}</th>
          <td>{entry.start} ~ {entry.end}</td><td>{numeric(entry.calculation_dates_checked, '일')}</td>
          <td>{yesNo(entry.source_features_equal_independent_formulas, '일치', '불일치')}</td></tr>)}
      </Table> : <Empty>저장된 가격 검산 결과가 없습니다.</Empty>}
      {financialCheck ? <details><summary>재무 검산 {financialCheck.symbol} · {yesNo(financialCheck.all_equal, '일치', '불일치')}</summary>
        <p>{kst(financialCheck.observed_at)} · {financialCheck.division}</p>
        <Table label="재무 검산 항목" columns={['검산 항목', '일치 여부']}>
          {Object.entries(financialCheck.checks ?? {}).map(([name, match]) => <tr key={name}>
            <th scope="row">{name}</th><td>{yesNo(match, '일치', '불일치')}</td></tr>)}
        </Table>
      </details> : <Empty>저장된 재무 검산 결과가 없습니다.</Empty>}
    </>}
  </Panel>
}

export function RuntimeSettings({ value }: { value: NonNullable<DaemonSummary['live_settings']> }) {
  if (!Object.keys(value).length) return null
  return <details><summary>저장된 런타임 설정</summary>
    <dl className="data-pairs">{Object.entries(value).flatMap(([section, setting]) =>
      (setting !== null && typeof setting === 'object'
        ? Object.entries(setting).map(([key, field]) => [section + '.' + key, field] as const)
        : [[section, setting] as const]).map(([name, field]) =>
        <div className="settings-pair" key={name}><dt>{name}</dt><dd className="mono">
          {field == null ? '확인 불가' : typeof field === 'boolean' ? yesNo(field, '사용', '미사용') : String(field)}
        </dd></div>))}</dl>
  </details>
}

function DaytradeStatus({ value }: { value?: DaemonSummary }) {
  return <Panel title="단타 데몬" actions={<AppLink className="action-link" href="/daytrade">세션 원장 열기</AppLink>}>
    {!value?.present ? <Empty>{unavailableReason(value?.reason, '저장된 단타 데몬 상태가 없습니다.')}</Empty> : <>
      <div className="cluster"><Badge>{statusLabel(value.status)}</Badge><span className="muted">{kst(value.at)}</span></div>
      <dl className="data-pairs">
        <dt>신규 진입</dt><dd>{yesNo(value.entry_blocked, '차단', '차단 표시 없음')}</dd>
        <dt>미청산 보유</dt><dd>{numeric(value.unclosed_positions, '종목')}</dd>
        <dt>활성 소켓 / 프레임</dt><dd>{numeric(value.active_sockets)} / {numeric(value.ws_frames)}</dd>
        <dt>오류 코드</dt><dd className="mono">{value.error_code ?? '표시된 코드 없음'}</dd>
        <dt>알림 상태</dt><dd>{statusLabel(value.notification_status)}</dd>
        <dt>공유 토큰 상태</dt><dd>{statusLabel(value.token_status)}{value.token_valid_until && ' · ' + kst(value.token_valid_until)}</dd>
      </dl>
      {value.live_settings && <RuntimeSettings value={value.live_settings} />}
    </>}
  </Panel>
}

export default function Ops() {
  const resource = useResource<OpsData>('/api/ops/overview')
  const data = resource.data
  return <div className="stack page-content">
    <header className="page-heading"><h1>운영·포트폴리오</h1><p>수동 보유, 뉴스 비용 한도와 보관된 운영 상태를 조회합니다.</p></header>
    <ResourceState loading={resource.loading} error={resource.error} />
    {data && <>
      {!data.present && <p className="notice">{unavailableReason(data.reason, '조회할 로컬 운영 자료가 없습니다.')}</p>}
      <Portfolio value={data.holdings} />
      <div className="workspace-grid"><NewsUsage value={data.news_budget} /><DaytradeStatus value={data.daytrade} /></div>
      <Verification value={data.verification} />
    </>}
  </div>
}
