import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  root: resolve(__dirname),
  base: "/static/web-ui/",
  build: {
    outDir: resolve(__dirname, "../../tile_server/static/web-ui"),
    emptyOutDir: true,
  },
});
