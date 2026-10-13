# SPDX-License-Identifier: GPL-3.0-or-later
"""Shelfmark sidecar: catalog mapping, the HTTP client and the worker hand-off.

Shelfmark itself is replaced by an in-memory double that keeps its real contract:
tasks are named by the release's source id, the queue refuses duplicates, and a
completed task's file is served once until it is discarded.
"""
import base64
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest
from sqlalchemy import MetaData, create_engine

path = Path(__file__).resolve().parents[2] / 'cps/services/acquisition'
spec = importlib.util.spec_from_file_location('_acquisition_shelfmark_tests', path / '__init__.py', submodule_search_locations=[str(path)])
package = importlib.util.module_from_spec(spec); sys.modules[spec.name] = package; spec.loader.exec_module(package)
sm = importlib.import_module(spec.name + '.shelfmark')
w = importlib.import_module(spec.name + '.worker')
s = importlib.import_module(spec.name + '.storage')
k = importlib.import_module(spec.name + '.secrets')

PDF = b'%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n'
CONFIG = {'endpoint': 'http://shelfmark:8084', 'secret': 'KEY-123', 'allow_mobi': False}


def epub_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('OPS/book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest/><spine/></package>')
    return buffer.getvalue()


BOOK = {'provider': 'openlibrary', 'provider_id': 'OL1W', 'title': 'Dune', 'authors': ['Frank Herbert'],
        'publish_year': 1965, 'isbn_13': '9780441013593', 'language': 'en', 'description': 'Spice.',
        'cover_url': '/api/covers/openlibrary_OL1W?url=' + base64.urlsafe_b64encode(b'https://covers.example/1.jpg').decode().rstrip('='),
        'provider_display_name': 'Open Library', 'series_name': 'Dune', 'series_position': 1}


def release(source_id, fmt='epub', **extra):
    return dict({'source': 'direct_download', 'source_id': source_id, 'title': 'Dune (release)', 'format': fmt,
                 'size': '1.2 MB', 'language': 'en', 'indexer': None, 'extra': {'md5': source_id}}, **extra)


class FakeShelfmark:
    """In-memory Shelfmark with its real queue contract."""

    def __init__(self):
        self.books = [BOOK]
        self.releases = [release('aaa'), release('bbb', 'pdf'), release('ccc', 'azw3'), release('ddd', None),
                         release('eee', 'mobi')]
        self.tasks = {}
        self.files = {}
        self.calls = []
        self.has_more = False

    def client(self, config):
        assert config['secret'] == 'KEY-123'
        return FakeClient(self)


class FakeClient:
    def __init__(self, fake):
        self.fake = fake

    def config_info(self):
        return {'ok': True}

    def health(self):
        return {'status': 'ok'}

    def search(self, query, *, page=1, limit=20):
        self.fake.calls.append(('search', query, page))
        return {'books': self.fake.books, 'page': page, 'has_more': self.fake.has_more}

    def releases(self, provider, book_id, *, title=None, author=None):
        self.fake.calls.append(('releases', provider, book_id, title, author))
        return {'releases': self.fake.releases, 'errors': []}

    def task(self, task_id):
        found = self.fake.tasks.get(task_id)
        return (found['status'], found) if found else None

    def queue(self, payload):
        self.fake.calls.append(('queue', payload['source_id']))
        if payload['source_id'] in self.fake.tasks:
            return {'status': 'queued'}  # the client treats "already queued" as success
        self.fake.tasks[payload['source_id']] = {'status': 'queued', 'added_time': 1000.0}
        return {'status': 'queued'}

    def download(self, task_id, destination, *, max_bytes, checkpoint=lambda: None):
        self.fake.calls.append(('download', task_id))
        data = self.fake.files.get(task_id)
        if data is None:
            raise sm.ShelfmarkError('artifact_unavailable')
        if len(data) > max_bytes:
            raise sm.ShelfmarkError('file_too_large')
        checkpoint()
        destination.write_bytes(data)
        return w.digest(destination)

    def discard(self, task_id):
        self.fake.calls.append(('discard', task_id))
        self.fake.files.pop(task_id, None)

    def cancel(self, task_id):
        self.fake.calls.append(('cancel', task_id))


@pytest.fixture
def repo(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'app.db'))
    metadata = MetaData(); tables = s.define_tables(metadata); metadata.create_all(engine)
    now = [1000.0]
    repository = s.Repository(engine, tables, k.SecretBox(b'x' * 32), clock=lambda: now[0])
    connection = repository.create_connection('Shelfmark', 'shelfmark', dict(CONFIG), enabled=True)
    yield repository, connection, now
    engine.dispose()


