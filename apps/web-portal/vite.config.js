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
    // antd and echarts are inherently large singletons; after manualChunks
    // splits them into cacheable vendor chunks the largest is echarts-vendor
    // (~1 MB). Raise the warning limit so the build does not cry wolf over
    // intentional, cacheable vendor chunks — the app code chunk itself is
    // ~23 kB.
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        // Code-split the vendor libs so the main app bundle stays under the
        // 500 kB warning limit. react/react-dom, antd/@ant-design, and echarts
        // each land in their own cacheable chunk.
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('react-dom') || /node_modules\/react\//.test(id)) {
              return 'react-vendor'
            }
            if (id.includes('@ant-design') || id.includes('/antd/')) {
              return 'antd-vendor'
            }
            if (id.includes('echarts') || id.includes('zrender')) {
              return 'echarts-vendor'
            }
          }
        },
      },
    },
  },
})
