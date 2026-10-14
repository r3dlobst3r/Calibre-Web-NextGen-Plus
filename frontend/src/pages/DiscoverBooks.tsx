import { Link, useSearch } from 'wouter';
import { TrendingUp } from 'lucide-react';
import { GetMoreBooks } from '../components/GetMoreBooks';
import { useT } from '../lib/i18n';
import styles from './DiscoverBooks.module.css';

/** Genres Discover offers: Open Library subject key, English label. The server
 *  accepts exactly these (cps/services/acquisition/discover.py GENRES). */
const GENRES: readonly (readonly [string, string])[] = [
  ['fantasy', 'Fantasy'], ['science_fiction', 'Science fiction'], ['mystery', 'Mystery'],
  ['thriller', 'Thriller'], ['romance', 'Romance'], ['horror', 'Horror'],
  ['historical_fiction', 'Historical fiction'], ['young_adult_fiction', 'Young adult'],
  ['graphic_novels', 'Graphic novels'], ['classic_literature', 'Classics'], ['humor', 'Humor'],
  ['poetry', 'Poetry'], ['biography', 'Biography'], ['history', 'History'],
  ['science', 'Science'], ['business', 'Business'], ['self-help', 'Self-help'],
];
const GENRE_KEYS = new Set(GENRES.map(([key]) => key));

type Period = 'week' | 'all';

/** The list in the address, so back, reload and a shared link keep it. */
function readChoice(search: string): { list: string; period: Period } {
  const params = new URLSearchParams(search);
  const list = params.get('list') ?? 'popular';
  return {
    list: list === 'popular' || GENRE_KEYS.has(list) ? list : 'popular',
    period: params.get('period') === 'all' ? 'all' : 'week',
  };
}

function href(list: string, period: Period): string {
  const params = new URLSearchParams();
  if (list !== 'popular') params.set('list', list);
  if (period !== 'week') params.set('period', period);
  const query = params.toString();
  return `/discover-books${query ? `?${query}` : ''}`;
}

/** Popular books outside the library: this week's most-read on Open Library, or
 *  a genre's, either this week or of all time. Each can be downloaded like a
 *  searched book. */
export function DiscoverBooks() {
  const t = useT();
  const { list, period } = readChoice(useSearch());
  const feed = list === 'popular' ? `popular:${period}` : `genre:${list}:${period}`;
  const genre = GENRES.find(([key]) => key === list);
  const listName = genre ? t(genre[1]) : t('Everything');

  const choice = (value: string, label: string) => (
    <li key={value}>
      <Link href={href(value, period)} className={styles.chip}
        aria-current={list === value ? 'page' : undefined}>{label}</Link>
    </li>
  );

  return (
    <main className={styles.page} data-testid="discover-books">
      <div className={styles.heading}>
        <TrendingUp aria-hidden="true" focusable={false} />
        <h1 tabIndex={-1}>{t('Popular books')}</h1>
      </div>
      <p className={styles.lede}>
        {t('What readers on Open Library are reading. Pick a book to find a copy and download it into this library.')}
      </p>

      <nav className={styles.controls} aria-label={t('Popular books')}>
        <ul className={styles.periods} role="list" aria-label={t('Period')}>
          <li>
            <Link href={href(list, 'week')} className={styles.period}
              aria-current={period === 'week' ? 'page' : undefined}>{t('This week')}</Link>
          </li>
          <li>
            <Link href={href(list, 'all')} className={styles.period}
              aria-current={period === 'all' ? 'page' : undefined}>{t('All time')}</Link>
          </li>
        </ul>
        <ul className={styles.chips} role="list" aria-label={t('Genre')}>
          {choice('popular', t('Everything'))}
          {GENRES.map(([key, label]) => choice(key, t(label)))}
        </ul>
      </nav>

      <h2 className={styles.listHeading}>
        {period === 'week'
          ? t('{list}: popular this week', { list: listName })
          : t('{list}: popular of all time', { list: listName })}
      </h2>
      <GetMoreBooks key={feed} feed={feed} />
    </main>
  );
}
