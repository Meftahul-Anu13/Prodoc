import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { setToken, onUnauthorized } from './api';
afterEach(() => { cleanup(); setToken(null); onUnauthorized(() => { }); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
