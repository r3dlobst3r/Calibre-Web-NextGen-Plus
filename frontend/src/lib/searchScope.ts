/* Which side of a library search the user is looking at.
 *
 * The top-bar search lands on `/?q=…`, which is the library's own results. When
 * the account may request books and a download service is connected, the same
 * search can also look outside the library: `/?q=…&scope=get`. Keeping the
 * choice in the URL means the back button, a reload and a shared link all land
 * on the tab the user was actually reading.
 *
 * Import-free so the node unit lane can exercise it directly.
 */

export type SearchScope = 'library' | 'get';

/** The tab a URL asks for. Anything unrecognised is the library: that is what
 *  `/?q=` always meant, so an old link can never land somewhere new. */
export function searchScopeFromQuery(search: string): SearchScope {
  return new URLSearchParams(search).get('scope') === 'get' ? 'get' : 'library';
}

/** The URL for one tab of a search, keeping every other parameter as it was. */
export function searchScopeHref(search: string, scope: SearchScope): string {
  const params = new URLSearchParams(search);
  if (scope === 'get') params.set('scope', 'get');
  else params.delete('scope');
  const query = params.toString();
  return query ? `/?${query}` : '/';
}

/** The search text the tabs share. */
export function searchQueryFromQuery(search: string): string {
  return (new URLSearchParams(search).get('q') || '').trim();
}
