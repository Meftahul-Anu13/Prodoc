import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 120_000,
  outputDir: '../eval/reports/browser',
  reporter: [['list'], ['json', { outputFile: '../eval/reports/browser-results.json' }]],
  use: {
    baseURL: 'http://127.0.0.1:5173',
    channel: 'chrome',
    viewport: { width: 1360, height: 900 },
    video: 'on',
    trace: 'off',
    screenshot: 'only-on-failure',
  },
});
