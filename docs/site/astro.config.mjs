// @ts-check
import { defineConfig } from "astro/config";

// `SITE_BASE` is the URL path prefix at which the site is served. The
// two slots (prod at `/mat-vis/`, tst at `/mat-vis/tst/`) override it
// from CI; locally the default matches the prod path so a plain
// `npm run build && http-server dist -p 8080` reproduces the deployed
// shape at `http://localhost:8080/mat-vis/`.
const SITE_BASE = process.env.SITE_BASE ?? "/mat-vis/";

export default defineConfig({
  site: "https://morepet.github.io",
  base: SITE_BASE,
  output: "static",
  build: {
    format: "directory",
  },
  vite: {
    build: {
      target: "es2022",
    },
  },
});
