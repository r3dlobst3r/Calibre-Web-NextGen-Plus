import { useQuery } from '@tanstack/react-query';
import { Link } from 'wouter';
import { apiGet } from '../lib/api';
import { useT } from '../lib/i18n';
import styles from './SimilarBooks.module.css';

type SimilarItem = {
  hardcover_id: number;
  title: string;
  authors: string[];
  cover: string | null;
  rating: number | null;
  year: number | null;
  url: string | null;
  library_book_id: number | null;
};
type SimilarResponse = { available: boolean; items: SimilarItem[] };

/** "Books like this" from Hardcover: a strip of related titles under the book
 *  detail. Books already in the library link to their local page; the rest link
 *  out to Hardcover. Renders nothing when Hardcover is not set up, the book has
 *  no hardcover-id, or nothing came back, so it never leaves an empty heading. */
export function SimilarBooks({ bookId }: { bookId: number }) {
  const t = useT();
  const { data } = useQuery({
    queryKey: ['book-similar', bookId],
    queryFn: () => apiGet<SimilarResponse>(`/api/v1/books/${bookId}/similar`),
    staleTime: 60 * 60 * 1000,
    retry: false,
  });
  if (!data?.available || data.items.length === 0) return null;

  const heading = t('Books like this');
  return (
    <section className={styles.box} aria-label={heading}>
      <h2 className={styles.title}>{heading}</h2>
      <p className={styles.source}>{t('Suggested by Hardcover')}</p>
      <div className={styles.strip}>
        {data.items.map((b) => {
          const inner = (
            <>
              <div className={styles.cover}>
                {b.cover ? <img src={b.cover} alt="" loading="lazy" referrerPolicy="no-referrer" /> : null}
                {b.library_book_id ? <span className={styles.badge}>{t('In your library')}</span> : null}
              </div>
              <span className={styles.name} dir="auto">{b.title}</span>
              <span className={styles.author} dir="auto">{b.authors.join(', ')}</span>
            </>
          );
          return (
            <div className={styles.item} key={b.hardcover_id}>
              {b.library_book_id ? (
                <Link href={`/book/${b.library_book_id}`} className={styles.card}
                  aria-label={t('Open details for {title}', { title: b.title })}>{inner}</Link>
              ) : b.url ? (
                <a href={b.url} className={styles.card} target="_blank" rel="noopener noreferrer"
                  aria-label={t('{title} on Hardcover', { title: b.title })}>{inner}</a>
              ) : <div className={styles.card}>{inner}</div>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
