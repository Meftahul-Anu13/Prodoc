// Read-only inspection of a local signed-in session. Credentials are environment-only.
import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs';
const require = createRequire(path.resolve('frontend/package.json'));
const { chromium } = require('@playwright/test');
const email = process.env.INSPECT_EMAIL;
const password = process.env.INSPECT_PASSWORD;
if (!email || !password) throw new Error('Set INSPECT_EMAIL and INSPECT_PASSWORD in the process environment');
const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:5173');
  await page.getByLabel('Email address').fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  const loginResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/auth/login'));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  const login = await loginResponse;
  if (!login.ok()) throw new Error('Login failed: HTTP ' + login.status());
  const session = await login.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  await page.getByRole('heading', { name: 'Your documents' }).waitFor();
  const response = await page.request.get('http://127.0.0.1:8000/api/v1/documents', { headers });
  if (!response.ok()) throw new Error('Document listing failed: HTTP ' + response.status());
  const listing = await response.json();
  const documents = listing.items ?? listing.documents ?? listing;
  if (!Array.isArray(documents)) throw new Error('Unexpected document-list schema');
  const csvs = documents.filter(document => document.filename.toLowerCase().endsWith('.csv'));
  console.log(JSON.stringify({ csv_documents: csvs.map(({id, filename, status}) => ({id, filename, status})) }, null, 2));
  for (const document of csvs) {
    const result = await page.request.get(`http://127.0.0.1:8000/api/v1/documents/${document.id}/results`, { headers });
    if (!result.ok()) throw new Error('Results failed: HTTP ' + result.status());
    const data = await result.json();
    console.log(JSON.stringify({ filename: document.filename, metadata: data.document.metadata, invoices: data.invoices.map(invoice => ({ id: invoice.id, version: invoice.version, data: invoice.data })), pages: data.pages }, null, 2));
  }
  if (csvs.length) {
    await page.getByRole('button', { name: `Open ${csvs[0].filename}`, exact: true }).first().click();
    await page.getByRole('heading', { name: 'Extracted invoice' }).waitFor();
    fs.mkdirSync('var/inspection', { recursive: true });
    await page.screenshot({ path: 'var/inspection/csv-before.png', fullPage: true });
  }
  console.log(JSON.stringify({ uncaught_browser_errors: errors }));
} finally {
  await browser.close();
}
