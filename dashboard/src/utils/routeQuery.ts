import { useEffect, useState } from 'react'

export function useRouteQuery() {
  const [query, setQuery] = useState(() => new URLSearchParams(window.location.search))
  useEffect(() => {
    const sync = () => setQuery(new URLSearchParams(window.location.search))
    window.addEventListener('popstate', sync)
    return () => window.removeEventListener('popstate', sync)
  }, [])

  function updateQuery(changes: Record<string, string | null>) {
    const url = new URL(window.location.href)
    for (const [key, value] of Object.entries(changes)) {
      if (value) url.searchParams.set(key, value)
      else url.searchParams.delete(key)
    }
    window.history.replaceState(window.history.state, '', url)
    setQuery(new URLSearchParams(url.search))
  }

  return { query, updateQuery }
}
