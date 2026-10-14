# SPDX-License-Identifier: GPL-3.0-or-later
"""Calibre-Web NextGen+ downloads as a book source: search, releases and offers.

A search finds books with one of CWNG's metadata providers; "Find downloads" asks
the download engine (Shelfmark's, vendored in cps/services/downloader) to search
its sources for one book; each importable release becomes an opaque, owner-bound
offer. The acquisition worker then has the engine download it and imports the
file through ingest. The engine is imported lazily so this module, like the rest
of the acquisition package, loads without the application.
"""
import hashlib
import json
import re

from .contracts import AUDIOBOOK_FORMATS, DIRECT_FORMATS, MOBI_MEDIA_TYPE
from .http import TransportError
from .storage import NotFound


class DownloaderError(TransportError):
    pass


ADAPTER = 'downloader'
CONTENT_TYPES = ('ebook', 'audiobook')
# Release format -> media type. An unknown format is still offered; its bytes decide.
FORMAT_MEDIA = {'epub': 'application/epub+zip', 'pdf': 'application/pdf', 'mobi': MOBI_MEDIA_TYPE,
                'm4b': 'audio/x-m4b', 'm4a': 'audio/mp4'}
UNKNOWN_MEDIA = 'application/octet-stream'
ACTIVE_STATES = ('queued', 'resolving', 'locating', 'downloading')
PAGE_SIZE = 20
MAX_RELEASES = 60
METADATA_PROVIDERS = ('openlibrary', 'hardcover', 'google')


def connection_config(value):
    """The downloader's own options. Sources and clients live in the engine settings."""
    if not isinstance(value, dict) or set(value) - {'allow_mobi', 'metadata_provider'}:
        raise DownloaderError('invalid_connection')
    allow_mobi = value.get('allow_mobi', False)
    provider = value.get('metadata_provider', 'openlibrary')
    if not isinstance(allow_mobi, bool) or provider not in METADATA_PROVIDERS:
        raise DownloaderError('invalid_connection')
    return {'allow_mobi': allow_mobi, 'metadata_provider': provider}


def local_engine(config):
    from ..downloader.runtime import LocalEngine
    return LocalEngine(config)


def _text(value, limit=512):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _language(value):
    """A release language as a short code: Anna's Archive says "english [en]"."""
    text = _text(value, 64)
    if text is None:
        return None
    code = re.search(r'\[([a-z]{2,3}(?:-[a-z0-9]+)?)\]', text, re.IGNORECASE)
    return (code.group(1) if code else text)[:32]


def _year(value):
    match = re.match(r'\s*(\d{4})', str(value or ''))
    return int(match.group(1)) if match and 0 < int(match.group(1)) < 10000 else None


def _isbn(record):
    found = {}
    for key, value in (record.get('identifiers') or {}).items():
        digits = re.sub(r'[^0-9Xx]', '', str(value))
        if key.lower().startswith('isbn') and len(digits) in (10, 13):
            found['isbn_13' if len(digits) == 13 else 'isbn_10'] = digits.upper()
    return found


def cover_url(value):
    return value if isinstance(value, str) and re.match(r'https?://', value) and len(value) <= 4096 else None


def media_type(release, content_type):
    fmt = release.get('format')
    if fmt is None or (isinstance(fmt, str) and not fmt.strip()):
        return UNKNOWN_MEDIA
    media = FORMAT_MEDIA.get(fmt.strip().lower().lstrip('.')) if isinstance(fmt, str) else None
    # An ebook search never imports an audio file, and the other way round.
    if media is not None and (media in AUDIOBOOK_FORMATS) != (content_type == 'audiobook'):
        return None
    return media


def release_key(connection_id, release):
    return hashlib.sha256(json.dumps([connection_id, release.get('source'), release.get('source_id')]).encode()).hexdigest()[:64]


