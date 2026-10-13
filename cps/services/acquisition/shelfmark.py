# SPDX-License-Identifier: GPL-3.0-or-later
"""Shelfmark sidecar: book search, release discovery and the download hand-off.

Shelfmark runs as a private companion service that only CWNG can reach. It owns the
sources (direct download, Prowlarr, IRC, ...), the Cloudflare bypass and its own
download clients; CWNG owns accounts, approval, staging and import. The browser never
talks to Shelfmark: every release reaches it as an opaque, owner-bound offer.

The endpoint is the administrator's own service on the container network, so this
uses a plain HTTP session rather than the catalog transport's public-network policy,
which deliberately refuses private addresses and verbs other than GET/POST.
"""
import base64
import binascii
import hashlib
import json
import os
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .contracts import DIRECT_FORMATS, MOBI_MEDIA_TYPE
from .http import TransportError
from .storage import NotFound


class ShelfmarkError(TransportError):
    pass


# Release format -> media type. Anything else (azw3, cbz, an unknown torrent) is not
# importable as-is; an unknown format is still offered, and its bytes decide.
FORMAT_MEDIA = {'epub': 'application/epub+zip', 'pdf': 'application/pdf', 'mobi': MOBI_MEDIA_TYPE}
UNKNOWN_MEDIA = 'application/octet-stream'
ACTIVE_STATES = ('queued', 'resolving', 'locating', 'downloading')
MAX_JSON_BYTES = 8 * 1024 * 1024
PAGE_SIZE = 20
MAX_RELEASES = 60

# Settings tabs CWNG shows. Everything about accounts, sign-in, notifications, where
# files go and how they are named is CWNG's job, so those tabs stay with Shelfmark.
SETTINGS_TABS = ('search_mode', 'metadata_providers', 'hardcover', 'openlibrary', 'googlebooks', 'moly',
    'download_sources', 'direct_download', 'mirrors', 'libgen_config', 'prowlarr_config', 'prowlarr_clients',
    'newznab_config', 'irc', 'audiobookbay_config', 'cloudflare_bypass', 'network', 'advanced')
# Custom widgets only Shelfmark's own UI can draw (user lists, OIDC hints, ...).
SKIPPED_COMPONENTS = frozenset({'users_management', 'oidc_admin_hint', 'oidc_env_info',
    'request_policy_grid', 'naming_template', 'settings_label'})


def connection_config(value):
    """Validate the administrator's Shelfmark address and API key, without a probe."""
    if not isinstance(value, dict) or set(value) - {'endpoint', 'secret', 'allow_mobi'}:
        raise ShelfmarkError('invalid_connection')
    endpoint, secret = value.get('endpoint'), value.get('secret')
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ShelfmarkError('invalid_connection')
    parts = urlsplit(endpoint.strip())
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ShelfmarkError('invalid_connection')
    if not isinstance(secret, str) or not secret.strip() or len(secret) > 512 or any(
            ord(c) < 33 or ord(c) == 127 for c in secret):
        raise ShelfmarkError('invalid_authentication')
    allow_mobi = value.get('allow_mobi', False)
    if not isinstance(allow_mobi, bool):
        raise ShelfmarkError('invalid_connection')
    return {'endpoint': urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', '')),
            'secret': secret, 'allow_mobi': allow_mobi}


def environment_config(environ=None):
    """The sidecar's address and key as the deployment provides them, or None."""
    environ = os.environ if environ is None else environ
    endpoint = (environ.get('SHELFMARK_URL') or '').strip()
    secret = (environ.get('SHELFMARK_API_KEY') or '').strip()
    if not endpoint or not secret:
        return None
    return connection_config({'endpoint': endpoint, 'secret': secret})


def _requests_session():
    import requests
    session = requests.Session()
    session.trust_env = False
    return session


