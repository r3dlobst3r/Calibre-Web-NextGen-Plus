import assert from 'node:assert/strict';
import test from 'node:test';

import {
  changedValues,
  collectValues,
  fieldDisabled,
  isFieldVisible,
  moveItem,
  newTableRow,
  orderedItems,
  orderTabs,
  type SettingsField,
  type SettingsTab,
} from '../src/lib/downloadSettings.ts';

// These rules mirror Shelfmark's own settings screen. A field that shows here
// but not there (or saves differently) would be a setting the administrator
// cannot predict, so each rule is pinned to Shelfmark's behaviour.

const field = (overrides: Partial<SettingsField>): SettingsField => ({ key: 'K', type: 'TextField', ...overrides });

test('a condition can name a field from another tab', () => {
  const tabs: SettingsTab[] = [
    { name: 'search_mode', displayName: 'Search', fields: [field({ key: 'SEARCH_MODE', type: 'SelectField', value: 'direct' })] },
    { name: 'hardcover', displayName: 'Hardcover', fields: [field({ key: 'HARDCOVER_API_KEY', type: 'PasswordField', value: '' })] },
  ];
  const values = collectValues(tabs);
  assert.equal(values.SEARCH_MODE, 'direct');
  assert.equal(isFieldVisible(field({ showWhen: { field: 'SEARCH_MODE', value: 'universal' } }), values), false);
  assert.equal(isFieldVisible(field({ showWhen: { field: 'SEARCH_MODE', value: ['direct', 'x'] } }), values), true);
});

test('every condition in a list must hold', () => {
  const values = { A: 'on', B: 'off' };
  assert.equal(isFieldVisible(field({ showWhen: [{ field: 'A', value: 'on' }, { field: 'B', value: 'on' }] }), values), false);
  assert.equal(isFieldVisible(field({ showWhen: [{ field: 'A', value: 'on' }, { field: 'B', value: 'off' }] }), values), true);
});

test('notEmpty checks for any value, including in lists', () => {
  const condition = { showWhen: { field: 'LIST', notEmpty: true } };
  assert.equal(isFieldVisible(field(condition), { LIST: [] }), false);
  assert.equal(isFieldVisible(field(condition), { LIST: ['x'] }), true);
  assert.equal(isFieldVisible(field(condition), { LIST: '' }), false);
});

test('boolean conditions compare as Shelfmark does: exactly for one value, as text in a list', () => {
  assert.equal(isFieldVisible(field({ showWhen: { field: 'ON', value: true } }), { ON: true }), true);
  assert.equal(isFieldVisible(field({ showWhen: { field: 'ON', value: ['true'] } }), { ON: true }), true);
});

test('universal-only fields hide in direct search mode, and hidden fields always hide', () => {
  assert.equal(isFieldVisible(field({ universalOnly: true }), { SEARCH_MODE: 'direct' }), false);
  assert.equal(isFieldVisible(field({ universalOnly: true }), {}), true);
  assert.equal(isFieldVisible(field({ hiddenInUi: true }), {}), false);
});

test('a field set by the environment is locked and never sent', () => {
  const env = field({ key: 'URL', fromEnv: true, value: 'a' });
  assert.deepEqual(fieldDisabled(env, {}), { disabled: true, reason: 'fromEnv' });
  const tab: SettingsTab = { name: 't', displayName: 'T', fields: [env] };
  assert.deepEqual(changedValues(tab, { URL: 'a' }, { URL: 'b' }), {});
});

test('disabledWhen locks a field with its reason', () => {
  const locked = field({ disabledWhen: { field: 'MODE', value: ['off'], reason: 'Turn it on first' } });
  assert.deepEqual(fieldDisabled(locked, { MODE: 'off' }), { disabled: true, reason: 'Turn it on first' });
  assert.deepEqual(fieldDisabled(locked, { MODE: 'on' }), { disabled: false });
});

test('saving sends only changes, and a blank password keeps the stored one', () => {
  const tab: SettingsTab = { name: 'prowlarr', displayName: 'Prowlarr', fields: [
    field({ key: 'URL', value: 'http://a' }),
    field({ key: 'KEY', type: 'PasswordField', value: '', hasValue: true }),
    field({ key: 'LIST', type: 'MultiSelectField', value: ['x'] }),
    field({ key: 'test', type: 'ActionButton' }),
  ] };
  const saved = collectValues([tab]);
  assert.deepEqual(changedValues(tab, saved, { ...saved }), {});
  assert.deepEqual(changedValues(tab, saved, { ...saved, URL: 'http://b', LIST: ['x'] }), { URL: 'http://b' });
  assert.deepEqual(changedValues(tab, saved, { ...saved, KEY: 'new-secret' }), { KEY: 'new-secret' });
  assert.deepEqual(changedValues(tab, saved, { ...saved, LIST: ['x', 'y'] }), { LIST: ['x', 'y'] });
});

test('a custom widget’s bound values are tracked like any other field', () => {
  const tab: SettingsTab = { name: 't', displayName: 'T', fields: [
    { key: 'w', type: 'CustomComponentField', boundFields: [field({ key: 'BOUND', value: 1 })] },
  ] };
  assert.deepEqual(collectValues([tab]), { BOUND: 1 });
  assert.deepEqual(changedValues(tab, { BOUND: 1 }, { BOUND: 2 }), { BOUND: 2 });
});

test('an orderable list keeps stored order and adds new options switched off', () => {
  const options = [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }, { id: 'c', label: 'C', isPinned: true }];
  const items = orderedItems([{ id: 'b', enabled: true }, { id: 'gone', enabled: true }, { id: 'a', enabled: false }], options);
  assert.deepEqual(items, [{ id: 'b', enabled: true }, { id: 'a', enabled: false }, { id: 'c', enabled: false }]);
  assert.deepEqual(moveItem(items, 1, -1, options).map((i) => i.id), ['a', 'b', 'c']);
  // Pinned items hold their place, and the ends do not wrap.
  assert.equal(moveItem(items, 1, 1, options), items);
  assert.equal(moveItem(items, 0, -1, options), items);
});

test('a new table row starts from each column default', () => {
  assert.deepEqual(newTableRow([
    { key: 'name', label: 'Name', type: 'text' },
    { key: 'on', label: 'On', type: 'checkbox' },
    { key: 'tags', label: 'Tags', type: 'multiselect' },
    { key: 'kind', label: 'Kind', type: 'select', defaultValue: 'x' },
  ]), { name: '', on: false, tags: [], kind: 'x' });
});

test('tabs follow Shelfmark’s order, then their names', () => {
  const ordered = orderTabs([
    { name: 'b', displayName: 'Beta', order: 5, fields: [] },
    { name: 'a', displayName: 'Alpha', order: 5, fields: [] },
    { name: 'c', displayName: 'Gamma', order: 1, fields: [] },
  ]);
  assert.deepEqual(ordered.map((tab) => tab.name), ['c', 'a', 'b']);
});
