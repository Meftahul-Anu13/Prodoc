import { test, expect } from '@playwright/test';
import path from 'node:path';
import fs from 'node:fs';

// Drives the real browser/API/worker/DB. No mocked HTTP and no credentials in files/traces.
test('upload, evidence, correction, search and reconciliation walkthrough', async ({ page }) => {
  const password = process.env.DEMO_PASSWORD;
  if (!password) throw new Error('Use python -m scripts.demo_server to supply an ephemeral test login');
  const consoleErrors: string[] = [];
  page.on('pageerror', err => consoleErrors.push(err.message));
  await page.goto('/');
  await page.getByLabel('Email address').fill(process.env.DEMO_EMAIL!);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your documents' })).toBeVisible();
  const csv = 'invoice_number,invoice_date,supplier_name,customer_name,subtotal,tax,total,currency\nINV-DEMO,2026-01-01,Acme Ltd,Buyer Ltd,100.00,10.00,110.00,USD\n';
  await page.getByLabel('Upload documents', { exact: true }).setInputFiles([
    { name: 'demo-invoice.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) },
    { name: 'demo-mismatch.csv', mimeType: 'text/csv', buffer: Buffer.from(csv.replace('110.00', '111.00')) },
    ...['pdf', 'png', 'jpg', 'tiff', 'xlsx', 'xls'].map(extension => ({
      name: `family-0-clean.${extension}`, mimeType: 'application/octet-stream',
      buffer: fs.readFileSync(path.resolve('..', 'eval', 'data', 'test', `family-0-clean.${extension}`)),
    })),
  ]);
  await expect(page.locator('table tbody tr')).toHaveCount(8);
  await expect(page.locator('table tbody tr').filter({ hasText: 'needs review' })).toHaveCount(8, { timeout: 100_000 });
  await page.screenshot({ path: '../eval/reports/browser-queue.png', fullPage: true });
  await page.getByRole('button', { name: 'Open demo-invoice.csv', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Extracted invoice' })).toBeVisible();
  await expect(page.getByLabel('total', { exact: true })).toHaveValue('110.00');
  await page.getByRole('button', { name: 'evidence', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Source evidence' })).toBeVisible();
  await page.screenshot({ path: '../eval/reports/browser-evidence.png', fullPage: true });
  await page.getByRole('button', { name: 'extraction', exact: true }).click();
  // A monetary edit proves the browser/API decimal-string contract, not just text corrections.
  await page.getByLabel('total', { exact: true }).fill('110.000');
  await page.getByLabel('Reason for correction').fill('Verified source total and decimal representation');
  await page.getByRole('button', { name: 'Save corrections', exact: true }).click();
  await expect(page.getByText('Version 2', { exact: false })).toBeVisible();
  await expect(page.getByLabel('total', { exact: true })).toHaveValue('110.000');
  await page.getByRole('button', { name: 'audit', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Audit trail' })).toBeVisible();
  await expect(page.getByText('corrected', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await page.getByLabel('Search query').fill('INV-DEMO');
  await page.getByRole('button', { name: 'Search', exact: true }).last().click();
  await expect(page.locator('table tbody tr')).toHaveCount(2);
  await page.getByRole('radio', { name: /Semantic/ }).check();
  await page.getByRole('button', { name: 'Search', exact: true }).last().click();
  await expect(page.getByText(/keyword_fallback/)).toBeVisible();
  await page.getByRole('button', { name: 'Reconciliation', exact: true }).click();
  await expect(page.getByLabel('Left invoice')).toBeEnabled();
  const left = page.getByLabel('Left invoice');
  const right = page.getByLabel('Right invoice');
  const leftValue = await left.locator('option').filter({ hasText: /^demo-invoice.csv/ }).getAttribute('value');
  const rightValue = await right.locator('option').filter({ hasText: /^demo-mismatch.csv/ }).getAttribute('value');
  await left.selectOption(leftValue!);
  await right.selectOption(rightValue!);
  await page.getByRole('button', { name: 'Run reconciliation' }).click();
  await expect(page.getByRole('heading', { name: 'Reconciliation result' })).toBeVisible();
  // A human-corrected total is an assertion, not source evidence; conservative reconciliation may refuse it.
  await expect(page.locator('.badge').filter({ hasText: /mismatch|insufficient evidence/ }).first()).toBeVisible();
  await page.screenshot({ path: '../eval/reports/browser-reconciliation.png', fullPage: true });
  expect(consoleErrors).toEqual([]);
});