class ShelfmarkClient:
    """Thin JSON client for Shelfmark's API, authenticated by its admin API key."""

    def __init__(self, config, *, session_factory=_requests_session, timeout=(5, 60)):
        self.config, self.session_factory, self.timeout = config, session_factory, timeout

    def _request(self, method, path, *, params=None, body=None, timeout=None, stream=False):
        import requests
        url = self.config['endpoint'] + path
        headers = {'Authorization': 'Bearer ' + self.config['secret'], 'Accept': 'application/json'}
        session = self.session_factory()
        try:
            response = session.request(method, url, params=params, json=body, headers=headers,
                timeout=timeout or self.timeout, allow_redirects=False, stream=stream)
        except requests.RequestException:
            session.close()
            raise ShelfmarkError('shelfmark_unreachable') from None
        if not stream:
            session.close()
        return response, session

    def _json(self, method, path, *, params=None, body=None, timeout=None, accepted=(200,)):
        response, _ = self._request(method, path, params=params, body=body, timeout=timeout)
        if response.status_code in (401, 403):
            raise ShelfmarkError('needs_auth')
        if response.status_code == 429:
            raise ShelfmarkError('source_busy')
        content = response.content or b''
        if len(content) > MAX_JSON_BYTES:
            raise ShelfmarkError('invalid_shelfmark_response')
        try:
            payload = json.loads(content) if content else {}
        except (ValueError, UnicodeError):
            raise ShelfmarkError('invalid_shelfmark_response') from None
        if response.status_code not in accepted:
            error = ShelfmarkError('shelfmark_unavailable' if response.status_code == 503 else 'shelfmark_error')
            # Shelfmark's own explanation ("No metadata provider configured", a source
            # outage). It names no credential; the API layer decides who may see it.
            message = payload.get('message') or payload.get('error') if isinstance(payload, dict) else None
            error.detail = message[:300] if isinstance(message, str) else None
            error.status = response.status_code
            raise error
        return payload

    # -- discovery -------------------------------------------------------------

    def health(self):
        return self._json('GET', '/api/health', timeout=(3, 10))

    def config_info(self):
        """An authenticated read: proves the API key, not just that the port answers."""
        return self._json('GET', '/api/config', timeout=(3, 15))

    def search(self, query, *, page=1, limit=PAGE_SIZE):
        return self._json('GET', '/api/metadata/search', params={
            'query': query, 'page': str(page), 'limit': str(limit), 'content_type': 'ebook'})

    def releases(self, provider, book_id, *, title=None, author=None):
        params = {'provider': provider, 'book_id': book_id, 'content_type': 'ebook'}
        if title:
            params['title'] = title
        if author:
            params['author'] = author
        # Release search runs every source under Shelfmark's own wall-clock budget.
        return self._json('GET', '/api/releases', params=params, timeout=(5, 150))

    # -- download queue --------------------------------------------------------

    def queue(self, release):
        try:
            return self._json('POST', '/api/releases/download', body=release, timeout=(5, 30))
        except ShelfmarkError as error:
            # The queue refuses a task it already holds. That is the lost-response
            # case this hand-off is built around, so it counts as submitted.
            if 'already in the download queue' in (getattr(error, 'detail', None) or ''):
                return {'status': 'queued'}
            raise

    def task(self, task_id):
        """(status, task) for one queue entry, or None when Shelfmark has no record."""
        status = self._json('GET', '/api/status', timeout=(5, 20))
        if not isinstance(status, dict):
            raise ShelfmarkError('invalid_shelfmark_response')
        for state, tasks in status.items():
            if isinstance(tasks, dict) and isinstance(tasks.get(task_id), dict):
                return state, tasks[task_id]
        return None

    def download(self, task_id, destination, *, max_bytes, checkpoint=lambda: None):
        """Stream a completed task's file into a new private path; return its sha256."""
        response, session = self._request('GET', '/api/localdownload', params={'id': task_id},
            timeout=(5, 60), stream=True)
        digest, total = hashlib.sha256(), 0
        try:
            if response.status_code in (401, 403):
                raise ShelfmarkError('needs_auth')
            if response.status_code == 404:
                raise ShelfmarkError('artifact_unavailable')
            if response.status_code != 200:
                raise ShelfmarkError('shelfmark_error')
            declared = response.headers.get('Content-Length')
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise ShelfmarkError('file_too_large')
            with destination.open('xb') as output:
                os.chmod(destination, 0o600)
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    checkpoint()
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ShelfmarkError('file_too_large')
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not total:
                raise ShelfmarkError('empty_response')
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            response.close()
            session.close()
        return digest.hexdigest()

    def discard(self, task_id):
        """Best effort: CWNG has its own copy, so Shelfmark may drop the file."""
        try:
            self._json('DELETE', '/api/localdownload', params={'id': task_id}, timeout=(3, 15))
        except ShelfmarkError:
            pass

    def cancel(self, task_id):
        try:
            self._json('DELETE', '/api/download/' + task_id + '/cancel', timeout=(3, 15))
        except ShelfmarkError:
            pass

    # -- administration --------------------------------------------------------

    def settings(self):
        return self._json('GET', '/api/settings', timeout=(5, 30))

    def update_settings(self, tab, values):
        return self._json('PUT', '/api/settings/' + tab, body=values, timeout=(5, 30), accepted=(200, 400))

    def settings_action(self, tab, action, values):
        return self._json('POST', '/api/settings/' + tab + '/action/' + action, body=values,
            timeout=(5, 90), accepted=(200, 400))

    def metadata_providers(self):
        """Each provider's enabled flag and the one book search really uses."""
        return self._json('GET', '/api/metadata/providers', timeout=(5, 30))

    def onboarding(self):
        return self._json('GET', '/api/onboarding', timeout=(5, 15))

    def skip_onboarding(self):
        return self._json('POST', '/api/onboarding/skip', body={}, timeout=(5, 15))


