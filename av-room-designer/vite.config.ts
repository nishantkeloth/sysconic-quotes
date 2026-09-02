import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Base is an absolute sub-path ('/av-room-designer/') because this app is
// deployed alongside the main QTcal index.html under that path (see
// vercel.json's routes). A relative base ('./') looked appealing for
// portability, but it breaks the moment someone visits the URL WITHOUT a
// trailing slash (e.g. /av-room-designer, which is exactly how App tool
// links and typed-in URLs look): the browser resolves './assets/...'
// against the parent directory (site root) instead of this app's own
// directory, so asset requests 404 into the main app's catch-all route and
// get served text/html instead of JS, which fails with a MIME-type error.
// An absolute base sidesteps that regardless of trailing slash.
export default defineConfig({
  plugins: [react()],
  base: '/av-room-designer/',
  server: {
    port: 5183,
  },
});
