import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export type Capabilities = {
  read_only: boolean
  orders_enabled: false
  telegram_delivery_enabled: false
  routes: string[]
  phase_b_available: boolean
  daytrade_sessions_available?: boolean
  fixture_mode?: 'SYNTHETIC'
}

export type PollInterval = 5 | 30 | 0

const errors: Record<string, string> = {
  LOCAL_SESSION_REQUIRED: '로컬 세션이 없거나 만료됐습니다. 페이지를 다시 열어 주세요.',
  SESSION_AND_SAME_ORIGIN_REQUIRED: '세션과 요청 출처를 확인할 수 없습니다. 페이지를 다시 열어 주세요.',
  LONGTERM_LEDGER_UNAVAILABLE: '장기 모의계좌 원장을 읽을 수 없습니다.',
  LONGTERM_SOURCE_UNAVAILABLE: '장기 모의계좌 원장 또는 입력 자료를 열 수 없습니다.',
  NOT_FOUND: '요청한 기록을 찾을 수 없습니다.',
  BODY_TOO_LARGE: '요청이 허용된 크기를 넘었습니다.',
  INVALID_INPUT_OR_STATE: '입력 또는 현재 상태에서는 이 작업을 실행할 수 없습니다.',
  DAYTRADE_SAMPLE_UNAVAILABLE: '실행할 샘플 데이터 파일을 찾을 수 없습니다.',
  DAYTRADE_REPORT_UNAVAILABLE: '단타 원장을 읽을 수 없습니다. 원장 상태를 확인해 주세요.',
  DAYTRADE_SIMULATION_UNAVAILABLE: '샘플 실행을 완료하지 못했습니다. 원장 상태를 확인해 주세요.',
  DAYTRADE_LEDGER_EXISTS: '이미 단타 원장이 있습니다. 저장된 결과를 불러옵니다.',
  OBSERVED_RUN_REQUIRES_DATA_GATE_REVIEW: '관측 실험 실행에는 데이터 검토가 필요합니다.',
}

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code: string) {
    super(errors[code] ?? '요청을 완료하지 못했습니다. 서버 상태를 확인한 뒤 새로고침해 주세요.')
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!path.startsWith('/api/')) throw new Error('대시보드 API 경로가 올바르지 않습니다.')
  const response = await fetch(path, { credentials: 'same-origin', ...init })
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, String(payload?.error ?? ''))
  if (payload === null) throw new Error('서버 응답을 읽을 수 없습니다.')
  return payload as T
}

export function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal })
}

export function postJson<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Paper-Action': '1' },
    body: JSON.stringify(body),
  })
}

type Listener = () => void
type Polling = {
  interval: PollInterval
  setInterval: (value: PollInterval) => void
  refresh: Listener
  subscribe: (listener: Listener) => Listener
  onSuccess: Listener
  checkedAt: string | null
}
const PollContext = createContext<Polling | null>(null)
const CapabilityContext = createContext<Capabilities | undefined>(undefined)
const EvidenceContext = createContext<(mode: string | null) => void>(() => {})

export function PollProvider({ children, defaultInterval }: { children: ReactNode; defaultInterval: PollInterval }) {
  const listeners = useRef(new Set<Listener>())
  const [preferredInterval, setInterval] = useState<PollInterval | null>(null)
  const interval = preferredInterval ?? defaultInterval
  const [checkedAt, setCheckedAt] = useState<string | null>(null)
  const refresh = useCallback(() => listeners.current.forEach(listener => listener()), [])
  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener)
    return () => { listeners.current.delete(listener) }
  }, [])
  const onSuccess = useCallback(() => setCheckedAt(new Date().toISOString()), [])

  useEffect(() => {
    if (interval === 0) return
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden') refresh()
    }, interval * 1000)
    return () => window.clearInterval(timer)
  }, [interval, refresh])

  const value = useMemo(() => ({ interval, setInterval, refresh, subscribe, onSuccess, checkedAt }),
    [interval, refresh, subscribe, onSuccess, checkedAt])
  return <PollContext.Provider value={value}>{children}</PollContext.Provider>
}

export function usePolling() {
  const polling = useContext(PollContext)
  if (!polling) throw new Error('Polling provider is required.')
  return polling
}

export function useRefresh(): Listener {
  return usePolling().refresh
}

type Resource<T> = { data: T | undefined; error: string | null; loading: boolean }

export function useResource<T>(path: string | null): Resource<T> {
  const { subscribe, onSuccess } = usePolling()
  const [state, setState] = useState<{ path: string; value: Resource<T> } | null>(null)

  useEffect(() => {
    if (path === null) return
    const controller = new AbortController()
    let running = false
    let queued = false
    const refresh = async () => {
      if (controller.signal.aborted) return
      if (running) {
        queued = true
        return
      }
      running = true
      try {
        const data = await getJson<T>(path, controller.signal)
        if (controller.signal.aborted) return
        setState({ path, value: { data, error: null, loading: false } })
        if (path !== '/api/capabilities') onSuccess()
      } catch (error) {
        if (controller.signal.aborted) return
        const message = error instanceof ApiError ? error.message : '대시보드 서버에 연결할 수 없습니다. 서버 상태를 확인해 주세요.'
        setState(previous => ({ path, value: {
          data: previous?.path === path ? previous.value.data : undefined, error: message, loading: false,
        } }))
      } finally {
        running = false
        if (queued) {
          queued = false
          void refresh()
        }
      }
    }
    const unsubscribe = subscribe(refresh)
    void refresh()
    return () => { unsubscribe(); controller.abort() }
  }, [path, subscribe, onSuccess])

  return state?.path === path ? state.value : { data: undefined, error: null, loading: path !== null }
}

export function CapabilitiesProvider({ children, value }: { children: ReactNode; value: Capabilities | undefined }) {
  return <CapabilityContext.Provider value={value}>{children}</CapabilityContext.Provider>
}

export function useCapabilities() {
  return useContext(CapabilityContext)
}

export function EvidenceProvider({ children, onChange }: { children: ReactNode; onChange: (mode: string | null) => void }) {
  return <EvidenceContext.Provider value={onChange}>{children}</EvidenceContext.Provider>
}

export function useEvidenceMode(mode: string | null | undefined) {
  const update = useContext(EvidenceContext)
  useEffect(() => {
    update(mode ?? null)
    return () => update(null)
  }, [mode, update])
}

export function evidenceMode(value: string | null | undefined): string {
  if (value === 'OBSERVATION') return 'OBSERVED'
  if (value === 'SYNTHETIC' || value === 'OBSERVED' || value === 'LIVE_RECORDED') return value
  return 'UNKNOWN'
}