def browse_releases(repository, connection, fake):
    service = sm.ShelfmarkService(repository, client_factory=fake.client)
    books = service.browse(1, connection.id, query='dune')
    selection = books['publications'][0]['navigation'][0]['selection']
    return {p['identity']: p for p in service.browse(1, connection.id, selection=selection)['publications']}, books


# -- configuration -------------------------------------------------------------

def test_connection_config_normalizes_and_rejects_unsafe_values():
    assert sm.connection_config({'endpoint': 'http://shelfmark:8084/', 'secret': 'k'}) == {
        'endpoint': 'http://shelfmark:8084', 'secret': 'k', 'allow_mobi': False}
    for bad in ({'endpoint': 'ftp://x', 'secret': 'k'}, {'endpoint': 'http://u:p@x', 'secret': 'k'},
                {'endpoint': 'http://x?k=1', 'secret': 'k'}, {'endpoint': 'http://x', 'secret': ''},
                {'endpoint': 'http://x', 'secret': 'a b'}, {'endpoint': 'http://x', 'secret': 'k', 'extra': 1},
                {'endpoint': 'http://x', 'secret': 'k', 'allow_mobi': 'yes'}, 'nope'):
        with pytest.raises(sm.ShelfmarkError):
            sm.connection_config(bad)


def test_environment_config_needs_both_values():
    assert sm.environment_config({}) is None
    assert sm.environment_config({'SHELFMARK_URL': 'http://shelfmark:8084'}) is None
    assert sm.environment_config({'SHELFMARK_URL': 'http://shelfmark:8084', 'SHELFMARK_API_KEY': ' k '})['secret'] == 'k'


def test_cover_url_unwraps_the_private_cover_proxy():
    assert sm.cover_url(BOOK['cover_url']) == 'https://covers.example/1.jpg'
    assert sm.cover_url('https://covers.example/2.jpg') == 'https://covers.example/2.jpg'
    assert sm.cover_url('/api/covers/x?url=!!!') is None
    assert sm.cover_url('/api/covers/x?url=' + base64.urlsafe_b64encode(b'javascript:alert(1)').decode()) is None
    assert sm.cover_url(None) is None


# -- catalog mapping -----------------------------------------------------------

def test_root_advertises_search_and_search_maps_books(repo):
    repository, connection, _ = repo
    fake = FakeShelfmark()
    service = sm.ShelfmarkService(repository, client_factory=fake.client)
    root = service.browse(1, connection.id)
    assert root['publications'] == [] and len(root['searches']) == 1 and fake.calls == []
    result = service.browse(1, connection.id, selection=root['searches'][0]['selection'], query=' dune ')
    assert fake.calls == [('search', 'dune', 1)]
    book = result['publications'][0]
    assert book['title'] == 'Dune' and book['authors'] == ['Frank Herbert'] and book['year'] == 1965
    assert book['cover_url'] == 'https://covers.example/1.jpg' and book['isbn'] == ['9780441013593']
    assert book['offers'] == [] and book['navigation'][0]['title'] == 'Find downloads'
    assert book['series'] == {'name': 'Dune', 'position': 1}
    # The opaque selection is all the browser gets: no provider id leaks.
    assert 'OL1W' not in json.dumps(result)


def test_search_paginates_with_an_opaque_next_selection(repo):
    repository, connection, _ = repo
    fake = FakeShelfmark(); fake.has_more = True
    service = sm.ShelfmarkService(repository, client_factory=fake.client)
    first = service.browse(1, connection.id, query='dune')
    service.browse(1, connection.id, selection=first['pagination'][0]['selection'])
    assert fake.calls == [('search', 'dune', 1), ('search', 'dune', 2)]


def test_selections_are_owner_bound(repo):
    repository, connection, _ = repo
    fake = FakeShelfmark()
    service = sm.ShelfmarkService(repository, client_factory=fake.client)
    selection = service.browse(1, connection.id, query='dune')['publications'][0]['navigation'][0]['selection']
    with pytest.raises(s.NotFound):
        service.browse(2, connection.id, selection=selection)


@pytest.mark.parametrize('query', ['', '   ', 'x' * 501])
def test_invalid_queries_never_reach_shelfmark(repo, query):
    repository, connection, _ = repo
    fake = FakeShelfmark()
    with pytest.raises(sm.ShelfmarkError):
        sm.ShelfmarkService(repository, client_factory=fake.client).browse(1, connection.id, query=query)
    assert fake.calls == []


