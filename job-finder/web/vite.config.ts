/// <reference types="vitest/config" />
import path from "node:path"
import { fileURLToPath } from "node:url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
      // The geist package does not export its font files, so index.css reaches them through their real folder.
      "@geist-fonts": path.join(path.dirname(fileURLToPath(import.meta.resolve("geist/font"))), "fonts"),
    },
  },
  server: { proxy: { "/api": process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000" } },
  test: { environment: "jsdom", globals: true, setupFiles: "./src/test/setup.ts", css: false },
})
