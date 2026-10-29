import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies the API (incl. WebSocket) to a running app on :8080.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { chunkSizeWarningLimit: 1000 }, // single local app; one bundle is fine
  server: {
    proxy: {
      "/api": { target: "http://localhost:8080", ws: true, changeOrigin: true },
    },
  },
});
