import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The browser calls /api/... and Vite forwards it to the FastAPI server,
// so there are no cross-origin problems while developing.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