def download_payload(book, release, content_type):
    """The release as the engine's queue expects it (what Shelfmark's own UI sends)."""
    authors = [a for a in book.get('authors') or [] if isinstance(a, str)]
    payload = {
        'source': release['source'], 'source_id': release['source_id'],
        'title': book.get('title') or release.get('title'), 'author': ', '.join(authors) or None,
        'year': book.get('publish_year'), 'format': release.get('format'), 'size': release.get('size'),
        'size_bytes': release.get('size_bytes'), 'download_url': release.get('download_url'),
        'protocol': release.get('protocol'), 'indexer': release.get('indexer'),
        'seeders': release.get('seeders'),
        'extra': release.get('extra') if isinstance(release.get('extra'), dict) else {},
        'content_type': content_type, 'series_name': book.get('series_name'),
        'series_position': book.get('series_position'), 'language': release.get('language'),
    }
    return {key: value for key, value in payload.items() if value is not None}


def release_dict(release):
    """An engine Release (dataclass) or an already-plain dict, as a plain dict."""
    if isinstance(release, dict):
        return release
    from dataclasses import asdict
    return asdict(release)


class DownloaderService:
    """Catalog-shaped browsing over metadata search and the engine's sources."""

    def __init__(self, repository, *, book_search=None, discover=None, engine_factory=local_engine):
        self.repository, self.book_search, self.engine_factory = repository, book_search, engine_factory
        self.discover = discover

    def probe(self, config):
        config = connection_config(config)
        engine = self.engine_factory(config)
        return {'title': 'Downloads', 'protocol': ADAPTER, 'browse': True, 'search_advertised': True,
                'direct_download_advertised': True,
                'sources': {kind: engine.sources(kind) for kind in CONTENT_TYPES}}

    def _catalog(self, title):
        return {'title': title, 'protocol': ADAPTER, 'publications': [], 'navigation': [],
                'pagination': [], 'searches': [], 'groups': [], 'facets': []}

    def browse(self, owner_id, connection_id, *, selection=None, query=None, feed=None):
        snapshot = self.repository.connection_config(connection_id)
        config, revision = snapshot.config, snapshot.revision
        if selection is not None:
            payload = self.repository.offer_payload(owner_id, selection, connection_id).offer
            kind = payload.get('kind')
            if kind == 'downloader_book' and query is None:
                return self._releases(owner_id, connection_id, revision, config, payload)
            if kind != 'downloader_search':
                raise NotFound('Search selection is unavailable')
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 500):
            raise DownloaderError('invalid_search')
        if feed is not None:
            return self._feed(owner_id, connection_id, revision, config, feed)
        result = self._catalog('Downloads')
        search = self.repository.create_offer(owner_id, connection_id, {'kind': 'downloader_search'},
                                              expected_revision=revision)
        result['searches'].append({'title': '', 'selection': search})
        if query is None:
            return result
        if self.book_search is None:
            raise DownloaderError('search_unavailable')
        engine = self.engine_factory(config)
        kinds = [kind for kind in CONTENT_TYPES if engine.sources(kind)]
        records = self.book_search(query.strip(), config.get('metadata_provider', 'openlibrary')) or []
        for record in records[:100]:
            if isinstance(record, dict) and _text(record.get('title')):
                result['publications'].append(self._book(owner_id, connection_id, revision, record, kinds))
        result['content_types'] = kinds
        return result

    def _feed(self, owner_id, connection_id, revision, config, feed):
        """A list of popular books (see discover.py), shaped like a search result."""
        from .discover import FeedError, parse_feed
        try:
            parse_feed(feed)
        except FeedError:
            raise DownloaderError('invalid_search') from None
        if self.discover is None:
            raise DownloaderError('search_unavailable')
        try:
            records = self.discover(feed) or []
        except Exception:
            raise DownloaderError('search_unavailable') from None
        engine = self.engine_factory(config)
        kinds = [kind for kind in CONTENT_TYPES if engine.sources(kind)]
        result = self._catalog('Downloads')
        result['feed'] = feed
        for record in records[:100]:
            if isinstance(record, dict) and _text(record.get('title')):
                result['publications'].append(self._book(owner_id, connection_id, revision, record, kinds))
        result['content_types'] = kinds
        return result

    def _book(self, owner_id, connection_id, revision, record, kinds):
        authors = [a[:256] for a in record.get('authors') or [] if isinstance(a, str) and a.strip()][:20]
        source = record.get('source') if isinstance(record.get('source'), dict) else {}
        series = _text(record.get('series'), 256)
        position = record.get('series_index')
        languages = [code for code in (record.get('languages') or []) if isinstance(code, str)][:5]
        book = {'provider': _text(source.get('id'), 64) or 'cwng', 'provider_id': str(record.get('id') or '')[:256],
                'title': _text(record.get('title')), 'authors': authors,
                'publish_year': _year(record.get('publishedDate')), 'series_name': series,
                'series_position': position if series and isinstance(position, (int, float)) and position else None,
                'language': languages[0] if languages else None, **_isbn(record)}
        navigation = []
        for kind in kinds:
            selection = self.repository.create_offer(owner_id, connection_id,
                {'kind': 'downloader_book', 'content_type': kind, 'book': book}, expected_revision=revision)
            navigation.append({'title': 'Find audiobooks' if kind == 'audiobook' else 'Find downloads',
                               'relations': ['downloader:' + kind], 'selection': selection})
        return {
            'title': book['title'], 'authors': authors, 'languages': languages, 'offers': [],
            'identity': hashlib.sha256(json.dumps([book['provider'], book['provider_id'], book['title']]).encode()).hexdigest()[:32],
            'description': _text(re.sub(r'<[^>]+>', ' ', record.get('description') or ''), 4000),
            'navigation': navigation, 'cover_url': cover_url(record.get('cover')),
            'year': book['publish_year'], 'subtitle': None,
            'series': {'name': series, 'position': book['series_position']} if series else None,
            'isbn': [i for i in (book.get('isbn_13'), book.get('isbn_10')) if i],
            'source': _text(source.get('description'), 64),
        }

    def _releases(self, owner_id, connection_id, revision, config, payload):
        book = payload.get('book') if isinstance(payload.get('book'), dict) else {}
        content_type = payload.get('content_type') if payload.get('content_type') in CONTENT_TYPES else 'ebook'
        engine = self.engine_factory(config)
        releases, errors, _sources = engine.releases(book, content_type=content_type)
        result = self._catalog(_text(book.get('title')) or 'Downloads')
        result['content_type'] = content_type
        result['errors'] = [_text(e, 300) for e in errors if _text(e, 300)][:10]
        for release in [release_dict(r) for r in releases][:MAX_RELEASES]:
            if not _text(release.get('source'), 64) or not _text(release.get('source_id'), 512):
                continue
            result['publications'].append(self._release(owner_id, connection_id, revision, config, book,
                                                        release, content_type))
        return result

    def _release(self, owner_id, connection_id, revision, config, book, release, content_type):
        key = release_key(connection_id, release)
        media = media_type(release, content_type)
        language = _language(release.get('language'))
        size = _text(release.get('size'), 32)
        source = _text(release.get('indexer'), 64) or _text(release.get('source'), 64)
        publication = {'title': _text(release.get('title')) or _text(book.get('title')) or 'Untitled release',
            'identity': key, 'authors': [], 'languages': [language] if language else [], 'description': None,
            'offers': [], 'navigation': [], 'format': _text(release.get('format'), 16), 'size': size,
            'source': source, 'protocol': _text(release.get('protocol'), 16),
            'seeders': release.get('seeders') if isinstance(release.get('seeders'), int) else None}
        if media is None or media == MOBI_MEDIA_TYPE and config.get('allow_mobi') is not True:
            publication['unavailable_reason'] = 'unsupported_format'
            return publication
        selection = self.repository.create_offer(owner_id, connection_id, {
            'kind': 'acquisition', 'transport': ADAPTER, 'media_type': media, 'content_type': content_type,
            'title': _text(book.get('title')) or publication['title'], 'release_key': key,
            'task_id': release['source_id'], 'release': download_payload(book, release, content_type)},
            expected_revision=revision)
        label = ' · '.join(part for part in (size, source) if part) or None
        known = {**DIRECT_FORMATS, **AUDIOBOOK_FORMATS}
        publication['offers'] = [{'format': known[media][0] if media in known else 'Unknown',
            'label': label, 'identity': key, 'relation': 'download', 'offer_id': selection}]
        return publication


