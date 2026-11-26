import { Activity, CandlestickChart, FlaskConical, House, Menu, Moon, PanelLeftClose, PanelLeftOpen, RefreshCw, Search, Settings, Sun, TrendingUp } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { CapabilitiesProvider, EvidenceProvider, PollProvider, evidenceMode, usePolling, useResource } from './api/client'
import type { Capabilities, PollInterval } from './api/client'
import { AppLink, AppNavigation } from './components/AppLink'
import { Badge, Drawer, PageErrorBoundary, ResourceState } from './components/ui'
import Daytrade from './pages/Daytrade'
import Experiments from './pages/Experiments'
import Hermes from './pages/Hermes'
import Overview from './pages/Overview'
import Research from './pages/Research'
import Swing from './pages/Swing'
import Ops from './pages/Ops'
import { kst } from './utils/formatters'

const routes = [
  { path: '/', label: '통합 홈', compact: '홈', icon: House, page: Overview },
  { path: '/hermes', label: 'Hermes 모의계좌', compact: 'Hermes', icon: Activity, page: Hermes },
  { path: '/daytrade', label: '당일 단타', compact: '단타', icon: CandlestickChart, page: Daytrade },
  { path: '/experiments', label: '계산 검증 실험', compact: '실험', icon: FlaskConical, page: Experiments },
  { path: '/research', label: '통합 리서치 & 차트', compact: '리서치', icon: Search, page: Research },
  { path: '/swing', label: '테마·스윙 & 성과', compact: '스윙', icon: TrendingUp, page: Swing },
  { path: '/ops', label: '운영·포트폴리오', compact: '운영', icon: Settings, page: Ops },
]
const phaseBRoutes = new Set(['/research', '/swing', '/ops'])

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

function Workspace({ path, openPath }: { path: string; openPath: (href: string) => boolean }) {
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
  const phaseBUnavailable = phaseBRoutes.has(path) && capability?.phase_b_available === false
  const Page = phaseBUnavailable ? null : active?.page
  const multiTrack = path === '/' || evidence === 'MULTI-TRACK'
  const mode = readOnly ? 'READ_ONLY' : multiTrack ? 'MULTI-TRACK' : evidenceMode(evidence)
  const pnlLabel = pnl === 'krx' ? 'KRX 적/청' : '글로벌 녹/적'
  const nextPnlLabel = pnl === 'krx' ? '글로벌 녹/적' : 'KRX 적/청'

  useEffect(() => {
    document.title = (active?.label ?? '페이지 안내') + ' · Finance'
  }, [active])

  function navigate(href: string) {
    const moved = openPath(href)
    setMenu(false)
    if (!moved) return
    requestAnimationFrame(() => {
      window.scrollTo(0, 0)
      main.current?.focus()
    })
  }

  function navItems(items: typeof routes, mobile = false) {
    return items.map(({ path: href, label, compact: short, icon: Icon }) =>
      <AppLink key={href} href={href}
        aria-current={href === path ? 'page' : undefined} aria-label={label} title={label}>
        <Icon size={19} strokeWidth={1.5} aria-hidden="true" />
        <span>{mobile ? short : label}</span>
      </AppLink>)
  }

  return <AppNavigation.Provider value={navigate}>
    <CapabilitiesProvider value={capability}>
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
                {capability?.fixture_mode === 'SYNTHETIC' && <Badge tone="warn">감사용 합성 데이터</Badge>}
                {readOnly && evidenceMode(evidence) !== 'UNKNOWN' && <Badge>{evidenceMode(evidence)}</Badge>}
              </div>
              <div className="cluster global-tools">
                <span className="last-check mono" aria-live="off">{checkedAt ? kst(checkedAt) : '확인 대기'}</span>
                <label className="visually-hidden" htmlFor="poll-interval">자동 새로고침 주기</label>
                <select id="poll-interval" value={interval} onChange={event => setInterval(Number(event.target.value) as PollInterval)}>
                  <option value={5}>5초</option><option value={30}>30초</option><option value={0}>일시정지</option>
                </select>
                <button className="icon-button" aria-label="수동 새로고침" onClick={refresh}><RefreshCw size={18} /></button>
                <button aria-label={pnlLabel + ' (' + nextPnlLabel + '으로 전환)'} aria-pressed={pnl === 'global'}
                  onClick={() => setPnl(pnl === 'krx' ? 'global' : 'krx')}>{pnlLabel}</button>
                <button className="icon-button" aria-label={theme === 'dark' ? '라이트 테마로 전환' : '다크 테마로 전환'}
                  onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>
                  {theme === 'dark' ? <Sun size={19} /> : <Moon size={19} />}
                </button>
              </div>
            </header>
            <main id="dashboard-main" ref={main} tabIndex={-1} className="page">
              <ResourceState loading={loading} error={error} />
              {!pageAllowed && <><h1>지원하지 않는 화면</h1><p>이 서버는 당일 단타 읽기 전용 화면을 제공합니다.</p><AppLink className="action-link" href="/daytrade">당일 단타 열기</AppLink></>}
              {pageAllowed && Page && <PageErrorBoundary key={path}><Page /></PageErrorBoundary>}
              {pageAllowed && !Page && <>
                <h1>{active?.label ?? '페이지를 찾을 수 없습니다'}</h1>
                {phaseBUnavailable && <p className="notice">이 서버에서 읽기 전용 자료 API를 사용할 수 없습니다.</p>}
                {!active && <AppLink className="action-link" href="/">통합 홈으로 이동</AppLink>}
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
  </AppNavigation.Provider>
}

export default function App() {
  const [path, setPath] = useState(initialPath)

  useEffect(() => {
    const handlePop = () => setPath(initialPath())
    window.addEventListener('popstate', handlePop)
    return () => window.removeEventListener('popstate', handlePop)
  }, [])

  function openPath(href: string) {
    const current = new URL(window.location.href)
    const destination = new URL(canonicalLocation(new URL(href, current)), current)
    const samePage = destination.pathname === current.pathname
    if (destination.href === current.href || (samePage && !destination.search && !destination.hash)) return false
    window.history.pushState(null, '', destination)
    if (samePage) window.dispatchEvent(new PopStateEvent('popstate'))
    else setPath(destination.pathname)
    return true
  }

  return <PollProvider defaultInterval={path === '/daytrade' ? 5 : 30}>
    <Workspace path={path} openPath={openPath} />
  </PollProvider>
}
