import { dev } from 'astro';
import node from '@astrojs/node';

const server = await dev({
  root: new URL('.', import.meta.url),
  output: 'server',
  adapter: node({ mode: 'middleware' }),
  server: { host: '127.0.0.1' },
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, async () => {
    await server.stop();
    process.exit(0);
  });
}