# -- engine settings for Admin → Downloads -----------------------------------------

# The engine settings Admin → Downloads shows, tab by tab. A tab listed with a set
# shows only those keys; with None, all of it except HIDDEN_KEYS. Output, naming,
# email, Booklore and the engine's own UI options are absent on purpose: CWNG
# imports every download itself and has its own interface.
SETTINGS_FIELDS = {
    'download_sources': None,
    'mirrors': None,
    'libgen_config': None,
    'audiobookbay_config': None,
    'prowlarr_clients': None,
    'cloudflare_bypass': None,
    'network': None,
    'general': {'SUPPORTED_FORMATS', 'SUPPORTED_AUDIOBOOK_FORMATS'},
    'search_mode': {'BOOK_LANGUAGE'},
    'downloads': {'MAX_CONCURRENT_DOWNLOADS'},
    'advanced': {'DEBUG', 'LOG_LEVEL', 'DOWNLOAD_CLIENT_COMPLETED_PATH_TIMEOUT', 'PROWLARR_REMOTE_PATH_MAPPINGS'},
}
SETTINGS_TABS = tuple(SETTINGS_FIELDS)
# Fixed by CWNG (where files go, how they are sorted), never offered.
HIDDEN_KEYS = frozenset({'AA_CONTENT_TYPE_ROUTING', 'AA_CONTENT_TYPE_DIR_FICTION', 'AA_CONTENT_TYPE_DIR_NON_FICTION',
    'AA_CONTENT_TYPE_DIR_UNKNOWN', 'AA_CONTENT_TYPE_DIR_MAGAZINE', 'AA_CONTENT_TYPE_DIR_COMIC',
    'AA_CONTENT_TYPE_DIR_STANDARDS', 'AA_CONTENT_TYPE_DIR_MUSICAL_SCORE', 'AA_CONTENT_TYPE_DIR_OTHER'})
