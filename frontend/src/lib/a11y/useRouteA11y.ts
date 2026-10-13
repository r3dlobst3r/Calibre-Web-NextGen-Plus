/* SPA route-change accessibility (WCAG 2.2 SC 2.4.2 Page Titled, 2.4.3 Focus Order).
 *
 * A client-routed page swap is invisible to a screen reader: focus stays where
 * it was (often on a now-unmounted link) and the title never changes. On every
 * navigation this hook:
 *   1. moves focus to the <main> landmark (so the next Tab / SR read starts at
 *      the new content, not stranded mid-page), and
 *   2. sets document.title to "<page h1> · <instance>" so each view is
 *      distinctly titled in history and the SR announces the new page.
 *
 * Must be called inside a wouter <Router> (needs useLocation). Rendered once via
 * <RouteA11y> at the top of the routing tree. See patterns.md → "Route focus".
 */
import { displayInstanceName } from '../../components/BrandName';
import { useEffect, useRef } from 'react';
import { useLocation } from 'wouter';
import { useI18n } from '../i18n';

export function useRouteA11y(instanceName?: string): void {
  const [location] = useLocation();
  const { locale, ready } = useI18n();
  const firstRun = useRef(true);

  useEffect(() => {
    const isFirst = firstRun.current;
    firstRun.current = false;

    // Don't steal focus on initial page load — only on client navigation.
    if (!isFirst) {
      const main = document.getElementById('main');
      main?.focus();
    }

  }, [location]);

  useEffect(() => {
    // The route can render once with the identity translator while its catalog
    // is loading. Waiting for i18n prevents that transient English heading from
    // becoming the persistent browser/SR page title (#886).
    if (!ready) return;

    const base = displayInstanceName(instanceName);
    document.title = base;
    const updateTitle = () => {
      const heading = document.querySelector('main h1')?.textContent?.trim();
      // Entity routes intentionally render an ellipsis while their list query
      // resolves. Never leak that loading placeholder into browser history.
      if (heading && heading !== '…' && heading !== '...') {
        document.title = `${heading} · ${base}`;
      }
    };

    // The first frame covers synchronous routes. MutationObserver covers async
    // headings (series/tag/author/publisher/language direct URLs) whose real
    // name arrives after that frame.
    const raf = requestAnimationFrame(updateTitle);
    const observer = new MutationObserver(updateTitle);
    observer.observe(document.getElementById('root') || document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [location, instanceName, locale, ready]);
}

/** Zero-DOM component that runs route a11y side-effects. Mount inside <Router>. */
export function RouteA11y({ instanceName }: { instanceName?: string }) {
  useRouteA11y(instanceName);
  return null;
}
