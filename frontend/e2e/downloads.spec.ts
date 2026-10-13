import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function expectNoSeriousViolations(page: Page) {
  const findings = await new AxeBuilder({ page }).include('main').withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
  expect(findings.violations.filter((v) => ['critical', 'serious'].includes(v.impact ?? ''))
    .map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target) }))).toEqual([]);
}

/*
 * Downloads with the built-in downloader: the search tabs, Get more books and
 * Admin → Downloads. The server is a fixture here; the engine, worker and
 * import path have their own server-side suites.
 */

const downloader = { id: 'dl', label: 'Downloads', adapter: 'downloader', enabled: true, revision: 1 };

const book = (overrides: Record<string, unknown> = {}) => ({
  title: 'Moby Dick', identity: 'moby', authors: ['Herman Melville'], languages: ['en'], description: null,
  offers: [], navigation: [
    { title: 'Find downloads', relations: ['downloader:ebook'], selection: 'moby-releases' },
    { title: 'Find audiobooks', relations: ['downloader:audiobook'], selection: 'moby-audiobooks' },
  ],
  cover_url: null, year: 1851, subtitle: null, series: null, isbn: [], source: 'Open Library', ...overrides,
});

const audiobooks = {
  title: 'Moby Dick', protocol: 'downloader', navigation: [], pagination: [], searches: [], groups: [], facets: [],
  publications: [
    { title: 'Moby Dick (Audible)', identity: 'a1', authors: [], languages: ['en'], description: null, navigation: [],
      format: 'm4b', size: '480 MB', source: 'AudiobookBay',
      offers: [{ format: 'M4B', label: '480 MB · AudiobookBay', identity: 'a1', relation: 'download', offer_id: 'offer-a1' }] },
  ],
};

const releases = {
  title: 'Moby Dick', protocol: 'downloader', navigation: [], pagination: [], searches: [], groups: [], facets: [],
  errors: ['irc: not configured'],
  publications: [
    { title: 'Moby Dick (EPUB)', identity: 'r1', authors: [], languages: ['en'], description: null, navigation: [],
      format: 'epub', size: '1.2 MB', source: 'Anna’s Archive',
      offers: [{ format: 'EPUB', label: '1.2 MB · Anna’s Archive', identity: 'r1', relation: 'download', offer_id: 'offer-r1' }] },
    { title: 'Moby Dick (AZW3)', identity: 'r2', authors: [], languages: ['en'], description: null, navigation: [],
      format: 'azw3', size: '900 KB', source: 'Anna’s Archive', offers: [], unavailable_reason: 'unsupported_format' },
  ],
};

