import { compactDate, kst } from './formatters'

const labels: Record<string, string> = {
  RISK_HISTORY_COVERAGE_UNAVAILABLE: '저장된 위험 이력 범위 확인 불가',
  UNKNOWN: '확인 불가', OBSERVING: '장중 모니터링', LIVE_RECORDED: '실시간 기록',
  OBSERVED: '실제 시세', OBSERVATION: '실제 시세', SYNTHETIC: '가상 검증',
  LIVE: '실시간 장중', REPLAY: '과거 재현', PAPER_ONLY: '모의투자 전용', READ_ONLY: '조회 전용',
  APPROVED_CONSERVATIVE_V1: '보수형 단타 전략', SWING_CANDIDATE: '스윙 후보',
  SWING_THEME_V1: '테마 모멘텀 전략', UNIFIED_RESEARCH_V1: '통합 리서치 기준',
  SYNTHETIC_AUDIT_V1: '가상 검증 기준', DEFAULT: '장기 리서치', SWING: '테마·스윙',
  READY: '확정', COMPLETE: '완료', COMPLETED: '완료', RUNNING: '운영 중', PAUSED: '일시중지', FAILED: '실패',
  VALID: '유효', INVALID: '무효', EXPIRING: '만료 임박', EXPIRED: '만료',
  AVAILABLE: '사용 가능', UNAVAILABLE: '확인 불가', RESERVED: '발송 예약', SENT: '발송 완료',
  BLOCKED: '차단', HALTED: '일시 중지', ERROR: '오류', WAITING: '대기 중', COOLDOWN: '대기 시간 적용 중',
  ACTIVE_COOLDOWN: '대기 시간 적용 중', CHECKED: '검산 완료',
  THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY: '3년 백테스트 자료 일부 부족',
  BLOCKED_NO_POINT_IN_TIME_DATASET: '3년 백테스트 검증 자료 미확보',
  RESTART_LOCKED: '재시작 잠금', STATE_STORE_UNAVAILABLE: '상태 기록 확인 불가',
  MARKET_HOLIDAY_OR_NON_STANDARD_SESSION: '휴장 또는 특별 거래일',
  STOPPED_BEFORE_OPEN: '개장 전 중지', AFTER_MARKET_NO_COLLECTION: '장 마감 후 수집 종료',
  ALREADY_CLOSED: '이미 마감', CLOSED_WITHOUT_COLLECTION: '수집 없이 마감',
  STOPPED: '중지됨', CLOSED: '마감', ALREADY_RUNNING: '이미 운영 중',
  AUTH: '인증 오류', NETWORK: '연결 오류', STATE: '상태 기록 오류', DATA: '데이터 오류',
  BUY_RECOMMENDATION: '매수 추천', WATCH: '관찰', VERIFY_PENDING: '검증 보류',
  EXCLUDED: '제외', NOT_APPLICABLE: '평가 대상 아님', BUY: '매수', SELL: '매도', HOLD: '대기',
  MOMENTUM: '모멘텀', MOMENTUM_CONTINUATION: '모멘텀 지속', BOTTOM_REBOUND: '바닥 반등',
  WAIT_FOR_SETUP: '진입 조건 대기', BREAKOUT: '돌파', PULLBACK: '눌림목',
  SCORED: '점수 반영', VERIFIED_ACTIVE: '검증 완료', NOT_SCORED: '점수 미반영',
  CONSOLIDATED: '연결재무제표', SEPARATE: '별도재무제표', CFS: '연결재무제표', OFS: '별도재무제표',
  SHORT_TERM: '단기', MEDIUM_TERM: '중기', LONG_TERM: '장기',
  'SYNTHETIC PROFIT': '검증용 익절 종목', 'SYNTHETIC TIME EXIT': '검증용 시간 청산 종목',
  SIMULATED_FILL: '모의 체결', OBSERVATION_ONLY: '시세 관측만 수행', FIXTURE_ASSUMPTION: '샘플 비용 가정',
  V1: '기본 기준', PAPER: '모의 거래', PAPER_FIXTURE: '모의 거래 샘플', MARK_UNCERTAIN: '평가 확인 불가',
  INTEGRATED_KR: '국내 통합 시세', NEXT_QUOTE: '다음 호가 기준', SIMULATED: '모의 체결',
  COST_POLICY_UNKNOWN: '비용 기준 확인 불가', REVIEW_REQUIRED: '검토 필요',
  PRICE_BREAKDOWN: '가격 기준 이탈', GENERAL_FINANCIAL: '일반 재무 조건', DISCLOSURE: '공시 검토',
  NEGATIVE_DISCLOSURE: '부정적 공시', NEWS_REVERSAL: '뉴스 근거 약화',
  PENDING: '대기', REJECTED: '제외', SKIPPED: '건너뜀', UNKNOWN_EVIDENCE: '근거 확인 불가',
  UNFLATTENED: '미청산 보유', PRIORITY_REVIEW: '우선 검토', BUY_HOLD: '신규 매수 보류',
  original_annual_income_equal: '원문 연간 순이익',
  original_annual_ocf_equal: '원문 연간 영업현금흐름',
  original_interim_cumulative_equal: '원문 중간 누적 실적',
  previous_comparative_equal_separate_prior_year_report: '비교 전기 실적과 전년도 공시 대조',
  original_ttm_formula_equal: '원문 최근 12개월 계산식',
  stored_annual_income_equal: '저장된 연간 순이익',
  stored_ttm_income_equal: '저장된 최근 12개월 순이익',
  stored_operating_income_equal: '저장된 영업이익',
  stored_annual_ocf_equal: '저장된 연간 영업현금흐름',
  stored_periods_equal: '저장된 회계 기간',
  stored_receipt_links_equal: '저장된 공시 원문 링크',
}

