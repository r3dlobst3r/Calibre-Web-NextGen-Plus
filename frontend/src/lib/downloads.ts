/**
 * Client for Admin → Downloads (`cps/api/downloads_admin.py`): the downloader
 * built into this server, which searches Anna's Archive, LibGen and AudiobookBay
 * and hands downloads to configured clients. Stored passwords come back blank
 * with `hasValue` instead.
 */
import { apiGet, apiPatch, apiPost, apiPut } from './api';
import type { AcquisitionInstanceState } from './acquisition';
import type { SettingsTab, SettingsValues } from './downloadSettings';

export type MetadataProvider = 'openlibrary' | 'hardcover' | 'google';

export interface DownloaderStatus {
  enabled: boolean;
  connection: {
    id: string; label: string; adapter: string; enabled: boolean; revision: number;
    config: { allow_mobi: boolean; metadata_provider: MetadataProvider };
  } | null;
  /** Enabled sources by kind; an empty list means nothing can be found yet. */
  sources: { ebook: string[]; audiobook: string[] };
  /** Sources switched on that still cannot be searched, and why. */
  source_problems: SourceProblem[];
  acquisition: AcquisitionInstanceState;
}

export interface SourceProblem {
  source: 'direct_download' | 'libgen' | 'audiobookbay';
  reason: 'needs_mirror' | 'needs_provider' | 'needs_hostname';
}

export interface DownloaderTask {
  id: string;
  status: string;
  title: string | null;
  source: string | null;
  progress: number | null;
  message: string | null;
  added: number | null;
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

export function getDownloader(): Promise<DownloaderStatus> {
  return apiGet<DownloaderStatus>(BASE);
}

/** Switch downloads on or off, or change the downloader's own options. */
export function updateDownloader(
  changes: Partial<{ enabled: boolean; allow_mobi: boolean; metadata_provider: MetadataProvider }>,
): Promise<DownloaderStatus> {
  return apiPatch<DownloaderStatus>(BASE, changes);
}

export function getDownloaderQueue(): Promise<{ tasks: DownloaderTask[] }> {
  return apiGet<{ tasks: DownloaderTask[] }>(`${BASE}/queue`);
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
