import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.JEV_API_URL ?? "http://127.0.0.1:8000";
const wsBackend = backend.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/healthz": { target: backend },
      "/readyz": { target: backend },
      "/ws": { target: wsBackend, ws: true },
    },
  },
});
