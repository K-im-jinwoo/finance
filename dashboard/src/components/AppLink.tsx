import { createContext, useContext } from 'react'
import type { ComponentPropsWithoutRef } from 'react'

export const AppNavigation = createContext<((href: string) => void) | null>(null)

export function AppLink({ href, onClick, ...props }: ComponentPropsWithoutRef<'a'> & { href: string }) {
  const navigate = useContext(AppNavigation)
  return <a {...props} href={href} onClick={event => {
    onClick?.(event)
    if (!navigate || event.defaultPrevented || event.button !== 0 || event.altKey || event.ctrlKey ||
      event.shiftKey || event.metaKey || (props.target && props.target !== '_self') ||
      event.currentTarget.hasAttribute('download') || !href.startsWith('/') ||
      new URL(event.currentTarget.href).origin !== window.location.origin) return
    event.preventDefault()
    navigate(href)
  }} />
}
