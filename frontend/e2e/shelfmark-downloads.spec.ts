import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function expectNoSeriousViolations(page: Page) {
  const findings = await new AxeBuilder({ page }).include('main').withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
  expect(findings.violations.filter((v) => ['critical', 'serious'].includes(v.impact ?? ''))
    .map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target) }))).toEqual([]);
}

/*
 * Downloads through the hidden Shelfmark service: the search tabs, Get more
 * books, and Admin → Downloads. The download service is a fixture here; the
 * worker and import path have their own server-side suites.
 */

const shelfmark = { id: 'sm', label: 'Shelfmark', adapter: 'shelfmark', enabled: true, revision: 1 };

const book = (overrides: Record<string, unknown> = {}) => ({
  title: 'Moby Dick', identity: 'moby', authors: ['Herman Melville'], languages: ['en'], description: null,
  offers: [], navigation: [{ title: 'Find downloads', relations: ['shelfmark:releases'], selection: 'moby-releases' }],
  cover_url: null, year: 1851, subtitle: null, series: null, isbn: [], source: 'Open Library', ...overrides,
});

const releases = {
  title: 'Moby Dick', protocol: 'shelfmark', navigation: [], pagination: [], searches: [], groups: [], facets: [],
  errors: ['irc: not configured'],
  publications: [
    { title: 'Moby Dick (EPUB)', identity: 'r1', authors: [], languages: ['en'], description: null, navigation: [],
      format: 'epub', size: '1.2 MB', source: 'Anna’s Archive',
      offers: [{ format: 'EPUB', label: '1.2 MB · Anna’s Archive', identity: 'r1', relation: 'download', offer_id: 'offer-r1' }] },
    { title: 'Moby Dick (AZW3)', identity: 'r2', authors: [], languages: ['en'], description: null, navigation: [],
      format: 'azw3', size: '900 KB', source: 'Anna’s Archive', offers: [], unavailable_reason: 'unsupported_format' },
  ],
};

async function fixture(page: Page, options: { connections?: unknown[]; searchError?: boolean } = {}) {
  const jobs: { id: string; state: string; title: string; result?: unknown }[] = [];
  const posted: Record<string, unknown>[] = [];
  const searches: string[] = [];
  await page.route('**/api/v1/auth/me', async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), acquisition_access: true } });
  });
  await page.route('**/api/v1/acquisition**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/acquisition')) {
      return route.fulfill({ json: { connections: options.connections ?? [shelfmark], can_acquire: true,
        runtime: { available: true, reasons: [] } } });
    }
    if (url.pathname.endsWith('/catalog')) {
      const selection = url.searchParams.get('selection');
      if (selection === 'moby-releases') return route.fulfill({ json: releases });
      searches.push(url.searchParams.get('q') ?? '');
      if (options.searchError) {
        return route.fulfill({ status: 502, json: { error: { code: 'shelfmark_unavailable',
          message: 'No metadata provider configured. Enable one in Settings.' } } });
      }
      return route.fulfill({ json: { title: 'Shelfmark', protocol: 'shelfmark', navigation: [], pagination: [],
        searches: [], groups: [], facets: [],
        publications: [book(), book({ identity: 'owned', title: 'Owned Book', library_book_id: 1 })] } });
    }
    if (url.pathname.endsWith('/jobs') && route.request().method() === 'POST') {
      posted.push(route.request().postDataJSON());
      jobs.push({ id: 'job-1', state: 'downloading', title: 'Moby Dick' });
      return route.fulfill({ status: 202, json: jobs[0] });
    }
    if (url.pathname.endsWith('/jobs')) return route.fulfill({ json: { jobs } });
    return route.fallback();
  });
  return { jobs, posted, searches };
}

test('a search can look outside the library and download a book into it', async ({ page }) => {
  const { jobs, posted, searches } = await fixture(page);
  await page.goto('/app/?q=moby');
  const tabs = page.getByRole('navigation', { name: 'Search in' });
  await expect(tabs.getByRole('link', { name: 'In your library' })).toHaveAttribute('aria-current', 'page');
  await tabs.getByRole('link', { name: 'Get more books' }).click();
  await expect(page).toHaveURL(/scope=get/);
  await expect(page.getByRole('heading', { name: 'Get more books', level: 1 })).toBeVisible();
  expect(searches).toEqual(['moby']);

  // A result the library already has links to it instead of only offering a download.
  const owned = page.getByRole('listitem').filter({ hasText: 'Owned Book' });
  await expect(owned.getByRole('link', { name: 'In your library' })).toHaveAttribute('href', /\/book\/1$/);

  const moby = page.getByRole('listitem').filter({ hasText: 'Herman Melville' }).first();
  await moby.getByRole('button', { name: 'Find downloads' }).click();
  const files = page.getByRole('list', { name: 'Downloads for Moby Dick' });
  await expect(files.getByText('1.2 MB · Anna’s Archive · EN')).toBeVisible();
  await expect(files.getByText('Format not supported')).toBeVisible();
  await expect(page.getByText('Some sources could not be searched')).toBeVisible();
  await expectNoSeriousViolations(page);

  await files.getByRole('button', { name: 'Download' }).click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toMatchObject({ connection_id: 'sm', offer_id: 'offer-r1', add_to_my_library: true });
  await expect(files.getByRole('status')).toHaveText('Downloading');

  jobs[0] = { ...jobs[0], state: 'imported', result: { book_ids: [1], disposition: 'imported' } };
  await expect(files.getByRole('link', { name: 'In your library' })).toHaveAttribute('href', /\/book\/1$/, { timeout: 10_000 });

  // The tab choice lives in the URL, so going back returns to the library results.
  await page.getByRole('navigation', { name: 'Search in' }).getByRole('link', { name: 'In your library' }).click();
  await expect(page).not.toHaveURL(/scope=get/);
  await expect(page.getByTestId('catalog-page')).toBeVisible();
});

