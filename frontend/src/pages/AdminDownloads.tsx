import { useEffect, useId, useState } from 'react';
import { Link } from 'wouter';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ChevronLeft, Download } from 'lucide-react';
import {
  getDownloader,
  getDownloaderQueue,
  getDownloadSettings,
  updateDownloader,
  type DownloaderStatus,
  type MetadataProvider,
} from '../lib/downloads';
import { useRuntimeReasonText } from '../lib/acquisitionCopy';
import { ApiError } from '../lib/api';
import { useT } from '../lib/i18n';
import { useAnnouncer } from '../lib/a11y/announcer';
import { SpinnerCentered } from '../components/Spinner';
import { SectionError } from '../components/SectionError';
import { DownloadSettingsForm } from '../components/DownloadSettingsForm';
import styles from './AdminAcquisition.module.css';
import own from './AdminDownloads.module.css';

function errorCode(error: unknown): string | undefined {
  if (error instanceof ApiError && error.detail && typeof error.detail.code === 'string') {
    return error.detail.code;
  }
  return undefined;
}

const SOURCE_NAMES: Record<string, string> = {
  direct_download: "Anna's Archive", libgen: 'LibGen', audiobookbay: 'AudiobookBay',
};

/** Admin → Downloads: the downloader built into this server. Switch it on, choose
 *  how books are searched, and configure its sources, clients and network. */
