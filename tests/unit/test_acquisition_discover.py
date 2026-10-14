# SPDX-License-Identifier: GPL-3.0-or-later
"""Popular-book feeds for Discover: names, Open Library URLs, parsing and the cache. Offline."""
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from cps.services.acquisition import discover

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(discover, '_cache', {})


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_feed_names_are_a_closed_set():
    assert discover.parse_feed('popular:week') == ('popular', None, 'week')
    assert discover.parse_feed('genre:science_fiction:all') == ('genre', 'science_fiction', 'all')
    for bad in ('popular', 'popular:month', 'genre:astrology:week', 'genre:fantasy', 'genre:fantasy:week:x',
                'genre:fantasy language:eng:week', None, 'x' * 200):
        with pytest.raises(discover.FeedError):
            discover.parse_feed(bad)


def test_weekly_popular_uses_open_library_trending():
    assert discover.feed_url('popular:week').startswith('https://openlibrary.org/trending/weekly.json?')


@pytest.mark.parametrize('feed, sort, query', [
    ('popular:all', 'readinglog', 'language:eng'),
    ('genre:fantasy:week', 'trending', 'subject_key:fantasy language:eng'),
    ('genre:horror:all', 'readinglog', 'subject_key:horror language:eng'),
])
def test_other_feeds_search_by_subject_and_sort(feed, sort, query):
    url = urlparse(discover.feed_url(feed))
    params = parse_qs(url.query)
    assert url.netloc == 'openlibrary.org' and url.path == '/search.json'
    assert params['sort'] == [sort] and params['q'] == [query] and params['limit'] == [str(discover.LIMIT)]


def test_the_readers_language_narrows_the_list():
    assert parse_qs(urlparse(discover.feed_url('genre:fantasy:all', 'fr')).query)['q'] == [
        'subject_key:fantasy language:fre']
    assert parse_qs(urlparse(discover.feed_url('genre:fantasy:all', 'xx')).query)['q'] == [
        'subject_key:fantasy language:eng']


def test_records_come_back_in_the_metadata_shape_and_are_cached():
    calls = []
    def get(url, headers, timeout):
        calls.append(url)
        return Response({'works': [
            {'key': '/works/OL1W', 'title': 'Atomic Habits', 'author_name': ['James Clear'],
             'cover_i': 12539702, 'first_publish_year': 2016},
            {'key': '/works/OL2W'},  # no title: skipped
        ]})
    clock = iter([0, 10, discover.CACHE_SECONDS + 20, discover.CACHE_SECONDS + 20])
    now = lambda: next(clock)
    first = discover.records('popular:week', get=get, now=now)
    assert first == discover.records('popular:week', get=get, now=now)
    assert len(calls) == 1
    assert [r['title'] for r in first] == ['Atomic Habits']
    record = first[0]
    assert record['id'] == '/works/OL1W' and record['authors'] == ['James Clear']
    assert record['cover'] == 'https://covers.openlibrary.org/b/id/12539702-L.jpg'
    assert record['publishedDate'] == '2016' and record['source']['id'] == 'openlibrary'
    discover.records('popular:week', get=get, now=now)
    assert len(calls) == 2  # an hour later the list is fetched again


def test_search_results_parse_too():
    get = lambda url, headers, timeout: Response({'docs': [{'key': '/works/OL3W', 'title': 'Dune'}]})
    assert [r['title'] for r in discover.records('genre:science_fiction:all', get=get)] == ['Dune']


def test_the_page_offers_exactly_the_genres_the_server_accepts():
    page = (Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'pages' / 'DiscoverBooks.tsx').read_text()
    for key, label in discover.GENRES:
        assert f"'{key}'" in page and f"'{label}'" in page, key