# -- presentation ---------------------------------------------------------------

def cover_url(value):
    """The original cover address, unwrapping Shelfmark's private cover-cache proxy."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value.startswith('/'):
        encoded = parse_qs(urlsplit(value).query).get('url', [''])[0]
        try:
            value = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8')
        except (binascii.Error, ValueError, UnicodeError):
            return None
    return value if urlsplit(value).scheme in ('http', 'https') else None


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
    return value if isinstance(value, int) and 0 < value < 10000 else None


def release_media_type(release):
    fmt = release.get('format')
    if fmt is None or (isinstance(fmt, str) and not fmt.strip()):
        return UNKNOWN_MEDIA
    return FORMAT_MEDIA.get(fmt.strip().lower().lstrip('.')) if isinstance(fmt, str) else None


def release_key(connection_id, release):
    return hashlib.sha256(json.dumps([connection_id, release.get('source'), release.get('source_id')]).encode()).hexdigest()[:64]


def download_payload(book, release):
    """The body Shelfmark's own UI posts to /api/releases/download for this release."""
    authors = [a for a in book.get('authors') or [] if isinstance(a, str)]
    payload = {
        'source': release['source'], 'source_id': release['source_id'],
        'title': book.get('title') or release.get('title'),
        'author': ', '.join(authors) or None, 'year': book.get('publish_year'),
        'format': release.get('format'), 'size': release.get('size'), 'size_bytes': release.get('size_bytes'),
        'download_url': release.get('download_url'), 'protocol': release.get('protocol'),
        'indexer': release.get('indexer'), 'seeders': release.get('seeders'),
        'extra': release.get('extra') if isinstance(release.get('extra'), dict) else {},
        'content_type': 'ebook', 'series_name': book.get('series_name'),
        'series_position': book.get('series_position'), 'subtitle': book.get('subtitle'),
        'language': release.get('language'),
    }
    return {key: value for key, value in payload.items() if value is not None}