async function fixture(page: Page, options: { connections?: unknown[]; searchError?: boolean; noSources?: boolean; admin?: boolean } = {}) {
  const jobs: { id: string; state: string; title: string; result?: unknown }[] = [];
  const posted: Record<string, unknown>[] = [];
  const searches: string[] = [];
  await page.route('**/api/v1/auth/me', async (route) => {
    const response = await route.fetch();
    const me = await response.json();
    await route.fulfill({ response, json: { ...me, acquisition_access: true,
      ...(options.admin === false ? { role: { ...me.role, admin: false } } : {}) } });
  });
  await page.route('**/api/v1/acquisition**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/acquisition')) {
      return route.fulfill({ json: { connections: options.connections ?? [downloader], can_acquire: true,
        runtime: { available: true, reasons: [] } } });
    }
    if (url.pathname.endsWith('/catalog')) {
      const selection = url.searchParams.get('selection');
      if (selection === 'moby-releases') return route.fulfill({ json: releases });
      if (selection === 'moby-audiobooks') return route.fulfill({ json: audiobooks });
      searches.push(url.searchParams.get('q') ?? '');
      if (options.searchError) {
        return route.fulfill({ status: 502, json: { error: { code: 'search_unavailable',
          message: 'Book search is not available right now.' } } });
      }
      return route.fulfill({ json: { title: 'Downloads', protocol: 'downloader', navigation: [], pagination: [],
        searches: [], groups: [], facets: [],
        publications: options.noSources ? [book({ navigation: [] })]
          : [book(), book({ identity: 'owned', title: 'Owned Book', library_book_id: 1 })] } });
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
  expect(posted[0]).toMatchObject({ connection_id: 'dl', offer_id: 'offer-r1', add_to_my_library: true });
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
  await expect(page.getByRole('alert').filter({ hasText: 'Book search is not available right now' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
});

test('an admin is told when no download source can be searched yet', async ({ page }) => {
  await fixture(page, { noSources: true });
  await page.goto('/app/?q=moby&scope=get');
  const notice = page.getByTestId('get-more-no-sources');
  await expect(notice).toContainText('No download source can be searched yet');
  await expect(notice.getByRole('link', { name: 'Open Downloads' })).toHaveAttribute('href', /\/admin\/downloads$/);
  await expect(page.getByRole('button', { name: 'Find downloads' })).toHaveCount(0);
});

test('a reader without admin rights is told to ask an administrator', async ({ page }) => {
  await fixture(page, { noSources: true, admin: false });
  await page.goto('/app/?q=moby&scope=get');
  const notice = page.getByTestId('get-more-no-sources');
  await expect(notice).toContainText('Ask an administrator');
  await expect(notice.getByRole('link')).toHaveCount(0);
});

test('Admin → Downloads names a switched-on source that is missing its address', async ({ page }) => {
  await page.route('**/api/v1/admin/downloads**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/queue')) return route.fulfill({ json: { tasks: [] } });
    if (url.pathname.endsWith('/settings')) return route.fulfill({ json: { groups: [], tabs: [] } });
    return route.fulfill({ json: {
      enabled: true, connection: { ...downloader, config: { allow_mobi: false, metadata_provider: 'openlibrary' } },
      sources: { ebook: [], audiobook: [] },
      source_problems: [{ source: 'direct_download', reason: 'needs_mirror' },
                        { source: 'audiobookbay', reason: 'needs_hostname' }],
      acquisition: { enabled: true, migration_status: 'ready', runtime: { available: true, reasons: [] } },
    } });
  });
  await page.goto('/app/admin/downloads');
  const problems = page.getByTestId('downloads-source-problems');
  await expect(problems).toContainText("Add an Anna's Archive mirror URL under Mirrors below.");
  await expect(problems).toContainText('AudiobookBay is switched on but has no hostname.');
  await expect(page.getByText('No ebook source can be searched yet.')).toBeVisible();
  await expectNoSeriousViolations(page);
});

test('an audiobook is found and requested from the same search', async ({ page }) => {
  const { posted } = await fixture(page);
  await page.goto('/app/?q=moby&scope=get');
  const moby = page.getByRole('listitem').filter({ hasText: 'Herman Melville' }).first();
  await moby.getByRole('button', { name: 'Find audiobooks' }).click();
  const files = page.getByRole('list', { name: 'Downloads for Moby Dick' });
  await expect(files.getByText('480 MB · AudiobookBay · EN')).toBeVisible();
  await files.getByRole('button', { name: 'Download' }).click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toMatchObject({ offer_id: 'offer-a1' });
});