export function AdminDownloads() {
  const t = useT();
  const announce = useAnnouncer();
  const queryClient = useQueryClient();
  const reasonText = useRuntimeReasonText();
  const formId = useId();
  const [actionError, setActionError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  // A switch shows what was asked for straight away; the server's answer replaces it.
  const [pending, setPending] = useState<{ enabled?: boolean; allow_mobi?: boolean }>({});

  const status = useQuery({ queryKey: ['admin', 'downloads'], queryFn: getDownloader, retry: false });
  const enabled = pending.enabled ?? !!status.data?.enabled;
  const settings = useQuery({ queryKey: ['admin', 'downloads', 'settings'], queryFn: getDownloadSettings, retry: false });
  const queue = useQuery({
    queryKey: ['admin', 'downloads', 'queue'],
    queryFn: getDownloaderQueue,
    enabled,
    refetchInterval: (q) => (q.state.data?.tasks ?? []).some((task) =>
      ['queued', 'resolving', 'locating', 'downloading'].includes(task.status)) ? 3000 : false,
  });

  // The server sends the tabs in the order an administrator needs them.
  const tabs = settings.data?.tabs ?? [];
  useEffect(() => {
    if (tabs.length && !tabs.some((tab) => tab.name === activeTab)) setActiveTab(tabs[0].name);
  }, [tabs, activeTab]);

  const update = useMutation({
    mutationFn: updateDownloader,
    onSettled: () => setPending({}),
    onSuccess: (result: DownloaderStatus, changes) => {
      setActionError(null);
      queryClient.setQueryData(['admin', 'downloads'], result);
      void queryClient.invalidateQueries({ queryKey: ['acquisition'] });
      if ('enabled' in changes) announce(result.enabled ? t('Downloads are on.') : t('Downloads are off.'));
      else announce(t('Saved.'));
    },
    onError: (error) => {
      const code = errorCode(error);
      setActionError(code === 'needs_review'
        ? t('Book sources need an administrator review first. Open Book sources.')
        : code === 'conflict'
          ? t('Finish or cancel the downloads in progress before changing this.')
          : t('The change could not be saved.'));
    },
  });

  // A saved source setting can switch a source on or off.
  const refreshStatus = () => { void queryClient.invalidateQueries({ queryKey: ['admin', 'downloads'], exact: true }); };

  if (status.isLoading) return <SpinnerCentered />;

  const header = (
    <>
      <Link href="/admin" className={styles.back}>
        <ChevronLeft size={16} aria-hidden="true" focusable={false} />
        <span>{t('Admin')}</span>
      </Link>
      <header className={styles.heading}>
        <Download aria-hidden="true" focusable={false} />
        <h1>{t('Downloads')}</h1>
      </header>
    </>
  );

  if (status.isError || !status.data) {
    return (
      <div className={styles.container}>
        {header}
        <SectionError message={errorCode(status.error) === 'forbidden'
            ? t('This account is no longer an administrator.')
            : t('The download settings could not be loaded.')}
          onRetry={() => void status.refetch()} retrying={status.isFetching} />
      </div>
    );
  }

  const data = status.data;
  const runtime = data.acquisition.runtime;
  const config = data.connection?.config ?? { allow_mobi: false, metadata_provider: 'openlibrary' as MetadataProvider };
  const sourceList = (names: string[]) => names.map((name) => SOURCE_NAMES[name] ?? name).join(', ');
  const activeSettingsTab = tabs.find((tab) => tab.name === activeTab);
  const tasks = queue.data?.tasks ?? [];

  return (
    <div className={styles.container}>
      {header}
      <p className={styles.lede}>
        {t('Find books outside your library and download them into it. Readers search as usual and choose Get more books; every download is imported here and credited to the person who asked for it.')}
      </p>

      <section className={styles.card} aria-labelledby={`${formId}-switch`}>
        <h2 id={`${formId}-switch`}>{t('Downloader')}</h2>
        <label className={styles.switchRow}>
          <input type="checkbox" checked={enabled} disabled={update.isPending}
            onChange={(event) => {
              setPending({ enabled: event.target.checked });
              update.mutate({ enabled: event.target.checked });
            }} />
          <span>
            <span className={styles.switchLabel}>{t('Allow downloads')}</span>
            <span className={styles.hint}>{t('Who may download, and whether they need approval, is set under Book sources.')}</span>
          </span>
        </label>
        {enabled && (
          <div className={own.options}>
            <label className={own.field}>
              <span>{t('Search books with')}</span>
              <select value={config.metadata_provider} disabled={update.isPending}
                onChange={(event) => update.mutate({ metadata_provider: event.target.value as MetadataProvider })}>
                <option value="openlibrary">Open Library</option>
                <option value="hardcover">Hardcover</option>
                <option value="google">Google Books</option>
              </select>
            </label>
            <label className={styles.switchRow}>
              <input type="checkbox" checked={pending.allow_mobi ?? config.allow_mobi} disabled={update.isPending}
                onChange={(event) => {
                  setPending({ allow_mobi: event.target.checked });
                  update.mutate({ allow_mobi: event.target.checked });
                }} />
              <span>{t('Allow direct DRM-free MOBI 6 books')}</span>
            </label>
            <p className={styles.hint}>
              {data.sources.ebook.length
                ? t('Ebooks come from: {sources}.', { sources: sourceList(data.sources.ebook) })
                : t('No ebook source is switched on yet. Turn one on under Download Sources below.')}
              {' '}
              {data.sources.audiobook.length
                ? t('Audiobooks come from: {sources}.', { sources: sourceList(data.sources.audiobook) })
                : t('Audiobooks are off; switch on AudiobookBay and a torrent or debrid client to find them.')}
            </p>
          </div>
        )}
        {actionError && <p className={styles.bad} role="alert">{actionError}</p>}
      </section>

      {enabled && (!data.acquisition.enabled || !runtime.available) && (
        <div className={styles.notice} role="status">
          <AlertTriangle size={16} aria-hidden="true" focusable={false} />
          <div>
            <p>{!data.acquisition.enabled
              ? t('Requests are switched off, so nobody can download yet. Turn them on and choose who may download in Book sources.')
              : t('Downloads are paused:')}</p>
            {data.acquisition.enabled && (
              <ul className={styles.reasons} role="list">
                {runtime.reasons.map((reason) => <li key={reason}>{reasonText(reason)}</li>)}
              </ul>
            )}
            <Link href="/admin/acquisition" className={own.link}>{t('Open Book sources')}</Link>
          </div>
        </div>
      )}

      {enabled && tasks.length > 0 && (
        <section className={styles.card} aria-labelledby={`${formId}-queue`}>
          <h2 id={`${formId}-queue`}>{t('Downloading now')}</h2>
          <ul className={own.tasks} role="list">
            {tasks.map((task) => (
              <li key={task.id} className={own.task}>
                <span className={own.taskTitle}>{task.title ?? task.id}</span>
                <span className={own.taskMeta}>
                  {[task.source, task.status, task.progress != null && task.status === 'downloading'
                    ? `${Math.round(task.progress)}%` : null, task.message].filter(Boolean).join(' · ')}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className={styles.card} aria-labelledby={`${formId}-settings`}>
        <h2 id={`${formId}-settings`}>{t('Sources and clients')}</h2>
        {settings.isLoading ? <SpinnerCentered size={24} /> : settings.isError ? (
          <SectionError message={t('The download settings could not be loaded.')}
            onRetry={() => void settings.refetch()} retrying={settings.isFetching} />
        ) : (
          <div className={own.settings}>
            <nav className={own.tabs} aria-label={t('Settings sections')}>
              {tabs.map((tab) => (
                <button key={tab.name} type="button" className={own.tab}
                  aria-current={tab.name === activeTab ? 'true' : undefined} onClick={() => setActiveTab(tab.name)}>
                  {tab.displayName}
                </button>
              ))}
            </nav>
            <div className={own.panel}>
              {activeSettingsTab && (
                <>
                  <h3 className={own.panelTitle}>{activeSettingsTab.displayName}</h3>
                  <DownloadSettingsForm tabs={tabs} tab={activeSettingsTab} onSaved={refreshStatus} />
                </>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