class ShelfmarkService:
    """Catalog-shaped browsing over Shelfmark, with owner-bound opaque selections."""

    def __init__(self, repository, *, client_factory=ShelfmarkClient):
        self.repository, self.client_factory = repository, client_factory

    def probe(self, config):
        config = connection_config(config)
        client = self.client_factory(config)
        client.config_info()
        health = client.health()
        return {'title': 'Shelfmark', 'protocol': 'shelfmark', 'browse': True, 'search_advertised': True,
                'direct_download_advertised': True,
                'status': health.get('status') if isinstance(health, dict) else None}

    def _catalog(self, title):
        return {'title': title, 'protocol': 'shelfmark', 'publications': [], 'navigation': [],
                'pagination': [], 'searches': [], 'groups': [], 'facets': []}

    def browse(self, owner_id, connection_id, *, selection=None, query=None):
        snapshot = self.repository.connection_config(connection_id)
        config, revision = snapshot.config, snapshot.revision
        page = 1
        if selection is not None:
            payload = self.repository.offer_payload(owner_id, selection, connection_id).offer
            kind = payload.get('kind')
            if kind == 'shelfmark_book' and query is None:
                return self._releases(owner_id, connection_id, revision, config, payload)
            if kind == 'shelfmark_page' and query is None:
                query, page = payload['query'], payload['page']
            elif kind != 'shelfmark_search':
                raise NotFound('Search selection is unavailable')
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 500):
            raise ShelfmarkError('invalid_search')
        result = self._catalog('Shelfmark')
        search = self.repository.create_offer(owner_id, connection_id, {'kind': 'shelfmark_search'},
                                              expected_revision=revision)
        result['searches'].append({'title': '', 'selection': search})
        if query is None:
            return result
        found = self.client_factory(config).search(query.strip(), page=page)
        books = found.get('books') if isinstance(found, dict) else None
        if not isinstance(books, list):
            raise ShelfmarkError('invalid_shelfmark_response')
        for book in books[:100]:
            if not isinstance(book, dict) or not _text(book.get('provider'), 64) or not _text(book.get('provider_id'), 256):
                continue
            result['publications'].append(self._book(owner_id, connection_id, revision, book))
        if found.get('has_more') and page < 50:
            more = self.repository.create_offer(owner_id, connection_id,
                {'kind': 'shelfmark_page', 'query': query.strip(), 'page': page + 1}, expected_revision=revision)
            result['pagination'].append({'title': 'Next', 'relations': ['next'], 'selection': more})
        return result

    def _book(self, owner_id, connection_id, revision, book):
        authors = [a[:256] for a in book.get('authors') or [] if isinstance(a, str) and a.strip()][:20]
        keep = {key: book.get(key) for key in ('provider', 'provider_id', 'title', 'authors', 'publish_year',
                'series_name', 'series_position', 'subtitle') if book.get(key) is not None}
        selection = self.repository.create_offer(owner_id, connection_id,
            {'kind': 'shelfmark_book', 'book': keep}, expected_revision=revision)
        language = _text(book.get('language'), 32)
        series = _text(book.get('series_name'), 256)
        position = book.get('series_position')
        return {
            'title': _text(book.get('title')) or 'Untitled',
            'identity': hashlib.sha256(json.dumps([book['provider'], book['provider_id']]).encode()).hexdigest()[:32],
            'authors': authors, 'languages': [language] if language else [],
            'description': _text(book.get('description'), 4000), 'offers': [],
            'navigation': [{'title': 'Find downloads', 'relations': ['shelfmark:releases'], 'selection': selection}],
            'cover_url': cover_url(book.get('cover_url')), 'year': _year(book.get('publish_year')),
            'subtitle': _text(book.get('subtitle')),
            'series': {'name': series, 'position': position if isinstance(position, (int, float)) else None} if series else None,
            'isbn': [i for i in (_text(book.get('isbn_13'), 20), _text(book.get('isbn_10'), 20)) if i],
            'source': _text(book.get('provider_display_name'), 64),
        }

    def _releases(self, owner_id, connection_id, revision, config, payload):
        book = payload.get('book') if isinstance(payload.get('book'), dict) else {}
        authors = [a for a in book.get('authors') or [] if isinstance(a, str)]
        found = self.client_factory(config).releases(book.get('provider'), book.get('provider_id'),
            title=book.get('title'), author=', '.join(authors) or None)
        releases = found.get('releases') if isinstance(found, dict) else None
        if not isinstance(releases, list):
            raise ShelfmarkError('invalid_shelfmark_response')
        result = self._catalog(_text(book.get('title')) or 'Downloads')
        result['errors'] = [_text(e, 300) for e in found.get('errors') or [] if _text(e, 300)][:10]
        for release in releases[:MAX_RELEASES]:
            if not isinstance(release, dict) or not _text(release.get('source'), 64) or not _text(release.get('source_id'), 512):
                continue
            # Sources use this for their own categories ("fiction", "book (unknown)");
            # only an audiobook is something CWNG cannot import.
            if 'audio' in str(release.get('content_type') or '').lower():
                continue
            result['publications'].append(self._release(owner_id, connection_id, revision, config, book, release))
        return result

    def _release(self, owner_id, connection_id, revision, config, book, release):
        key = release_key(connection_id, release)
        media = release_media_type(release)
        language = _language(release.get('language'))
        size = _text(release.get('size'), 32)
        source = _text(release.get('indexer'), 64) or _text(release.get('source'), 64)
        publication = {'title': _text(release.get('title')) or _text(book.get('title')) or 'Untitled release',
            'identity': key, 'authors': [], 'languages': [language] if language else [], 'description': None,
            'offers': [], 'navigation': [], 'format': _text(release.get('format'), 16),
            'size': size, 'source': source, 'protocol': _text(release.get('protocol'), 16),
            'seeders': release.get('seeders') if isinstance(release.get('seeders'), int) else None}
        if media is None:
            publication['unavailable_reason'] = 'unsupported_format'
            return publication
        if media == MOBI_MEDIA_TYPE and config.get('allow_mobi') is not True:
            publication['unavailable_reason'] = 'unsupported_format'
            return publication
        selection = self.repository.create_offer(owner_id, connection_id, {
            'kind': 'acquisition', 'transport': 'shelfmark', 'media_type': media,
            'title': _text(book.get('title')) or publication['title'],
            'release_key': key, 'task_id': release['source_id'],
            'release': download_payload(book, release)}, expected_revision=revision)
        label = ' · '.join(part for part in (size, source) if part) or None
        publication['offers'] = [{'format': DIRECT_FORMATS[media][0] if media in DIRECT_FORMATS else 'Unknown',
            'label': label, 'identity': key, 'relation': 'download', 'offer_id': selection}]
        return publication