def test_releases_become_offers_only_for_importable_formats(repo):
    repository, connection, _ = repo
    fake = FakeShelfmark()
    releases, _ = browse_releases(repository, connection, fake)
    assert fake.calls[-1] == ('releases', 'openlibrary', 'OL1W', 'Dune', 'Frank Herbert')
    by_format = {p['format']: p for p in releases.values()}
    assert by_format['epub']['offers'][0]['format'] == 'EPUB'
    assert by_format['epub']['offers'][0]['label'] == '1.2 MB · direct_download'
    assert by_format['pdf']['offers'][0]['format'] == 'PDF'
    assert by_format[None]['offers'][0]['format'] == 'Unknown'
    assert by_format['azw3']['offers'] == [] and by_format['azw3']['unavailable_reason'] == 'unsupported_format'
    # MOBI needs the connection's explicit opt-in.
    assert by_format['mobi']['offers'] == []
    offer_id = by_format['epub']['offers'][0]['offer_id']
    payload = repository.offer_payload(1, offer_id, connection.id).offer
    assert payload['transport'] == 'shelfmark' and payload['task_id'] == 'aaa'
    assert payload['media_type'] == 'application/epub+zip'
    assert payload['release']['source_id'] == 'aaa' and payload['release']['author'] == 'Frank Herbert'
    assert payload['release']['content_type'] == 'ebook' and payload['title'] == 'Dune'


def test_mobi_releases_are_offered_once_the_connection_allows_them(repo):
    repository, connection, _ = repo
    repository.set_connection_enabled(connection.id, False)
    repository.update_connection(connection.id, label='Shelfmark', config=dict(CONFIG, allow_mobi=True))
    repository.set_connection_enabled(connection.id, True)
    releases, _ = browse_releases(repository, connection, FakeShelfmark())
    mobi = next(p for p in releases.values() if p['format'] == 'mobi')
    assert mobi['offers'][0]['format'] == 'MOBI'


def test_only_audiobook_releases_are_left_out(repo):
    repository, connection, _ = repo
    fake = FakeShelfmark(); fake.releases = [release('aaa'), release('zzz', 'm4b', content_type='audiobook'),
        # Anna's Archive reports its own category here, not "ebook".
        release('fff', 'pdf', content_type='fiction', language='english [en]')]
    releases, _ = browse_releases(repository, connection, fake)
    assert [p['format'] for p in releases.values()] == ['epub', 'pdf']
    assert [p['languages'] for p in releases.values()] == [['en'], ['en']]


# -- settings proxy ------------------------------------------------------------

def test_visible_settings_hide_account_tabs_and_stored_passwords():
    document = {'groups': [], 'tabs': [
        {'name': 'security', 'fields': [{'key': 'AUTH_METHOD', 'type': 'SelectField', 'value': 'builtin'}]},
        {'name': 'users', 'fields': []},
        {'name': 'prowlarr_config', 'fields': [
            {'key': 'PROWLARR_URL', 'type': 'TextField', 'value': 'http://prowlarr'},
            {'key': 'PROWLARR_API_KEY', 'type': 'PasswordField', 'value': 'SECRET'},
            {'key': 'x', 'type': 'CustomComponentField', 'component': 'users_management'}]},
        {'name': 'download_sources', 'fields': []}]}
    visible = sm.visible_settings(document)
    assert [t['name'] for t in visible['tabs']] == ['download_sources', 'prowlarr_config']
    fields = visible['tabs'][1]['fields']
    assert [f['key'] for f in fields] == ['PROWLARR_URL', 'PROWLARR_API_KEY']
    assert fields[1]['value'] == '' and fields[1]['hasValue'] is True
    assert 'SECRET' not in json.dumps(visible)


def test_settings_updates_are_limited_to_shown_tabs():
    assert sm.settings_values('prowlarr_config', {'PROWLARR_URL': 'x'}) == {'PROWLARR_URL': 'x'}
    with pytest.raises(s.NotFound):
        sm.settings_values('security', {'AUTH_METHOD': 'none'})
    for bad in (None, [], {'bad key': 1}, {'k' * 200: 1}):
        with pytest.raises(sm.ShelfmarkError):
            sm.settings_values('network', bad)


# -- HTTP client ---------------------------------------------------------------

class Response:
    def __init__(self, status=200, body=None, *, content=None, headers=None):
        self.status_code = status
        self.content = content if content is not None else json.dumps(body if body is not None else {}).encode()
        self.headers = headers or {}

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), 4):
            yield self.content[start:start + 4]

    def close(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self):
        return self

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


