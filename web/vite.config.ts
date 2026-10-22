import path from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Not a VITE_ variable on purpose: the backend address stays in the dev server, never in
// browser code.
const apiTarget = process.env.INQUEST_API_URL ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, 'src') },
  },
  server: {
    proxy: {
      // /api/* -> backend /*. The MCP mount is excluded: it is for investigator runtimes
      // only and must never be reachable from the browser.
      '^/api/(?!mcp(/|$))': {
        target: apiTarget,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: true,
    restoreMocks: true,
    unstubGlobals: true,
  },
})
