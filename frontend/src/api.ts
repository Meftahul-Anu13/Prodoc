export interface User {
    id: string;
    email: string;
    role: string;
}
export interface DocumentRecord {
    id: string;
    filename: string;
    status: string;
    created_at?: string;
    page_count?: number | null;
    error_code?: string | null;
    metadata?: Record<string, unknown>;
}
export interface Invoice {
    id: string;
    version: number;
    data: Record<string, unknown>;
    review_status: string;
}
export interface Results {
    document: DocumentRecord;
    invoices: Invoice[];
    pages: unknown[];
    events: unknown[];
}
export interface Reconciliation {
    id: string;
    status: string;
    differences?: unknown;
    [key: string]: unknown;
}
let token: string | null = null;
let unauthorized: () => void = () => { };
export function setToken(value: string | null) { token = value; }
export function onUnauthorized(callback: () => void) { unauthorized = callback; }
export class ApiError extends Error {
    constructor(message: string, public code: string, public requestId?: string, public status?: number) { super(message); }
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (token)
        headers.set('Authorization', `Bearer ${token}`);
    if (init.body && !(init.body instanceof FormData))
        headers.set('Content-Type', 'application/json');
    const response = await fetch(`/api/v1${path}`, { ...init, headers });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
        if (response.status === 401 && path !== '/auth/login') {
            setToken(null);
            unauthorized();
        }
        ;
        throw new ApiError(body?.error?.message || `Request failed (${response.status})`, body?.error?.code || 'REQUEST_FAILED', body?.request_id, response.status);
    }
    return body as T;
}
export const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) });
export async function apiBlob(path: string): Promise<Blob> {
    const headers = new Headers();
    if (token)
        headers.set('Authorization', `Bearer ${token}`);
    const response = await fetch(`/api/v1${path}`, { headers });
    if (!response.ok)
        throw new ApiError('File is not available.', 'file_unavailable', undefined, response.status);
    return response.blob();
}
export function display(value: unknown): string { if (value === null || value === undefined)
    return '—'; if (typeof value === 'object')
    return JSON.stringify(value, null, 2); return String(value); }
export function isPending(status: string) { return ['queued', 'pending', 'processing', 'uploaded', 'running', 'retrying', 'reconciling', 'extracting', 'ocr', 'validating'].includes(status.toLowerCase()); }