export function labelForCode(value?: string | null, fallback = '확인 불가'): string {
  return value && Object.hasOwn(labels, value) ? labels[value] : fallback
}

export function evidenceLabel(value?: string | null): string | undefined {
  return ['LIVE_RECORDED', 'OBSERVED', 'OBSERVATION', 'SYNTHETIC'].includes(value ?? '')
    ? labelForCode(value) : undefined
}

export function evidenceText(value?: string | null, fallback = '확인 불가'): string {
  if (!value?.trim()) return fallback
  if (Object.hasOwn(labels, value)) return labels[value]
  return /^[A-Za-z][A-Za-z0-9_.:-]*$/.test(value) ? fallback : readableLog(value)
}

// Archived notes can contain raw identifiers mixed into otherwise readable prose.
export function readableLog(value: string, lookup = labelForCode) {
  return value
    .replace(/\b(?:UR|SW|E)-[A-Za-z0-9-]+\b/g, '저장된 기록')
    .replace(/\borders_enabled=false\s*·\s*PAPER_READY=(?:true|false)\s*·\s*/g, '')
    .replace(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, code => lookup(code))
    .replace(/\b(?:BUY|SELL|HOLD|PAPER|LIVE|REPLAY|OBSERVATION|OBSERVED|OBSERVING|SYNTHETIC|READY|UNKNOWN|VALID|INVALID|REJECTED|SKIPPED|CLOSED|V1)\b/g, code => lookup(code))
}

type ReportIdentity = { report_id: string; as_of?: string | null; signal_date?: string | null; price_session?: string | null }
type ReportKind = 'research' | 'swing' | 'performance'

function reportDate(report: ReportIdentity) {
  const idDate = /^R-(\d{4})(\d{2})(\d{2})T/.exec(report.report_id)
  return compactDate(report.as_of ?? report.signal_date ?? report.price_session ??
    (idDate ? idDate.slice(1, 4).join('-') : undefined))
}

export function reportName(report: ReportIdentity, kind: ReportKind, peers: readonly ReportIdentity[] = []) {
  const name = { research: '장기 리서치 보고서', swing: '테마·스윙 보고서', performance: '성과 평가 보고서' }[kind]
  const date = reportDate(report)
  if (!date) return name
  const sameDay = peers.some(other => other.report_id !== report.report_id && reportDate(other) === date)
  const time = sameDay && report.as_of ? kst(report.as_of).split(' ')[1] : undefined
  return date + ' ' + name + (time ? ' (' + time + ')' : '')
}
