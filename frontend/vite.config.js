import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true
  },
  server: {
    host: "127.0.0.1",
    port: 8792,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8791",
        changeOrigin: true
      }
    }
  },
  preview: {
    host: "127.0.0.1",
    port: 8793
  }
});
