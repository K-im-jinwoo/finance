import { Activity, CandlestickChart, FlaskConical, House, Menu, Moon, PanelLeftClose, PanelLeftOpen, RefreshCw, Search, Settings, Sun, TrendingUp } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { MouseEvent } from 'react'
import { CapabilitiesProvider, EvidenceProvider, PollProvider, evidenceMode, usePolling, useResource } from './api/client'
import type { Capabilities, PollInterval } from './api/client'
import { Badge, Drawer, ResourceState } from './components/ui'
import Daytrade from './pages/Daytrade'
import Experiments from './pages/Experiments'
import Hermes from './pages/Hermes'
import Overview from './pages/Overview'
import { kst } from './utils/formatters'

const routes = [
  { path: '/', label: '통합 홈', compact: '홈', icon: House, page: Overview },
  { path: '/hermes', label: 'Hermes 모의계좌', compact: 'Hermes', icon: Activity, page: Hermes },
  { path: '/daytrade', label: '당일 단타', compact: '단타', icon: CandlestickChart, page: Daytrade },
  { path: '/experiments', label: '계산 검증 실험', compact: '실험', icon: FlaskConical, page: null },
  { path: '/research', label: '통합 리서치 & 차트', compact: '리서치', icon: Search, page: null },
  { path: '/swing', label: '테마·스윙 & 성과', compact: '스윙', icon: TrendingUp, page: null },
  { path: '/ops', label: '운영·포트폴리오', compact: '운영', icon: Settings, page: null },
]
const planned: Record<string, string> = {
  '/research': '통합 리서치 보고서, 판정별 종목 목록과 일봉 차트를 연결할 예정입니다.',
  '/swing': '테마 촉매, 스윙 후보와 5·20·60거래일 사후 성과를 연결할 예정입니다.',
  '/ops': '수동 보유, 뉴스 API 예산과 Oracle 데이터 커버리지를 연결할 예정입니다.',
}

export function canonicalLocation(url: URL) {
  if (url.pathname === '/' && url.searchParams.has('experiment')) return '/experiments' + url.search + url.hash
  return url.pathname + url.search + url.hash
}

function initialPath() {
  const url = new URL(window.location.href)
  const canonical = canonicalLocation(url)
  if (canonical !== url.pathname + url.search + url.hash) window.history.replaceState(null, '', canonical)
  return window.location.pathname
}

function preference(key: string, fallback: string) {
  try { return window.localStorage.getItem(key) ?? fallback }
  catch { return fallback }
}

function savePreference(key: string, value: string) {
  try { window.localStorage.setItem(key, value) }
  catch { /* The current tab still keeps its selected preference. */ }
}