test('Admin → Downloads switches downloads on and edits the engine settings', async ({ page }) => {
  const saved: { tab: string; values: unknown }[] = [];
  const patches: unknown[] = [];
  let state = { enabled: false, config: { allow_mobi: false, metadata_provider: 'openlibrary' } };
  const status = () => ({
    enabled: state.enabled,
    connection: state.enabled ? { ...downloader, enabled: true, config: state.config } : null,
    sources: { ebook: ['direct_download'], audiobook: [] },
    acquisition: { enabled: true, migration_status: 'ready', runtime: { available: true, reasons: [] } },
  });
  await page.route('**/api/v1/admin/downloads**', async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.pathname.endsWith('/queue')) {
      return route.fulfill({ json: { tasks: [{ id: 'aaa', status: 'downloading', title: 'Moby Dick',
        source: "Anna's Archive", progress: 40, message: null, added: 1 }] } });
    }
    if (url.pathname.endsWith('/settings')) {
      return route.fulfill({ json: { groups: [], tabs: [
        { name: 'download_sources', displayName: 'Download Sources', order: 1, fields: [
          { key: 'DIRECT_DOWNLOAD_ENABLED', type: 'CheckboxField', label: "Enable Anna's Archive", value: true },
          { key: 'AA_DONATOR_KEY', type: 'PasswordField', label: 'Donator key', value: '', hasValue: true },
        ] },
        { name: 'cloudflare_bypass', displayName: 'Cloudflare Bypass', order: 2, fields: [
          { key: 'USE_CF_BYPASS', type: 'CheckboxField', label: 'Use the bypass', value: true },
          { key: 'USING_EXTERNAL_BYPASSER', type: 'CheckboxField', label: 'Use FlareSolverr', value: false },
          { key: 'EXT_BYPASSER_URL', type: 'TextField', label: 'FlareSolverr address', value: '',
            showWhen: { field: 'USING_EXTERNAL_BYPASSER', value: true } },
          { key: 'test_bypasser', type: 'ActionButton', label: 'Test connection', style: 'primary' },
        ] },
      ] } });
    }
    if (url.pathname.includes('/action/')) return route.fulfill({ json: { success: false, message: 'FlareSolverr did not answer' } });
    if (method === 'PUT') {
      saved.push({ tab: url.pathname.split('/').pop()!, values: route.request().postDataJSON() });
      return route.fulfill({ json: { success: true, message: 'Settings saved' } });
    }
    if (method === 'PATCH') {
      const body = route.request().postDataJSON();
      patches.push(body);
      state = { enabled: body.enabled ?? state.enabled, config: { ...state.config, ...body } };
      delete (state.config as Record<string, unknown>).enabled;
    }
    return route.fulfill({ json: status() });
  });

  await page.goto('/app/admin/downloads');
  await expect(page.getByRole('heading', { name: 'Downloads', level: 1 })).toBeVisible();
  await page.getByLabel('Allow downloads').check();
  await expect.poll(() => patches).toEqual([{ enabled: true }]);
  await expect(page.getByText("Ebooks come from: Anna's Archive.")).toBeVisible();
  await expect(page.getByText('Moby Dick')).toBeVisible();
  await page.getByLabel('Search books with').selectOption('hardcover');
  await expect.poll(() => patches.length).toBe(2);
  expect(patches[1]).toEqual({ metadata_provider: 'hardcover' });

  // A stored secret is never shown; blank keeps it.
  await expect(page.getByLabel('Donator key')).toHaveValue('');
  await expect(page.getByLabel('Donator key')).toHaveAttribute('placeholder', 'Saved. Leave blank to keep it.');
  await expectNoSeriousViolations(page);

  await page.getByRole('button', { name: 'Cloudflare Bypass' }).click();
  // Fields follow the engine's own visibility rules.
  await expect(page.getByLabel('FlareSolverr address')).toHaveCount(0);
  await page.getByLabel('Use FlareSolverr').check();
  await page.getByLabel('FlareSolverr address').fill('http://flaresolverr:8191');
  await page.getByRole('button', { name: 'Test connection' }).click();
  await expect(page.getByText('FlareSolverr did not answer')).toBeVisible();
  await page.getByRole('button', { name: 'Save' }).click();
  await expect.poll(() => saved).toEqual([{ tab: 'cloudflare_bypass',
    values: { USING_EXTERNAL_BYPASSER: true, EXT_BYPASSER_URL: 'http://flaresolverr:8191' } }]);
});
