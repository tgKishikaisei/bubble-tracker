import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Proxy: перенаправляем /api запросы на FastAPI при локальной разработке (npm run dev)
  // В Docker — проксирование делает Nginx (см. bubble-dashboard-ui/Dockerfile)
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // WebSocket /api/ws/stats тоже через прокси (без ws: true дашборд падал обратно на опрос)
        ws: true,
      },
    },
  },
})
