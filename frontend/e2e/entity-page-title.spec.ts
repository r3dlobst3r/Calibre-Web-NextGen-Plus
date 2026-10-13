import { test, expect } from '@playwright/test';

test('direct entity page updates the tab title after its delayed name query (#892)', async ({ page }) => {
  await page.goto('/app');
  const response = await page.request.get('/api/v1/series');
  expect(response.ok()).toBeTruthy();
  const first = (await response.json())?.items?.[0];
  test.skip(!first, 'seed has no series');

  await page.route('**/api/v1/series', async (route) => {
    const upstream = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.fulfill({ response: upstream });
  });
  await page.goto(`/app/series/${first.id}`);
  await expect(page.locator('main h1')).toHaveText(first.name);
  await expect(page).toHaveTitle(`${first.name} · Calibre-Web NextGen+`);
  expect(await page.title()).not.toContain('…');
});

test('failed or missing entities never leak a permanent ellipsis title (#892)', async ({ page }) => {
  await page.route('**/api/v1/series', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{}' }));
  await page.goto('/app/series/404404');
  await expect(page.locator('main h1')).toHaveText('Could not load this page');
  await expect(page).toHaveTitle('Could not load this page · Calibre-Web NextGen+');

  await page.unroute('**/api/v1/series');
  await page.route('**/api/v1/series', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[]}' }));
  await page.goto('/app/series/404404');
  await expect(page.locator('main h1')).toHaveText('Page not found');
  await expect(page).toHaveTitle('Page not found · Calibre-Web NextGen+');
});
