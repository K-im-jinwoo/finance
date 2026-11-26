export type Presence = { present: boolean; reason?: string }

export type ReportSummary = {
  report_id: string
  as_of?: string | null
  observed_at?: string | null
  price_session?: string | null
  expected_price_session?: string | null
  policy_version?: string | null
  status?: string | null
  mode?: string | null
  candidate_count?: number
  decision_counts?: Record<string, number>
}
export type ReportList = Presence & { reports?: ReportSummary[]; latest?: ReportSummary | null }

const unavailableReasons: Record<string, string> = {
  DATABASE_MISSING: '연결할 로컬 데이터베이스 파일이 없습니다.',
  DATABASE_UNAVAILABLE: '로컬 데이터베이스의 내용이나 형식을 확인할 수 없습니다.',
  REPORT_NOT_FOUND: '선택한 보고서가 저장되어 있지 않습니다.',
  DAYTRADE_SESSION_UNAVAILABLE: '선택한 날짜에 조회할 수 있는 원장이 없습니다.',
  DAYTRADE_SESSION_NOT_RECORDED: '선택한 날짜의 거래 세션이 아직 기록되지 않았습니다.',
  DAYTRADE_SESSION_DATE_MISMATCH: '선택한 날짜와 원장의 거래일이 일치하지 않습니다.',
  DAYTRADE_REPORT_UNAVAILABLE: '저장된 단타 원장을 읽을 수 없습니다.',
  DAYTRADE_SESSIONS_MISSING: '저장된 날짜별 단타 원장이 없습니다.',
  VERIFICATION_UNAVAILABLE: '저장된 검산 자료를 확인할 수 없습니다.',
}

export function unavailableReason(reason: string | undefined, fallback: string) {
  return reason ? unavailableReasons[reason] ?? fallback : fallback
}
