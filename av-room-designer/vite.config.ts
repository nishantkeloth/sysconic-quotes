import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Base is relative ('./') so the built assets work whether this ends up
// served at the site root or under a sub-path (e.g. /av-designer/) once
// it's wired into vercel.json alongside the main index.html app.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5183,
  },
});