SKIPPED_COMPONENTS = frozenset({'users_management', 'oidc_admin_hint', 'oidc_env_info',
    'request_policy_grid', 'naming_template', 'settings_label'})


def field_shown(tab, key):
    allowed = SETTINGS_FIELDS.get(tab)
    return key not in HIDDEN_KEYS and (allowed is None or key in allowed)


def _redact_field(field):
    field = dict(field)
    if field.get('type') == 'PasswordField':
        field['hasValue'] = bool(field.get('value'))
        field['value'] = ''
    if isinstance(field.get('boundFields'), list):
        field['boundFields'] = [_redact_field(f) for f in field['boundFields'] if isinstance(f, dict)]
    return field


def visible_settings(document, *, locked=()):
    """The engine settings CWNG administers, stored secrets withheld, CWNG-owned keys hidden."""
    tabs = document.get('tabs') if isinstance(document, dict) else None
    if not isinstance(tabs, list):
        raise DownloaderError('invalid_settings')
    shown = []
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get('name') not in SETTINGS_TABS:
            continue
        name = tab['name']
        restricted = SETTINGS_FIELDS[name] is not None
        fields = [_redact_field(f) for f in tab.get('fields') or [] if isinstance(f, dict)
                  and not (restricted and f.get('type') == 'HeadingField')
                  and (f.get('type') == 'HeadingField' or field_shown(name, f.get('key')))
                  and f.get('key') not in locked
                  and not (f.get('type') == 'CustomComponentField' and f.get('component') in SKIPPED_COMPONENTS)]
        if any(f.get('type') != 'HeadingField' for f in fields):
            shown.append(dict(tab, fields=fields))
    shown.sort(key=lambda tab: SETTINGS_TABS.index(tab['name']))
    return {'tabs': shown, 'groups': [g for g in document.get('groups') or [] if isinstance(g, dict)]}


def settings_values(tab, values, *, locked=()):
    """Validate a tab update. An empty password keeps the stored one (the engine's rule)."""
    if tab not in SETTINGS_TABS:
        raise NotFound('Settings tab is unavailable')
    if not isinstance(values, dict) or len(values) > 200 or any(
            not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,128}', k) or k in locked
            or not field_shown(tab, k) for k in values):
        raise DownloaderError('invalid_request')
    if len(json.dumps(values)) > 512 * 1024:
        raise DownloaderError('invalid_request')
    return values
