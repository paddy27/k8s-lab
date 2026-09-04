import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Base "" (relative) so the built assets work when served from FastAPI
// at any path, not just the domain root.
export default defineConfig({
  plugins: [react()],
  base: "",
  build: {
    outDir: "dist",
  },
});
