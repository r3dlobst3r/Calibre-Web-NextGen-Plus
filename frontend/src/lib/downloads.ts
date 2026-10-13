/**
 * Client for Admin → Downloads (`cps/api/downloads_admin.py`): the download
 * service (Shelfmark) that runs hidden beside CWNG. Its API key stays on the
 * server; stored passwords come back blank with `hasValue` instead.
 */
import { apiGet, apiPost, apiPut } from './api';
import type { AcquisitionInstanceState } from './acquisition';
import type { SettingsTab, SettingsValues } from './downloadSettings';

export interface DownloadServiceStatus {
  /** SHELFMARK_URL and SHELFMARK_API_KEY are both set for this server. */
  environment: { configured: boolean; endpoint: string | null };
  connection: { id: string; label: string; adapter: string; enabled: boolean; revision: number; endpoint: string } | null;
  /** The service answered an authenticated request just now. */
  reachable: boolean;
  error: string | null;
  acquisition: AcquisitionInstanceState;
  /** Set by setup: book search had no provider and Open Library was switched on. */
  provider_enabled?: boolean;
}

export interface DownloadSettingsDocument {
  tabs: SettingsTab[];
  groups: { name: string; displayName: string; icon?: string | null; order?: number }[];
}

export interface DownloadSettingsResult {
  success?: boolean;
  message?: string;
  updated?: string[];
  requiresRestart?: boolean;
  [key: string]: unknown;
}

const BASE = '/api/v1/admin/downloads';

export function getDownloadService(): Promise<DownloadServiceStatus> {
  return apiGet<DownloadServiceStatus>(BASE);
}

/** Connect (or reconnect) the service. With no body the server uses its own
 *  SHELFMARK_URL / SHELFMARK_API_KEY. */
export function setUpDownloadService(body?: { endpoint: string; secret: string }): Promise<DownloadServiceStatus> {
  return apiPost<DownloadServiceStatus>(`${BASE}/setup`, body);
}

export function getDownloadSettings(): Promise<DownloadSettingsDocument> {
  return apiGet<DownloadSettingsDocument>(`${BASE}/settings`);
}

export function saveDownloadSettings(tab: string, values: SettingsValues): Promise<DownloadSettingsResult> {
  return apiPut<DownloadSettingsResult>(`${BASE}/settings/${encodeURIComponent(tab)}`, values);
}

export function runDownloadSettingsAction(tab: string, action: string, values: SettingsValues): Promise<DownloadSettingsResult> {
  return apiPost<DownloadSettingsResult>(
    `${BASE}/settings/${encodeURIComponent(tab)}/action/${encodeURIComponent(action)}`, values);
}
