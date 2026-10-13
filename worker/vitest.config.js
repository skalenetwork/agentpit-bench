import { defineWorkersConfig } from '@cloudflare/vitest-pool-workers/config';

export default defineWorkersConfig({
  test: {
    poolOptions: {
      workers: {
        singleWorker: true,
        wrangler: { configPath: './wrangler.toml' },
        miniflare: {
          // Test-only values; production secrets are set with `wrangler secret put`.
          bindings: {
            GOOGLE_CLIENT_ID: 'test-client.apps.googleusercontent.com',
            SESSION_SECRET: 'test-session-secret-0123456789abcdef',
            BENCH_INGEST_KEY: 'test-bench-key',
          },
        },
      },
    },
  },
});
