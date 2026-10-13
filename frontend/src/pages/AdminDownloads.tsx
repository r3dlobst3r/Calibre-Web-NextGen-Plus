import { useEffect, useId, useState } from 'react';
import { Link } from 'wouter';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, ChevronLeft, Download, RefreshCw } from 'lucide-react';
import { orderTabs } from '../lib/downloadSettings';
import { getDownloadService, getDownloadSettings, setUpDownloadService } from '../lib/downloads';
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

/** Admin → Downloads: the download service (Shelfmark) that runs hidden beside
 *  this server. Connecting it, its health, and its source settings all live
 *  here, because Shelfmark's own interface is not exposed. */
export function AdminDownloads() {
  const t = useT();
  const announce = useAnnouncer();
  const queryClient = useQueryClient();
  const reasonText = useRuntimeReasonText();
  const formId = useId();
  const [manual, setManual] = useState(false);
  const [endpoint, setEndpoint] = useState('');
  const [secret, setSecret] = useState('');
  const [setupError, setSetupError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<string | null>(null);

  const status = useQuery({ queryKey: ['admin', 'downloads'], queryFn: getDownloadService, retry: false });
  const connected = !!status.data?.connection?.enabled && !!status.data?.reachable;
  const settings = useQuery({
    queryKey: ['admin', 'downloads', 'settings'],
    queryFn: getDownloadSettings,
    enabled: connected,
    retry: false,
  });

  const tabs = orderTabs(settings.data?.tabs ?? []);
  useEffect(() => {
    if (tabs.length && !tabs.some((tab) => tab.name === activeTab)) setActiveTab(tabs[0].name);
  }, [tabs, activeTab]);

  const setup = useMutation({
    mutationFn: () => setUpDownloadService(manual ? { endpoint: endpoint.trim(), secret: secret.trim() } : undefined),
    onSuccess: (result) => {
      setSetupError(null);
      setSecret('');
      queryClient.setQueryData(['admin', 'downloads'], result);
      void queryClient.invalidateQueries({ queryKey: ['admin', 'downloads', 'settings'] });
      void queryClient.invalidateQueries({ queryKey: ['acquisition'] });
      announce(result.provider_enabled
        ? t('Connected. Open Library was switched on so books can be searched.')
        : t('Connected.'));
    },
    onError: (error) => {
      const code = errorCode(error);
      setSetupError(code === 'shelfmark_auth'
        ? t('The download service rejected the API key. Check SHELFMARK_API_KEY matches on both containers.')
        : code === 'shelfmark_unreachable'
          ? t('The download service did not answer at that address.')
          : code === 'shelfmark_not_configured'
            ? t('No address or API key is set. Set SHELFMARK_URL and SHELFMARK_API_KEY, or enter them below.')
            : code === 'needs_review'
              ? t('Book sources need an administrator review first. Open Book sources.')
              : code === 'invalid_connection' || code === 'invalid_authentication'
                ? t('Check the address (http://host:port) and the API key.')
                : error instanceof Error && error.message ? error.message : t('The download service could not be connected.'));
    },
  });

  if (status.isLoading) return <SpinnerCentered />;

  const back = (
    <Link href="/admin" className={styles.back}>
      <ChevronLeft size={16} aria-hidden="true" focusable={false} />
      <span>{t('Admin')}</span>
    </Link>
  );
  const heading = (
    <header className={styles.heading}>
      <Download aria-hidden="true" focusable={false} />
      <h1>{t('Downloads')}</h1>
    </header>
  );

  if (status.isError || !status.data) {
    return (
      <div className={styles.container}>
        {back}{heading}
        <SectionError
          message={errorCode(status.error) === 'forbidden'
            ? t('This account is no longer an administrator.')
            : t('The download service status could not be loaded.')}
          onRetry={() => void status.refetch()}
          retrying={status.isFetching}
        />
      </div>
    );
  }

  const data = status.data;
  const acquisition = data.acquisition;
  const runtime = acquisition.runtime;
  const activeSettingsTab = tabs.find((tab) => tab.name === activeTab);

  return (
    <div className={styles.container}>
      {back}{heading}
      <p className={styles.lede}>
        {t('The download service finds books outside your library. Search, then pick a file, and the book is downloaded and imported here. It runs privately beside this server and is managed only from this page.')}
      </p>

      <section className={styles.card} aria-labelledby={`${formId}-service`}>
        <h2 id={`${formId}-service`}>{t('Download service')}</h2>
        {connected ? (
          <p className={styles.ok}>
            <CheckCircle2 size={16} aria-hidden="true" focusable={false} />
            <span>{t('Connected to {endpoint}.', { endpoint: data.connection?.endpoint ?? '' })}</span>
          </p>
        ) : data.connection && !data.reachable ? (
          <p className={styles.bad}>{data.error === 'needs_auth'
            ? t('The download service rejected the stored API key.')
            : t('The download service is not responding at {endpoint}.', { endpoint: data.connection.endpoint })}</p>
        ) : data.connection && !data.connection.enabled ? (
          <p className={styles.hint}>{t('The download service is connected but switched off in Book sources.')}</p>
        ) : (
          <p className={styles.hint}>{data.environment.configured
            ? t('Ready to connect to {endpoint}.', { endpoint: data.environment.endpoint ?? '' })
            : t('Not set up. Set SHELFMARK_URL and SHELFMARK_API_KEY for this server, or enter them below.')}</p>
        )}

        <form className={own.setup} onSubmit={(event) => { event.preventDefault(); setup.mutate(); }}>
          {manual && (
            <div className={own.fields}>
              <label className={own.field}>
                <span>{t('Address')}</span>
                <input type="url" required value={endpoint} placeholder="http://shelfmark:8084"
                  onChange={(event) => setEndpoint(event.target.value)} />
              </label>
              <label className={own.field}>
                <span>{t('API key')}</span>
                <input type="password" required value={secret} autoComplete="new-password"
                  onChange={(event) => setSecret(event.target.value)} />
              </label>
            </div>
          )}
          <div className={own.buttons}>
            <button type="submit" className={own.primary} disabled={setup.isPending}>
              <RefreshCw size={14} aria-hidden="true" focusable={false} />
              <span>{setup.isPending ? t('Connecting…') : data.connection ? t('Reconnect') : t('Connect')}</span>
            </button>
            <button type="button" className={own.secondary} onClick={() => setManual((value) => !value)}>
              {manual ? t('Use the server settings') : t('Enter address and key')}
            </button>
          </div>
          {setupError && <p className={styles.bad} role="alert">{setupError}</p>}
        </form>
      </section>

      {connected && (!acquisition.enabled || !runtime.available) && (
        <div className={styles.notice} role="status">
          <AlertTriangle size={16} aria-hidden="true" focusable={false} />
          <div>
            <p>{!acquisition.enabled
              ? t('Requests are switched off, so nobody can download yet. Turn them on and choose who may download in Book sources.')
              : t('Downloads are paused:')}</p>
            {acquisition.enabled && (
              <ul className={styles.reasons} role="list">
                {runtime.reasons.map((reason) => <li key={reason}>{reasonText(reason)}</li>)}
              </ul>
            )}
            <Link href="/admin/acquisition" className={own.link}>{t('Open Book sources')}</Link>
          </div>
        </div>
      )}

      {connected && (
        <section className={styles.card} aria-labelledby={`${formId}-settings`}>
          <h2 id={`${formId}-settings`}>{t('Sources and search')}</h2>
          {settings.isLoading ? <SpinnerCentered size={24} /> : settings.isError ? (
            <SectionError message={t('The download service settings could not be loaded.')}
              onRetry={() => void settings.refetch()} retrying={settings.isFetching} />
          ) : (
            <div className={own.settings}>
              <nav className={own.tabs} aria-label={t('Settings sections')}>
                {tabs.map((tab) => (
                  <button key={tab.name} type="button" className={own.tab} aria-current={tab.name === activeTab ? 'true' : undefined}
                    onClick={() => setActiveTab(tab.name)}>
                    {tab.displayName}
                  </button>
                ))}
              </nav>
              <div className={own.panel}>
                {activeSettingsTab && (
                  <>
                    <h3 className={own.panelTitle}>{activeSettingsTab.displayName}</h3>
                    <DownloadSettingsForm tabs={tabs} tab={activeSettingsTab} />
                  </>
                )}
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
