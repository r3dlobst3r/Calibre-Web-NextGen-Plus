import assert from 'node:assert/strict';
import test from 'node:test';

import { searchQueryFromQuery, searchScopeFromQuery, searchScopeHref } from '../src/lib/searchScope.ts';

// The top-bar search has always meant the library. The download tab is an
// addition, so every URL that existed before it must still mean the library.

test('a plain search is the library', () => {
  assert.equal(searchScopeFromQuery('?q=dune'), 'library');
  assert.equal(searchScopeFromQuery(''), 'library');
});

test('only scope=get opens the download tab', () => {
  assert.equal(searchScopeFromQuery('?q=dune&scope=get'), 'get');
  assert.equal(searchScopeFromQuery('?q=dune&scope=GET'), 'library');
  assert.equal(searchScopeFromQuery('?q=dune&scope=anything'), 'library');
});

test('switching tabs keeps the search and every other parameter', () => {
  assert.equal(searchScopeHref('?q=dune&page=2', 'get'), '/?q=dune&page=2&scope=get');
  assert.equal(searchScopeHref('?q=dune&scope=get', 'library'), '/?q=dune');
  assert.equal(searchScopeHref('?q=two+words&scope=get', 'get'), '/?q=two+words&scope=get');
});

test('the library tab of an empty search is the plain home page', () => {
  assert.equal(searchScopeHref('?scope=get', 'library'), '/');
});

test('the search text is trimmed and missing text is empty', () => {
  assert.equal(searchQueryFromQuery('?q=%20dune%20'), 'dune');
  assert.equal(searchQueryFromQuery('?scope=get'), '');
});