function Workspace({ path, openPath }: { path: string; openPath: (path: string) => void }) {
  const { data: capability, error, loading } = useResource<Capabilities>('/api/capabilities')
  const { interval, setInterval, refresh, checkedAt } = usePolling()
  const [theme, setTheme] = useState(() => preference('finance.theme', 'dark') === 'light' ? 'light' : 'dark')
  const [pnl, setPnl] = useState(() => preference('finance.pnl', 'krx') === 'global' ? 'global' : 'krx')
  const [compact, setCompact] = useState(false)
  const [menu, setMenu] = useState(false)
  const [evidence, setEvidence] = useState<string | null>(null)
  const main = useRef<HTMLElement>(null)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#080C14' : '#F4F6F3')
    savePreference('finance.theme', theme)
  }, [theme])
  useEffect(() => {
    document.documentElement.dataset.pnl = pnl
    savePreference('finance.pnl', pnl)
  }, [pnl])

  const active = routes.find(route => route.path === path)
  const readOnly = capability?.read_only === true
  const allowedRoutes = capability ? routes.filter(route => capability.routes.includes(route.path)) : []
  const pageAllowed = !capability || capability.routes.includes(path)
  const Page = path === '/experiments' ? Experiments : active?.page
  const mode = readOnly ? 'READ_ONLY' : evidenceMode(evidence)

  useEffect(() => {
    document.title = (active?.label ?? '페이지 안내') + ' · Finance'
  }, [active])

  function navigate(event: MouseEvent<HTMLAnchorElement>, href: string) {
    if (event.button !== 0 || event.altKey || event.ctrlKey || event.shiftKey || event.metaKey) return
    event.preventDefault()
    openPath(href)
    setMenu(false)
    requestAnimationFrame(() => {
      window.scrollTo(0, 0)
      main.current?.focus()
    })
  }

  function navItems(items: typeof routes, mobile = false) {
    return items.map(({ path: href, label, compact: short, icon: Icon }) =>
      <a key={href} href={href} onClick={event => navigate(event, href)}
        aria-current={href === path ? 'page' : undefined} aria-label={label} title={label}>
        <Icon size={19} strokeWidth={1.5} aria-hidden="true" />
        <span>{mobile ? short : label}</span>
      </a>)
  }

  return <CapabilitiesProvider value={capability}>
    <EvidenceProvider onChange={setEvidence}>
      <a className="skip-link" href="#dashboard-main">본문으로 건너뛰기</a>
      <div className={compact ? 'app app-collapsed' : 'app'}>
        <aside className="sidebar" aria-label="사이드바">
          <div className="brand">
            <Activity size={21} aria-hidden="true" /><span>Finance</span>
            <button className="icon-button collapse-button" aria-label={compact ? '사이드바 펼치기' : '사이드바 접기'}
              aria-expanded={!compact} onClick={() => setCompact(!compact)}>
              {compact ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
            </button>
          </div>
          <nav className="primary-nav" aria-label="대시보드 메뉴">{navItems(allowedRoutes)}</nav>
        </aside>
        <div className="app-main">
          <header className="global-bar">
            <div className="cluster global-context">
              {!readOnly && <button className="icon-button mobile-menu" aria-label="메뉴 열기" aria-expanded={menu}
                disabled={!allowedRoutes.length} onClick={() => setMenu(true)}><Menu size={20} /></button>}
              <span className="mobile-brand">Finance</span>
              <Badge tone="ok">실주문 차단 <span className="badge-extra">(PAPER ONLY)</span></Badge>
              <Badge>{mode}</Badge>
              {readOnly && evidenceMode(evidence) !== 'UNKNOWN' && <Badge>{evidenceMode(evidence)}</Badge>}
            </div>
            <div className="cluster global-tools">
              <span className="last-check mono" aria-live="off">{checkedAt ? kst(checkedAt) : '확인 대기'}</span>
              <label className="visually-hidden" htmlFor="poll-interval">자동 새로고침 주기</label>
              <select id="poll-interval" value={interval} onChange={event => setInterval(Number(event.target.value) as PollInterval)}>
                <option value={5}>5초</option><option value={30}>30초</option><option value={0}>일시정지</option>
              </select>
              <button className="icon-button" aria-label="수동 새로고침" onClick={refresh}><RefreshCw size={18} /></button>
              <button aria-label="글로벌 녹색 상승·적색 하락 적용" aria-pressed={pnl === 'global'}
                onClick={() => setPnl(pnl === 'krx' ? 'global' : 'krx')}>{pnl === 'krx' ? 'KRX 적/청' : '글로벌 녹/적'}</button>
              <button className="icon-button" aria-label={theme === 'dark' ? '라이트 테마로 전환' : '다크 테마로 전환'}
                onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>
                {theme === 'dark' ? <Sun size={19} /> : <Moon size={19} />}
              </button>
            </div>
          </header>
          <main id="dashboard-main" ref={main} tabIndex={-1} className="page">
            <ResourceState loading={loading} error={error} />
            {!pageAllowed && <><h1>지원하지 않는 화면</h1><p>이 서버는 당일 단타 읽기 전용 화면을 제공합니다.</p><a href="/daytrade">당일 단타 열기</a></>}
            {pageAllowed && Page && <Page />}
            {pageAllowed && !Page && <>
              <h1>{active?.label ?? '페이지를 찾을 수 없습니다'}</h1>
              {active && <div className="notice stack"><Badge tone="warn">Phase B 준비 중</Badge><p>{planned[path]}</p></div>}
              {!active && <a href="/">통합 홈으로 이동</a>}
            </>}
          </main>
        </div>
        {allowedRoutes.length > 1 && <nav className="mobile-tabs" aria-label="주요 화면">
          {navItems(allowedRoutes.filter(route => ['/', '/hermes', '/daytrade', '/research'].includes(route.path)), true)}
          <button onClick={() => setMenu(true)} aria-expanded={menu}><Menu size={19} aria-hidden="true" />더보기</button>
        </nav>}
        {menu && <Drawer title="대시보드 메뉴" onClose={() => setMenu(false)}>
          <nav className="primary-nav" aria-label="전체 메뉴">{navItems(allowedRoutes)}</nav>
        </Drawer>}
      </div>
    </EvidenceProvider>
  </CapabilitiesProvider>
}

export default function App() {
  const [path, setPath] = useState(initialPath)

  useEffect(() => {
    const handlePop = () => setPath(initialPath())
    window.addEventListener('popstate', handlePop)
    return () => window.removeEventListener('popstate', handlePop)
  }, [])

  function openPath(destination: string) {
    if (destination === window.location.pathname) return
    window.history.pushState(null, '', destination)
    setPath(destination)
  }

  return <PollProvider defaultInterval={path === '/daytrade' ? 5 : 30}>
    <Workspace path={path} openPath={openPath} />
  </PollProvider>
}
