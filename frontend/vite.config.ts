import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build version, injected so the running page can tell when a newer build
// is live and reload itself (#331). Railway sets RAILWAY_GIT_COMMIT_SHA; an
// explicit APP_VERSION wins. "dev" locally, which disables the check.
const APP_VERSION =
  process.env.APP_VERSION || process.env.RAILWAY_GIT_COMMIT_SHA || 'dev'

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
  },
  server: {
    port: 3000,
  },
})