# -- settings proxy ---------------------------------------------------------------

def _redact_field(field):
    field = dict(field)
    if field.get('type') == 'PasswordField':
        field['hasValue'] = bool(field.get('value'))
        field['value'] = ''
    if isinstance(field.get('boundFields'), list):
        field['boundFields'] = [_redact_field(f) for f in field['boundFields'] if isinstance(f, dict)]
    return field


def visible_settings(document):
    """The Shelfmark settings tabs CWNG administers, with stored secrets withheld."""
    tabs = document.get('tabs') if isinstance(document, dict) else None
    if not isinstance(tabs, list):
        raise ShelfmarkError('invalid_shelfmark_response')
    shown = []
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get('name') not in SETTINGS_TABS:
            continue
        fields = [_redact_field(f) for f in tab.get('fields') or [] if isinstance(f, dict)
                  and not (f.get('type') == 'CustomComponentField' and f.get('component') in SKIPPED_COMPONENTS)]
        shown.append(dict(tab, fields=fields))
    shown.sort(key=lambda tab: (SETTINGS_TABS.index(tab['name'])))
    groups = [g for g in document.get('groups') or [] if isinstance(g, dict)]
    return {'tabs': shown, 'groups': groups}


def settings_values(tab, values):
    """Validate a tab update. An empty password keeps the stored one (Shelfmark's rule)."""
    if tab not in SETTINGS_TABS:
        raise NotFound('Settings tab is unavailable')
    if not isinstance(values, dict) or len(values) > 200 or any(
            not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,128}', k) for k in values):
        raise ShelfmarkError('invalid_request')
    if len(json.dumps(values)) > 512 * 1024:
        raise ShelfmarkError('invalid_request')
    return values
