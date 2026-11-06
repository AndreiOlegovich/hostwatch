import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` talks to a running container on the default port.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8766" } },
});
