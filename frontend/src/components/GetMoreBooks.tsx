import { useCallback, useId, useMemo, useRef, useState } from 'react';
import { Link } from 'wouter';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, BookCheck, ChevronDown, ChevronUp, Download, Headphones, RefreshCw, Send } from 'lucide-react';
import {
  createAcquisitionJob,
  getAcquisitionBootstrap,
  getAcquisitionCatalog,
  getAcquisitionJobs,
  isAcquisitionPending,
  type AcquisitionCatalog,
  type AcquisitionConnection,
  type AcquisitionJob,
  type AcquisitionPublication,
} from '../lib/acquisition';
import { hasVisibleReceipt } from '../lib/acquisitionViewState';
import { ApiError } from '../lib/api';
import { formatAuthors } from '../lib/authors';
import { useT } from '../lib/i18n';
import { useMe } from '../lib/queries';
import { useAnnouncer } from '../lib/a11y/announcer';
import { BookCover } from './BookCover';
import { EmptyState } from './EmptyState';
import { Spinner } from './Spinner';
import styles from './GetMoreBooks.module.css';

function errorCode(error: unknown): string | undefined {
  if (error instanceof ApiError && error.detail && typeof error.detail.code === 'string') {
    return error.detail.code;
  }
  return undefined;
}

/** The download service, when an administrator has connected one. */
export function useDownloadService(enabled: boolean): {
  connection: AcquisitionConnection | undefined;
  canAcquire: boolean;
  isLoading: boolean;
  error: unknown;
} {
  const bootstrap = useQuery({
    queryKey: ['acquisition', 'bootstrap'],
    queryFn: getAcquisitionBootstrap,
    enabled,
    retry: false,
  });
  return {
    connection: bootstrap.data?.connections.find((c) => c.adapter === 'downloader' && c.enabled),
    canAcquire: !!bootstrap.data?.can_acquire,
    isLoading: enabled && bootstrap.isLoading,
    error: bootstrap.error,
  };
}

function useServiceErrorText(): (error: unknown) => string {
  const t = useT();
  return useCallback((error: unknown) => {
    const code = errorCode(error);
    switch (code) {
      case 'search_unavailable': return t('Book search is not available right now. Try again in a moment.');
      case 'downloads_disabled': return t('Downloads are switched off. Ask an administrator to check Downloads.');
      case 'invalid_search': return t('Enter a shorter search.');
      case 'source_busy': return t('A download source is busy. Try again in a moment.');
      case 'not_found': return t('That result expired. Search again.');
      default: return t('Books could not be searched.');
    }
  }, [t]);
}

function useJobLabel(): (job: AcquisitionJob) => { label: string; tone: 'active' | 'ok' | 'bad' | 'muted' } {
  const t = useT();
  return useCallback((job: AcquisitionJob) => {
    switch (job.state) {
      case 'awaiting_approval': return { label: t('Waiting for approval'), tone: 'muted' };
      case 'queued': return { label: t('Queued'), tone: 'active' };
      case 'resolving':
      case 'downloading': return { label: t('Downloading'), tone: 'active' };
      case 'staged':
      case 'publishing':
      case 'importing': return { label: t('Importing'), tone: 'active' };
      case 'imported': return { label: t('In your library'), tone: 'ok' };
      case 'failed': return { label: t('Failed'), tone: 'bad' };
      case 'rejected': return { label: t('Request rejected'), tone: 'muted' };
      case 'cancelled': return { label: t('Request cancelled'), tone: 'muted' };
      default: return { label: job.state, tone: 'muted' };
    }
  }, [t]);
}

