import { Link, useSearch } from 'wouter';
import { Download, Library } from 'lucide-react';
import { useT } from '../lib/i18n';
import { searchScopeFromQuery, searchScopeHref, type SearchScope } from '../lib/searchScope';
import styles from './SearchScopeTabs.module.css';

/** "In your library" / "Get more books" for one search. Links, not buttons:
 *  the choice lives in the URL, so back, reload and a shared link all keep it. */
export function SearchScopeTabs() {
  const t = useT();
  const search = useSearch();
  const scope = searchScopeFromQuery(search);
  const tab = (value: SearchScope, label: string, Icon: typeof Library) => (
    <Link href={searchScopeHref(search, value)} className={styles.tab}
      aria-current={scope === value ? 'page' : undefined} data-testid={`search-scope-${value}`}>
      <Icon size={16} aria-hidden="true" focusable={false} />
      <span>{label}</span>
    </Link>
  );
  return (
    <nav className={styles.tabs} aria-label={t('Search in')}>
      {tab('library', t('In your library'), Library)}
      {tab('get', t('Get more books'), Download)}
    </nav>
  );
}
