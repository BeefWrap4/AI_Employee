import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite config for the AI Employee web portal.
// Dev proxy routes API calls to the local backend services so the SPA
// can talk to knowledge-api / rca-agent / agent-platform-api / tool-registry
// without CORS configuration.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api/knowledge': {
        target: 'http://127.0.0.1:8010',
        rewrite: (p) => p.replace(/^\/api\/knowledge/, ''),
      },
      '/api/rca': {
        target: 'http://127.0.0.1:8020',
        rewrite: (p) => p.replace(/^\/api\/rca/, ''),
      },
      '/api/platform': {
        target: 'http://127.0.0.1:8030',
        rewrite: (p) => p.replace(/^\/api\/platform/, ''),
      },
      '/api/tools': {
        target: 'http://127.0.0.1:8040',
        rewrite: (p) => p.replace(/^\/api\/tools/, ''),
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
