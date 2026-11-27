import { defineConfig } from 'astro/config';
import node from '@astrojs/node';

export default defineConfig({
  output: process.env.DAYFRAME_SITE_MODE === 'service' ? 'server' : 'static',
  adapter: process.env.DAYFRAME_SITE_MODE === 'service' ? node({ mode: 'middleware' }) : undefined,
  build: { format: 'directory' },
  server: { host: '127.0.0.1' },
});
