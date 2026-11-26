import { readFile } from 'node:fs/promises'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const apiTarget = 'http://127.0.0.1:8765'
const spaPaths = new Set(['/', '/hermes', '/daytrade', '/experiments', '/research', '/swing', '/ops'])

export default defineConfig({
  base: '/',
  plugins: [
    react(),
    {
      name: 'finance-woff2-only',
      enforce: 'pre',
      async load(id) {
        if (!/[/\\]@fontsource[/\\].*\.css$/.test(id)) return
        const css = await readFile(id, 'utf8')
        // The Python asset allowlist supports WOFF2. Drop the legacy Fontsource fallback
        // before Vite resolves URLs and attaches them to the output manifest.
        return css.replace(/,\s*url\([^)]*\.woff["']?\)\s*format\(["']?woff["']?\)/g, '')
      },
    },
    {
      name: 'finance-dev-paper-session',
      apply: 'serve',
      configureServer(server) {
        server.middlewares.use(async (request, response, next) => {
          const path = request.url?.split('?')[0] ?? ''
          if (request.method === 'GET' && spaPaths.has(path) &&
              request.headers['sec-fetch-site'] !== 'cross-site') {
            try {
              const bootstrap = await fetch(new URL('/daytrade', apiTarget), {
                signal: AbortSignal.timeout(1500),
                headers: request.headers.cookie ? { Cookie: request.headers.cookie } : undefined,
              })
              const cookie = bootstrap.headers.get('set-cookie')
              if (cookie) response.setHeader('Set-Cookie', cookie)
              await bootstrap.body?.cancel()
            } catch {
              // The UI will report an unavailable Python server.
            }
          }
          next()
        })
      },
    },
  ],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (forwarded, incoming) => {
            const host = incoming.headers.host
            const isLoopback = /^(127\.0\.0\.1|localhost):\d+$/.test(host ?? '')
            if (isLoopback && incoming.headers.origin === ('http://' + host) &&
                incoming.headers['sec-fetch-site'] !== 'cross-site') {
              forwarded.setHeader('Origin', apiTarget)
            }
          })
        },
      },
    },
  },
  build: {
    outDir: '../web/dist',
    emptyOutDir: true,
    assetsInlineLimit: 0,
    reportCompressedSize: false,
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