/** Search outside the library through the download service, and pull a book in. */
export function GetMoreBooks({ query }: { query: string }) {
  const t = useT();
  const { data: me } = useMe();
  const service = useDownloadService(!!me?.acquisition_access);
  const errorText = useServiceErrorText();
  const connectionId = service.connection?.id;

  const results = useInfiniteQuery({
    queryKey: ['acquisition', 'downloader-search', connectionId, query],
    queryFn: ({ pageParam }) => getAcquisitionCatalog(connectionId as string,
      pageParam ? { selection: pageParam } : { query }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last: AcquisitionCatalog) => last.pagination?.[0]?.selection,
    enabled: !!connectionId && !!query,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  const jobs = useQuery({
    queryKey: ['acquisition', 'jobs'],
    queryFn: getAcquisitionJobs,
    enabled: !!connectionId,
    refetchInterval: (q) => (q.state.data?.jobs ?? []).some((job) => isAcquisitionPending(job.state)) ? 3000 : false,
  });

  const books = useMemo(() => {
    const seen = new Set<string>();
    return (results.data?.pages ?? []).flatMap((page) => page.publications)
      .filter((book) => !seen.has(book.identity) && !!seen.add(book.identity));
  }, [results.data]);

  if (!me?.acquisition_access || errorCode(service.error) === 'not_found') {
    return (
      <EmptyState icon={AlertTriangle} title={t('Downloads are not available')}
        message={t('This account cannot request books, or requests are switched off.')} />
    );
  }
  if (service.isLoading) return <div className={styles.loading}><Spinner /></div>;
  if (!service.connection) {
    return (
      <EmptyState icon={AlertTriangle} title={t('Downloads are not set up')}
        message={t('An administrator needs to switch on downloads before books can be found here.')}>
        {me?.role?.admin && <Link href="/admin/downloads" className={styles.link}>{t('Set up downloads')}</Link>}
      </EmptyState>
    );
  }

  return (
    <section className={styles.panel} aria-labelledby="get-more-heading">
      <h2 id="get-more-heading" className="sr-only">{t('Books to download')}</h2>
      {results.isLoading ? (
        <div className={styles.loading} role="status"><Spinner /><span>{t('Searching…')}</span></div>
      ) : results.isError && !books.length ? (
        <div className={styles.error} role="alert">
          <p>{errorText(results.error)}</p>
          <button type="button" className={styles.secondary} onClick={() => void results.refetch()} disabled={results.isFetching}>
            <RefreshCw size={14} aria-hidden="true" focusable={false} /><span>{t('Try again')}</span>
          </button>
        </div>
      ) : !books.length ? (
        <EmptyState message={t('No books found for "{q}".', { q: query })} />
      ) : (
        <>
          <ul className={styles.books} role="list">
            {books.map((book) => (
              <BookResult key={book.identity} book={book} connectionId={connectionId as string}
                canAcquire={service.canAcquire} jobs={jobs.data?.jobs ?? []} />
            ))}
          </ul>
          {results.hasNextPage && (
            <button type="button" className={styles.more} onClick={() => void results.fetchNextPage()}
              disabled={results.isFetchingNextPage}>
              {results.isFetchingNextPage ? t('Loading…') : t('More results')}
            </button>
          )}
        </>
      )}
      <p className={styles.activity}>
        <Link href="/find-books">{t('See all your requests')}</Link>
      </p>
    </section>
  );
}

function BookResult({ book, connectionId, canAcquire, jobs }: {
  book: AcquisitionPublication;
  connectionId: string;
  canAcquire: boolean;
  jobs: AcquisitionJob[];
}) {
  const t = useT();
  // One button per kind of download the enabled sources can find: ebooks, and
  // audiobooks when an audiobook source is on. Only one list is open at a time.
  const [open, setOpen] = useState<string | null>(null);
  const panelId = useId();
  const kinds = book.navigation.filter((nav) => nav.relations[0]?.startsWith('downloader:'));
  const selection = open;
  const meta = [book.year, book.series ? (book.series.position != null
    ? t('{series} #{position}', { series: book.series.name, position: book.series.position })
    : book.series.name) : null, book.source].filter(Boolean).join(' · ');

  return (
    <li className={styles.book}>
      <div className={styles.cover}>
        <BookCover coverUrl={book.cover_url ?? null} title={book.title} authors={book.authors} />
      </div>
      <div className={styles.info}>
        <h3 className={styles.title}>{book.title}</h3>
        {book.authors.length > 0 && <p className={styles.authors}>{formatAuthors(book.authors)}</p>}
        {meta && <p className={styles.meta}>{meta}</p>}
        <div className={styles.actions}>
          {book.library_book_id != null && (
            <Link href={`/book/${book.library_book_id}`} className={styles.owned}>
              <BookCheck size={14} aria-hidden="true" focusable={false} />
              <span>{t('In your library')}</span>
            </Link>
          )}
          {kinds.map((nav, index) => {
            const audiobook = nav.relations[0] === 'downloader:audiobook';
            const expanded = open === nav.selection;
            return (
              <button key={nav.selection} type="button"
                className={book.library_book_id != null || index > 0 ? styles.secondary : styles.primary}
                aria-expanded={expanded} aria-controls={panelId}
                onClick={() => setOpen(expanded ? null : nav.selection)}>
                {expanded ? <ChevronUp size={14} aria-hidden="true" focusable={false} />
                  : audiobook ? <Headphones size={14} aria-hidden="true" focusable={false} />
                  : <ChevronDown size={14} aria-hidden="true" focusable={false} />}
                <span>{expanded ? (audiobook ? t('Hide audiobooks') : t('Hide downloads'))
                  : (audiobook ? t('Find audiobooks') : t('Find downloads'))}</span>
              </button>
            );
          })}
        </div>
        {selection && (
          <div id={panelId}>
            <ReleaseList key={selection} book={book} selection={selection} connectionId={connectionId}
              canAcquire={canAcquire} jobs={jobs} />
          </div>
        )}
      </div>
    </li>
  );
}

function ReleaseList({ book, selection, connectionId, canAcquire, jobs }: {
  book: AcquisitionPublication;
  selection: string;
  connectionId: string;
  canAcquire: boolean;
  jobs: AcquisitionJob[];
}) {
  const t = useT();
  const announce = useAnnouncer();
  const queryClient = useQueryClient();
  const errorText = useServiceErrorText();
  const jobLabel = useJobLabel();
  const [requested, setRequested] = useState<Record<string, string>>({});
  const [requestError, setRequestError] = useState<string | null>(null);
  // One idempotency key per offer, so a double click or a retried request
  // resolves to the same job instead of downloading the book twice.
  const keys = useRef(new Map<string, string>());
  const keyFor = (offerId: string) => {
    const existing = keys.current.get(offerId);
    if (existing) return existing;
    const fresh = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    keys.current.set(offerId, fresh);
    return fresh;
  };

  const releases = useQuery({
    queryKey: ['acquisition', 'downloader-releases', connectionId, selection],
    queryFn: () => getAcquisitionCatalog(connectionId, { selection }),
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  const request = useMutation({
    mutationFn: (variables: { identity: string; offerId: string }) => createAcquisitionJob({
      connection_id: connectionId, offer_id: variables.offerId,
      idempotency_key: keyFor(variables.offerId), add_to_my_library: true,
    }),
    onSuccess: (job, variables) => {
      setRequestError(null);
      setRequested((current) => ({ ...current, [variables.identity]: job.id }));
      void queryClient.invalidateQueries({ queryKey: ['acquisition', 'jobs'] });
      announce(job.state === 'awaiting_approval'
        ? t('Requested. An administrator has to approve it.')
        : t('Downloading {title}.', { title: book.title }));
    },
    onError: (error) => {
      const code = errorCode(error);
      setRequestError(code === 'acquisition_unavailable'
        ? t('Downloads are paused right now. Ask an administrator to check Downloads.')
        : code === 'not_found'
          ? t('That choice expired. Search again and pick the file again.')
          : t('The download could not be started.'));
    },
  });

  if (releases.isLoading) {
    return (
      <div className={styles.loading} role="status">
        <Spinner size={20} />
        <span>{t('Searching download sources… this can take up to a minute.')}</span>
      </div>
    );
  }
  if (releases.isError) {
    return (
      <div className={styles.error} role="alert">
        <p>{errorText(releases.error)}</p>
        <button type="button" className={styles.secondary} onClick={() => void releases.refetch()} disabled={releases.isFetching}>
          <RefreshCw size={14} aria-hidden="true" focusable={false} /><span>{t('Try again')}</span>
        </button>
      </div>
    );
  }
  const rows = releases.data?.publications ?? [];
  const sourceErrors = releases.data?.errors ?? [];
  const jobsById = new Map(jobs.map((job) => [job.id, job]));

  return (
    <div className={styles.releases}>
      {requestError && <p className={styles.errorText} role="alert">{requestError}</p>}
      {!rows.length ? (
        <p className={styles.muted}>{t('No downloads were found for this book.')}</p>
      ) : (
        <ul className={styles.releaseList} role="list" aria-label={t('Downloads for {title}', { title: book.title })}>
          {rows.map((release) => {
            const offer = release.offers[0];
            const job = requested[release.identity] ? jobsById.get(requested[release.identity]) : undefined;
            const status = job ? jobLabel(job) : null;
            const details = [release.size, release.source, release.languages[0]?.toUpperCase()].filter(Boolean).join(' · ');
            return (
              <li key={release.identity} className={styles.release}>
                <span className={styles.format}>{(offer?.format === 'Unknown' ? release.format : offer?.format)
                  ?? release.format?.toUpperCase() ?? t('File')}</span>
                <span className={styles.releaseInfo}>
                  <span className={styles.releaseTitle}>{release.title}</span>
                  {details && <span className={styles.meta}>{details}</span>}
                </span>
                <span className={styles.releaseAction}>
                  {status ? (
                    job?.state === 'imported' && hasVisibleReceipt(job.result?.book_ids) ? (
                      <Link href={`/book/${job.result!.book_ids[0]}`} className={styles.owned}>
                        <BookCheck size={14} aria-hidden="true" focusable={false} /><span>{status.label}</span>
                      </Link>
                    ) : (
                      <span className={styles.status} data-tone={status.tone} role="status">{status.label}</span>
                    )
                  ) : offer ? (
                    <button type="button" className={styles.primary}
                      disabled={request.isPending && request.variables?.identity === release.identity}
                      onClick={() => request.mutate({ identity: release.identity, offerId: offer.offer_id })}>
                      {canAcquire ? <Download size={14} aria-hidden="true" focusable={false} />
                        : <Send size={14} aria-hidden="true" focusable={false} />}
                      <span>{canAcquire ? t('Download') : t('Request')}</span>
                    </button>
                  ) : (
                    <span className={styles.muted}>{t('Format not supported')}</span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {sourceErrors.length > 0 && (
        <details className={styles.sourceErrors}>
          <summary>{t('Some sources could not be searched')}</summary>
          <ul role="list">{sourceErrors.map((message) => <li key={message}>{message}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