def test_client_sends_the_key_and_never_follows_redirects():
    session = Session([Response(body={'books': []})])
    sm.ShelfmarkClient(CONFIG, session_factory=session).search('dune')
    method, url, kwargs = session.requests[0]
    assert (method, url) == ('GET', 'http://shelfmark:8084/api/metadata/search')
    assert kwargs['headers']['Authorization'] == 'Bearer KEY-123'
    assert kwargs['allow_redirects'] is False and kwargs['params']['content_type'] == 'ebook'


@pytest.mark.parametrize('status,code', [(401, 'needs_auth'), (403, 'needs_auth'), (429, 'source_busy'),
                                         (503, 'shelfmark_unavailable'), (500, 'shelfmark_error')])
def test_client_maps_http_failures(status, code):
    session = Session([Response(status, {'error': 'No metadata provider configured'})])
    with pytest.raises(sm.ShelfmarkError) as raised:
        sm.ShelfmarkClient(CONFIG, session_factory=session).search('dune')
    assert raised.value.code == code
    if status >= 500:
        assert raised.value.detail == 'No metadata provider configured'


def test_client_reports_an_unreachable_service():
    import requests

    class Down(Session):
        def request(self, *args, **kwargs):
            raise requests.ConnectionError('refused')
    with pytest.raises(sm.ShelfmarkError) as raised:
        sm.ShelfmarkClient(CONFIG, session_factory=Down([])).health()
    assert raised.value.code == 'shelfmark_unreachable'


def test_queue_treats_an_existing_entry_as_submitted():
    session = Session([Response(500, {'error': 'Release is already in the download queue'})])
    assert sm.ShelfmarkClient(CONFIG, session_factory=session).queue({'source': 'x', 'source_id': 'y'}) == {'status': 'queued'}
    session = Session([Response(500, {'error': 'Source is unavailable'})])
    with pytest.raises(sm.ShelfmarkError):
        sm.ShelfmarkClient(CONFIG, session_factory=session).queue({'source': 'x', 'source_id': 'y'})


def test_task_lookup_reads_the_status_buckets():
    status = {'queued': {}, 'complete': {'aaa': {'id': 'aaa', 'added_time': 5}}}
    client = sm.ShelfmarkClient(CONFIG, session_factory=Session([Response(body=status), Response(body=status)]))
    assert client.task('aaa') == ('complete', {'id': 'aaa', 'added_time': 5})
    assert client.task('zzz') is None


def test_download_streams_with_a_hard_size_limit(tmp_path):
    good = Session([Response(content=PDF)])
    target = tmp_path / 'book.part'
    digest = sm.ShelfmarkClient(CONFIG, session_factory=good).download('aaa', target, max_bytes=1024)
    assert target.read_bytes() == PDF and digest == w.digest(target)
    assert good.requests[0][2]['params'] == {'id': 'aaa'} and good.requests[0][2]['stream'] is True
    for response, code in ((Response(content=PDF), 'file_too_large'), (Response(404), 'artifact_unavailable'),
                           (Response(content=b''), 'empty_response'),
                           (Response(content=PDF, headers={'Content-Length': '999999'}), 'file_too_large')):
        target = tmp_path / ('fail-' + code + str(len(response.content)))
        with pytest.raises(sm.ShelfmarkError) as raised:
            sm.ShelfmarkClient(CONFIG, session_factory=Session([response])).download('aaa', target, max_bytes=10 if code == 'file_too_large' and not response.headers else 1024 if response.headers else 100)
        assert raised.value.code == code and not target.exists()


# -- worker hand-off -------------------------------------------------------------

@pytest.fixture
def flow(repo, tmp_path):
    repository, connection, now = repo
    fake = FakeShelfmark()
    releases, _ = browse_releases(repository, connection, fake)
    offers = {p['format']: p['offers'][0]['offer_id'] for p in releases.values() if p['offers']}
    ingest = tmp_path / 'ingest'; ingest.mkdir()
    worker = w.AcquisitionWorker(repository, tmp_path / 'staging', ingest, allowed=lambda owner: True,
        transfer=lambda *a, **kw: pytest.fail('Shelfmark jobs never use the catalog transport'),
        shelfmark_factory=fake.client, clock=lambda: now[0])
    yield repository, worker, fake, offers, ingest, now


def run(repository, worker, now):
    now[0] += 61  # past any release delay
    return worker.run_once()


