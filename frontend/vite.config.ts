import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// В разработке API проксируется на backend (uvicorn на :8000)
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
