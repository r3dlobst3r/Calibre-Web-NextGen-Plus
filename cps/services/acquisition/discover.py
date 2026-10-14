# SPDX-License-Identifier: GPL-3.0-or-later
"""Popular books to browse when there is nothing to search for yet.

Open Library publishes what its readers are looking at: ``/trending/weekly``
for the whole site, and a ``trending`` or ``readinglog`` sort on its search for
one subject. A feed names one of those lists:

* ``popular:week`` / ``popular:all``
* ``genre:<subject>:week`` / ``genre:<subject>:all``

Records come back in the metadata providers' shape, so the downloader turns
them into the same books (with Find downloads) as a typed search. Each list is
cached for an hour: it changes slowly, and Open Library asks clients to be
gentle. The fetch runs on the caller's worker thread, never the request hub.
"""
import time
from dataclasses import asdict, is_dataclass
from urllib.parse import urlencode

#: Subjects offered as genres: Open Library subject key, English label.
GENRES = (
    ('fantasy', 'Fantasy'), ('science_fiction', 'Science fiction'), ('mystery', 'Mystery'),
    ('thriller', 'Thriller'), ('romance', 'Romance'), ('horror', 'Horror'),
    ('historical_fiction', 'Historical fiction'), ('young_adult_fiction', 'Young adult'),
    ('graphic_novels', 'Graphic novels'), ('classic_literature', 'Classics'), ('humor', 'Humor'),
    ('poetry', 'Poetry'), ('biography', 'Biography'), ('history', 'History'),
    ('science', 'Science'), ('business', 'Business'), ('self-help', 'Self-help'),
)
GENRE_KEYS = frozenset(key for key, _label in GENRES)
PERIODS = ('week', 'all')
LIMIT = 40
CACHE_SECONDS = 60 * 60
TIMEOUT = (3.05, 15)

# Open Library tags editions with MARC language codes.
_LANGUAGES = {'en': 'eng', 'fr': 'fre', 'nl': 'dut', 'de': 'ger', 'es': 'spa', 'it': 'ita', 'pt': 'por',
              'sv': 'swe', 'da': 'dan', 'no': 'nor', 'fi': 'fin', 'pl': 'pol', 'cs': 'cze', 'ru': 'rus',
              'ja': 'jpn', 'zh': 'chi', 'ko': 'kor'}

_cache = {}


class FeedError(ValueError):
    pass


def parse_feed(feed):
    """``(kind, subject, period)`` for a feed name, or FeedError."""
    parts = feed.split(':') if isinstance(feed, str) and len(feed) <= 80 else []
    if len(parts) == 2 and parts[0] == 'popular' and parts[1] in PERIODS:
        return 'popular', None, parts[1]
    if len(parts) == 3 and parts[0] == 'genre' and parts[1] in GENRE_KEYS and parts[2] in PERIODS:
        return 'genre', parts[1], parts[2]
    raise FeedError(feed)


def feed_url(feed, locale='en'):
    kind, subject, period = parse_feed(feed)
    from ...metadata_provider.openlibrary import OpenLibrary
    if kind == 'popular' and period == 'week':
        return 'https://openlibrary.org/trending/weekly.json?' + urlencode({'limit': LIMIT})
    language = _LANGUAGES.get((locale or 'en')[:2].lower(), 'eng')
    query = f'subject_key:{subject} language:{language}' if subject else f'language:{language}'
    return OpenLibrary.SEARCH_URL + '?' + urlencode({
        'q': query, 'sort': 'trending' if period == 'week' else 'readinglog',
        'fields': OpenLibrary.SEARCH_FIELDS, 'limit': LIMIT})


def records(feed, locale='en', *, get=None, now=time.monotonic):
    """The feed's books as metadata records (dicts), newest fetch within the hour."""
    url = feed_url(feed, locale)
    cached = _cache.get(url)
    if cached and now() - cached[0] < CACHE_SECONDS:
        return cached[1]
    from ... import constants
    from ...metadata_provider.openlibrary import OpenLibrary
    if get is None:
        import requests
        get = requests.get
    response = get(url, headers={'User-Agent': constants.USER_AGENT}, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json() or {}
    provider = OpenLibrary()
    found = []
    for doc in (payload.get('works') or payload.get('docs') or [])[:LIMIT]:
        record = provider._parse_doc(doc, generic_cover='', locale=locale) if isinstance(doc, dict) else None
        if record is not None:
            found.append(asdict(record) if is_dataclass(record) else record)
    _cache[url] = (now(), found)
    return found