def test_release_is_queued_polled_fetched_and_published(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers['epub'], 'click', requires_approval=False)
    assert run(repository, worker, now).state == 'downloading'
    assert ('queue', 'aaa') in fake.calls and fake.tasks['aaa']['status'] == 'queued'
    fake.tasks['aaa']['status'] = 'downloading'
    assert run(repository, worker, now).state == 'downloading'
    assert not list(ingest.iterdir())
    fake.tasks['aaa']['status'] = 'complete'; fake.files['aaa'] = epub_bytes()
    result = run(repository, worker, now)
    assert result.state == 'importing'
    published = list(ingest.glob('*.epub'))
    assert len(published) == 1 and published[0].read_bytes() == epub_bytes()
    manifest = json.loads(Path(str(published[0]) + '.cwa.json').read_text())
    assert manifest['job_id'] == job.id
    # Shelfmark's copy goes only after the staged copy is durable.
    assert fake.calls[-1] == ('discard', 'aaa') and 'aaa' not in fake.files
    assert [call for call in fake.calls if call[0] == 'queue'] == [('queue', 'aaa')]


def test_unknown_format_is_decided_by_the_bytes(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[None] if None in offers else offers['Unknown'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['ddd']['status'] = 'complete'; fake.files['ddd'] = PDF
    assert run(repository, worker, now).state == 'importing'
    assert [p.suffix for p in ingest.iterdir() if not p.name.endswith('.json')] == ['.pdf']


def test_a_non_book_file_fails_instead_of_importing(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers['pdf'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['bbb']['status'] = 'complete'; fake.files['bbb'] = b'<html>login required</html>'
    result = run(repository, worker, now)
    assert result.state == 'failed' and result.error_code == 'no_usable_book'
    assert not list(ingest.iterdir())


def test_disallowed_format_is_refused_after_download(flow):
    repository, worker, fake, offers, ingest, now = flow
    worker.media_allowed = lambda media: media != 'application/pdf'
    repository.create_job(1, offers[None] if None in offers else offers['Unknown'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['ddd']['status'] = 'complete'; fake.files['ddd'] = PDF
    result = run(repository, worker, now)
    assert result.state == 'failed' and result.error_code == 'format_not_allowed'
    assert not list(ingest.iterdir())


@pytest.mark.parametrize('status', ['error', 'cancelled'])
def test_shelfmark_failure_fails_the_job(flow, status):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers['epub'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['aaa']['status'] = status
    result = run(repository, worker, now)
    assert result.state == 'failed' and result.error_code == 'client_job_failed'


def test_a_download_that_never_finishes_is_given_up(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers['epub'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['aaa']['status'] = 'downloading'
    now[0] += worker.download_deadline_seconds
    result = run(repository, worker, now)
    assert result.state == 'failed' and result.error_code == 'client_job_stalled'


def test_cancel_seen_by_the_worker_also_stops_the_shelfmark_download(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers['epub'], 'click', requires_approval=False)
    queue = FakeClient.queue
    def queue_then_cancel(self, payload):
        # The user cancels while the worker holds the job: only the worker can act.
        repository.request_cancel(1, job.id)
        return queue(self, payload)
    FakeClient.queue = queue_then_cancel
    try:
        run(repository, worker, now)
    finally:
        FakeClient.queue = queue
    assert repository.get_job(1, job.id).cancel_requested
    run(repository, worker, now)
    assert repository.get_job(1, job.id).state == 'cancelled'
    assert ('cancel', 'aaa') in fake.calls


def test_job_offer_reads_the_durable_offer_for_its_owner_only(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers['epub'], 'click', requires_approval=False)
    now[0] += 86400  # long past the selection's own expiry
    assert repository.job_offer(1, job.id)['task_id'] == 'aaa'
    with pytest.raises(s.NotFound):
        repository.job_offer(2, job.id)


def test_completed_file_missing_fails_cleanly(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers['epub'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['aaa']['status'] = 'complete'  # but no file
    result = run(repository, worker, now)
    assert result.state == 'failed' and result.error_code == 'artifact_unavailable'


def test_restart_after_staging_never_downloads_again(flow, tmp_path):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers['pdf'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['bbb']['status'] = 'complete'; fake.files['bbb'] = PDF
    assert run(repository, worker, now).state == 'importing'
    downloads = [c for c in fake.calls if c[0] == 'download']
    assert run(repository, worker, now).state == 'importing'
    assert [c for c in fake.calls if c[0] == 'download'] == downloads


def test_retry_after_failure_queues_the_release_again(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers['epub'], 'click', requires_approval=False)
    run(repository, worker, now)
    fake.tasks['aaa']['status'] = 'error'
    assert run(repository, worker, now).state == 'failed'
    del fake.tasks['aaa']  # Shelfmark lets an errored task be queued again
    repository.retry(1, job.id)
    assert run(repository, worker, now).state == 'downloading'
    assert [c for c in fake.calls if c[0] == 'queue'] == [('queue', 'aaa'), ('queue', 'aaa')]