test('without a connected download service the search stays the plain library', async ({ page }) => {
  await fixture(page, { connections: [] });
  await page.goto('/app/?q=moby');
  await expect(page.getByTestId('catalog-page')).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Search in' })).toHaveCount(0);
});

test('the download service explains why it cannot search', async ({ page }) => {
  await fixture(page, { searchError: true });
  await page.goto('/app/?q=moby&scope=get');
  await expect(page.getByRole('alert').filter({ hasText: 'No metadata provider configured' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
});

test('Admin → Downloads connects the service and edits its settings', async ({ page }) => {
  const saved: { tab: string; values: unknown }[] = [];
  let connected = false;
  const status = () => ({
    environment: { configured: true, endpoint: 'http://shelfmark:8084' },
    connection: connected ? { ...shelfmark, endpoint: 'http://shelfmark:8084' } : null,
    reachable: connected, error: null,
    acquisition: { enabled: true, migration_status: 'ready', runtime: { available: true, reasons: [] } },
  });
  await page.route('**/api/v1/admin/downloads**', async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.pathname.endsWith('/setup')) {
      connected = true;
      return route.fulfill({ json: { ...status(), provider_enabled: true } });
    }
    if (url.pathname.endsWith('/settings')) {
      return route.fulfill({ json: { groups: [], tabs: [
        { name: 'search_mode', displayName: 'Search', order: 1, fields: [
          { key: 'SEARCH_MODE', type: 'SelectField', label: 'Search mode', value: 'universal',
            options: [{ value: 'universal', label: 'Universal' }, { value: 'direct', label: 'Direct' }] },
          { key: 'HARDCOVER_NOTE', type: 'TextField', label: 'Universal only field', value: '', universalOnly: true },
        ] },
        { name: 'prowlarr_config', displayName: 'Prowlarr', order: 2, fields: [
          { key: 'PROWLARR_URL', type: 'TextField', label: 'Prowlarr URL', value: 'http://prowlarr:9696' },
          { key: 'PROWLARR_API_KEY', type: 'PasswordField', label: 'API key', value: '', hasValue: true },
          { key: 'test_prowlarr', type: 'ActionButton', label: 'Test connection', style: 'primary' },
        ] },
      ] } });
    }
    if (url.pathname.includes('/action/')) return route.fulfill({ json: { success: false, message: 'Prowlarr did not answer' } });
    if (method === 'PUT') {
      saved.push({ tab: url.pathname.split('/').pop()!, values: route.request().postDataJSON() });
      return route.fulfill({ json: { success: true, message: 'Settings saved' } });
    }
    return route.fulfill({ json: status() });
  });

  await page.goto('/app/admin/downloads');
  await expect(page.getByRole('heading', { name: 'Downloads', level: 1 })).toBeVisible();
  await expect(page.getByText('Ready to connect to http://shelfmark:8084.')).toBeVisible();
  await page.getByRole('button', { name: 'Connect' }).click();
  await expect(page.getByText('Connected to http://shelfmark:8084.')).toBeVisible();

  // Fields follow the service's own visibility rules.
  await expect(page.getByLabel('Universal only field')).toBeVisible();
  await page.getByLabel('Search mode').selectOption('direct');
  await expect(page.getByLabel('Universal only field')).toHaveCount(0);
  await page.getByRole('button', { name: 'Save' }).click();
  await expect.poll(() => saved).toEqual([{ tab: 'search_mode', values: { SEARCH_MODE: 'direct' } }]);

  await page.getByRole('button', { name: 'Prowlarr' }).click();
  // A stored secret is never shown; blank keeps it.
  await expect(page.getByLabel('API key', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('API key', { exact: true })).toHaveAttribute('placeholder', 'Saved. Leave blank to keep it.');
  await expectNoSeriousViolations(page);
  await page.getByRole('button', { name: 'Test connection' }).click();
  await expect(page.getByText('Prowlarr did not answer')).toBeVisible();
  await page.getByLabel('Prowlarr URL').fill('http://prowlarr:9797');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect.poll(() => saved.length).toBe(2);
  expect(saved[1]).toEqual({ tab: 'prowlarr_config', values: { PROWLARR_URL: 'http://prowlarr:9797' } });
});
