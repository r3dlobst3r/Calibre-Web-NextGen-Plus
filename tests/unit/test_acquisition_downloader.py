# SPDX-License-Identifier: GPL-3.0-or-later
"""The built-in downloader as a book source: catalog mapping and the worker hand-off.

The download engine is replaced by an in-memory double that keeps its contract:
downloads are named by the release's source id, the queue refuses a release it
already holds, and a finished download's file is handed over once and discarded.
"""
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
spec = importlib.util.spec_from_file_location('_acquisition_downloader_tests', path / '__init__.py', submodule_search_locations=[str(path)])
package = importlib.util.module_from_spec(spec); sys.modules[spec.name] = package; spec.loader.exec_module(package)
dl = importlib.import_module(spec.name + '.downloader')
w = importlib.import_module(spec.name + '.worker')
s = importlib.import_module(spec.name + '.storage')
k = importlib.import_module(spec.name + '.secrets')
from cps.services.acquisition import admission as a  # noqa: E402  (needs cps.constants)
st = importlib.import_module(spec.name + '.staging')

PDF = b'%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n'
M4B = b'\x00\x00\x00\x20ftypM4B \x00\x00\x02\x00M4B isommp42' + b'\x00' * 64
CONFIG = {'allow_mobi': False, 'metadata_provider': 'openlibrary'}


def epub_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('OPS/book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest/><spine/></package>')
    return buffer.getvalue()


RECORD = {'id': 'OL1W', 'title': 'Dune', 'authors': ['Frank Herbert'], 'url': 'https://openlibrary.org/works/OL1W',
          'source': {'id': 'openlibrary', 'description': 'Open Library', 'link': 'https://openlibrary.org'},
          'cover': 'https://covers.example/1.jpg', 'description': '<p>Spice.</p>', 'series': 'Dune',
          'series_index': 1, 'identifiers': {'isbn': '978-0441013593'}, 'publishedDate': '1965-08-01',
          'languages': ['en']}


def release(source_id, fmt='epub', **extra):
    return dict({'source': 'direct_download', 'source_id': source_id, 'title': 'Dune (release)', 'format': fmt,
                 'size': '1.2 MB', 'language': 'english [en]', 'indexer': None, 'extra': {'md5': source_id}}, **extra)


class FakeEngine:
    """In-memory download engine with the real queue contract."""

    def __init__(self):
        self.releases_by_type = {
            'ebook': [release('aaa'), release('bbb', 'pdf'), release('ccc', 'azw3'), release('ddd', None),
                      release('eee', 'mobi'), release('fff', 'm4b')],
            'audiobook': [release('abb1', 'm4b', source='audiobookbay', protocol='torrent'),
                          release('abb2', 'mp3', source='audiobookbay'), release('abb3', None, source='audiobookbay')],
        }
        self.enabled = {'ebook': ['direct_download', 'libgen'], 'audiobook': ['audiobookbay']}
        self.tasks, self.files, self.calls = {}, {}, []

    def factory(self, config):
        return FakeClient(self)


class FakeClient:
    def __init__(self, fake):
        self.fake = fake

    def sources(self, content_type='ebook'):
        return list(self.fake.enabled.get(content_type, []))

    def releases(self, book, *, content_type='ebook'):
        self.fake.calls.append(('releases', book.get('title'), content_type))
        return list(self.fake.releases_by_type[content_type]), ['irc: unavailable'], self.sources(content_type)

    def task(self, task_id):
        found = self.fake.tasks.get(task_id)
        return (found['status'], found) if found else None

    def queue(self, payload):
        self.fake.calls.append(('queue', payload['source_id']))
        # Like the engine's queue: a release it holds is refused unless it failed.
        current = self.fake.tasks.get(payload['source_id'])
        if current is None or current['status'] in ('error', 'cancelled'):
            self.fake.tasks[payload['source_id']] = {'status': 'queued', 'added_time': 1000.0}
        return True

    def download(self, task_id, destination, *, max_bytes, checkpoint=lambda: None):
        self.fake.calls.append(('download', task_id))
        data = self.fake.files.get(task_id)
        if data is None:
            raise dl.DownloaderError('artifact_unavailable')
        if len(data) > max_bytes:
            raise dl.DownloaderError('file_too_large')
        checkpoint()
        destination.write_bytes(data)
        return w.digest(destination)

    def discard(self, task_id):
        self.fake.calls.append(('discard', task_id))
        self.fake.files.pop(task_id, None)
        self.fake.tasks.pop(task_id, None)

    def cancel(self, task_id):
        self.fake.calls.append(('cancel', task_id))


@pytest.fixture
def repo(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'app.db'))
    metadata = MetaData(); tables = s.define_tables(metadata); metadata.create_all(engine)
    now = [1000.0]
    repository = s.Repository(engine, tables, k.SecretBox(b'x' * 32), clock=lambda: now[0])
    connection = repository.create_connection('Downloads', 'downloader', dict(CONFIG), enabled=True)
    yield repository, connection, now
    engine.dispose()


def service(repository, fake, records=(RECORD,), searches=None):
    def search(query, provider):
        if searches is not None:
            searches.append((query, provider))
        return [dict(r) for r in records]
    return dl.DownloaderService(repository, book_search=search, engine_factory=fake.factory)


def browse_releases(repository, connection, fake, content_type='ebook'):
    svc = service(repository, fake)
    books = svc.browse(1, connection.id, query='dune')
    nav = next(n for n in books['publications'][0]['navigation'] if n['relations'] == ['downloader:' + content_type])
    return {p['identity']: p for p in svc.browse(1, connection.id, selection=nav['selection'])['publications']}, books


# -- configuration ---------------------------------------------------------------

def test_connection_config_has_only_the_downloader_options():
    assert dl.connection_config({}) == {'allow_mobi': False, 'metadata_provider': 'openlibrary'}
    assert dl.connection_config({'allow_mobi': True, 'metadata_provider': 'hardcover'})['metadata_provider'] == 'hardcover'
    for bad in ({'allow_mobi': 'yes'}, {'metadata_provider': 'amazon'}, {'endpoint': 'x'}, 'nope'):
        with pytest.raises(dl.DownloaderError):
            dl.connection_config(bad)


def test_language_codes_and_media_types():
    assert dl._language('english [en]') == 'en' and dl._language('de') == 'de' and dl._language(None) is None
    assert dl.media_type({'format': 'EPUB'}, 'ebook') == 'application/epub+zip'
    assert dl.media_type({'format': None}, 'audiobook') == dl.UNKNOWN_MEDIA
    # An ebook search never offers an audio file, and an audiobook search no ebook.
    assert dl.media_type({'format': 'm4b'}, 'ebook') is None
    assert dl.media_type({'format': 'epub'}, 'audiobook') is None
    assert dl.media_type({'format': 'm4b'}, 'audiobook') == 'audio/x-m4b'
    assert dl.media_type({'format': 'mp3'}, 'audiobook') is None


# -- catalog mapping -------------------------------------------------------------

def test_search_uses_the_configured_metadata_provider_and_maps_books(repo):
    repository, connection, _ = repo
    fake, searches = FakeEngine(), []
    svc = service(repository, fake, searches=searches)
    root = svc.browse(1, connection.id)
    assert root['publications'] == [] and len(root['searches']) == 1 and searches == []
    result = svc.browse(1, connection.id, selection=root['searches'][0]['selection'], query=' dune ')
    assert searches == [('dune', 'openlibrary')]
    book = result['publications'][0]
    assert book['title'] == 'Dune' and book['authors'] == ['Frank Herbert'] and book['year'] == 1965
    assert book['cover_url'] == 'https://covers.example/1.jpg' and book['isbn'] == ['9780441013593']
    assert book['description'] == 'Spice.' and book['series'] == {'name': 'Dune', 'position': 1}
    assert [n['relations'] for n in book['navigation']] == [['downloader:ebook'], ['downloader:audiobook']]
    assert result['content_types'] == ['ebook', 'audiobook']
    assert 'OL1W' not in json.dumps(result)


def test_audiobook_search_is_offered_only_with_an_audiobook_source(repo):
    repository, connection, _ = repo
    fake = FakeEngine(); fake.enabled['audiobook'] = []
    result = service(repository, fake).browse(1, connection.id, query='dune')
    assert [n['relations'] for n in result['publications'][0]['navigation']] == [['downloader:ebook']]


def test_selections_are_owner_bound(repo):
    repository, connection, _ = repo
    fake = FakeEngine()
    svc = service(repository, fake)
    selection = svc.browse(1, connection.id, query='dune')['publications'][0]['navigation'][0]['selection']
    with pytest.raises(s.NotFound):
        svc.browse(2, connection.id, selection=selection)


@pytest.mark.parametrize('query', ['', '   ', 'x' * 501])
def test_invalid_queries_never_reach_a_provider(repo, query):
    repository, connection, _ = repo
    searches = []
    with pytest.raises(dl.DownloaderError):
        service(repository, FakeEngine(), searches=searches).browse(1, connection.id, query=query)
    assert searches == []


def test_ebook_releases_become_offers_only_for_importable_formats(repo):
    repository, connection, _ = repo
    fake = FakeEngine()
    releases, _ = browse_releases(repository, connection, fake)
    assert ('releases', 'Dune', 'ebook') in fake.calls
    by_format = {p['format']: p for p in releases.values()}
    assert by_format['epub']['offers'][0]['format'] == 'EPUB'
    assert by_format['epub']['offers'][0]['label'] == '1.2 MB · direct_download'
    assert by_format['epub']['languages'] == ['en']
    assert by_format['pdf']['offers'][0]['format'] == 'PDF'
    assert by_format[None]['offers'][0]['format'] == 'Unknown'
    for fmt in ('azw3', 'mobi', 'm4b'):
        assert by_format[fmt]['offers'] == [] and by_format[fmt]['unavailable_reason'] == 'unsupported_format'
    payload = repository.offer_payload(1, by_format['epub']['offers'][0]['offer_id'], connection.id).offer
    assert payload['transport'] == 'downloader' and payload['task_id'] == 'aaa' and payload['content_type'] == 'ebook'
    assert payload['release']['source_id'] == 'aaa' and payload['release']['author'] == 'Frank Herbert'


def test_audiobook_releases_offer_m4b_and_unknown_formats(repo):
    repository, connection, _ = repo
    releases, _ = browse_releases(repository, connection, FakeEngine(), 'audiobook')
    by_format = {p['format']: p for p in releases.values()}
    assert by_format['m4b']['offers'][0]['format'] == 'M4B'
    assert by_format[None]['offers'][0]['format'] == 'Unknown'
    assert by_format['mp3']['offers'] == []


def test_mobi_releases_are_offered_once_allowed(repo):
    repository, connection, _ = repo
    repository.set_connection_enabled(connection.id, False)
    repository.update_connection(connection.id, label='Downloads', config=dict(CONFIG, allow_mobi=True))
    repository.set_connection_enabled(connection.id, True)
    releases, _ = browse_releases(repository, connection, FakeEngine())
    assert next(p for p in releases.values() if p['format'] == 'mobi')['offers'][0]['format'] == 'MOBI'


# -- admission ---------------------------------------------------------------------

def offer(**overrides):
    return dict({'kind': 'acquisition', 'transport': 'downloader', 'media_type': 'application/epub+zip',
                 'content_type': 'ebook', 'task_id': 't', 'release': {'source_id': 't'}}, **overrides)


def test_admission_checks_format_against_content_type_and_policy():
    allowed = {'application/epub+zip', 'audio/x-m4b'}
    a.acquisition_offer(offer(), allowed)
    a.acquisition_offer(offer(media_type='audio/x-m4b', content_type='audiobook'), allowed)
    a.acquisition_offer(offer(media_type='application/octet-stream', content_type='audiobook'), allowed)
    for bad in (offer(media_type='audio/x-m4b'), offer(content_type='audiobook'), offer(task_id='other'),
                offer(content_type='video'), offer(media_type='application/x-mobipocket-ebook')):
        with pytest.raises(a.AdmissionError):
            a.acquisition_offer(bad, allowed)
    with pytest.raises(a.AdmissionError):
        a.acquisition_offer(offer(media_type='application/octet-stream', content_type='audiobook'), {'application/epub+zip'})


def test_audiobook_validation_reads_the_mpeg4_header(tmp_path):
    book = tmp_path / 'book.part'
    book.write_bytes(M4B)
    assert st.validate_book(book, 'audio/x-m4b') == 'm4b'
    book.write_bytes(b'<html>not audio</html>' * 4)
    with pytest.raises(st.StagingError):
        st.validate_book(book, 'audio/x-m4b')


# -- settings policy ---------------------------------------------------------------

def test_visible_settings_follow_the_field_policy_and_hide_secrets():
    document = {'groups': [], 'tabs': [
        {'name': 'security', 'fields': [{'key': 'AUTH_METHOD', 'type': 'SelectField', 'value': 'builtin'}]},
        {'name': 'downloads', 'fields': [{'key': 'BOOKS_OUTPUT_MODE', 'type': 'SelectField', 'value': 'folder'},
                                         {'key': 'MAX_CONCURRENT_DOWNLOADS', 'type': 'NumberField', 'value': 3}]},
        {'name': 'download_sources', 'fields': [
            {'key': 'DIRECT_DOWNLOAD_ENABLED', 'type': 'CheckboxField', 'value': True},
            {'key': 'AA_DONATOR_KEY', 'type': 'PasswordField', 'value': 'SECRET'},
            {'key': 'AA_CONTENT_TYPE_ROUTING', 'type': 'CheckboxField', 'value': False}]},
        {'name': 'prowlarr_clients', 'fields': [{'key': 'REALDEBRID_API_KEY', 'type': 'PasswordField', 'value': 'RD'}]}]}
    visible = dl.visible_settings(document)
    assert [t['name'] for t in visible['tabs']] == ['download_sources', 'prowlarr_clients', 'downloads']
    assert [f['key'] for f in visible['tabs'][2]['fields']] == ['MAX_CONCURRENT_DOWNLOADS']
    sources = visible['tabs'][0]['fields']
    assert [f['key'] for f in sources] == ['DIRECT_DOWNLOAD_ENABLED', 'AA_DONATOR_KEY']
    assert sources[1]['value'] == '' and sources[1]['hasValue'] is True
    assert 'SECRET' not in json.dumps(visible) and 'RD"' not in json.dumps(visible)


def test_settings_updates_are_limited_to_shown_fields():
    assert dl.settings_values('downloads', {'MAX_CONCURRENT_DOWNLOADS': 2}) == {'MAX_CONCURRENT_DOWNLOADS': 2}
    with pytest.raises(s.NotFound):
        dl.settings_values('security', {'AUTH_METHOD': 'none'})
    for tab, bad in (('downloads', {'BOOKS_OUTPUT_MODE': 'email'}), ('download_sources', {'AA_CONTENT_TYPE_ROUTING': True}),
                     ('network', None), ('network', {'bad key': 1})):
        with pytest.raises(dl.DownloaderError):
            dl.settings_values(tab, bad)


# -- worker hand-off ---------------------------------------------------------------

@pytest.fixture
def flow(repo, tmp_path):
    repository, connection, now = repo
    fake = FakeEngine()
    ebooks, _ = browse_releases(repository, connection, fake)
    audio, _ = browse_releases(repository, connection, fake, 'audiobook')
    offers = {('ebook', p['format']): p['offers'][0]['offer_id'] for p in ebooks.values() if p['offers']}
    offers.update({('audiobook', p['format']): p['offers'][0]['offer_id'] for p in audio.values() if p['offers']})
    ingest = tmp_path / 'ingest'; ingest.mkdir()
    worker = w.AcquisitionWorker(repository, tmp_path / 'staging', ingest, allowed=lambda owner: True,
        transfer=lambda *args, **kw: pytest.fail('downloader jobs never use the catalog transport'),
        engine_factory=fake.factory, clock=lambda: now[0])
    yield repository, worker, fake, offers, ingest, now


def run(worker, now):
    now[0] += 61  # past any release delay
    return worker.run_once()


def published(ingest):
    return sorted(p.name.rsplit('.', 1)[1] for p in ingest.iterdir() if not p.name.endswith('.json'))


def test_release_is_queued_polled_copied_and_published(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers[('ebook', 'epub')], 'click', requires_approval=False)
    assert run(worker, now).state == 'downloading'
    assert ('queue', 'aaa') in fake.calls
    fake.tasks['aaa']['status'] = 'downloading'
    assert run(worker, now).state == 'downloading' and not list(ingest.iterdir())
    fake.tasks['aaa']['status'] = 'complete'; fake.files['aaa'] = epub_bytes()
    assert run(worker, now).state == 'importing'
    assert published(ingest) == ['epub']
    manifest = json.loads(next(ingest.glob('*.cwa.json')).read_text())
    assert manifest['job_id'] == job.id
    assert fake.calls[-1] == ('discard', 'aaa')
    assert [c for c in fake.calls if c[0] == 'queue'] == [('queue', 'aaa')]


def test_audiobook_is_imported_as_m4b_with_the_large_limit(flow):
    repository, worker, fake, offers, ingest, now = flow
    worker.max_bytes = 10  # the ebook limit must not apply to audiobooks
    repository.create_job(1, offers[('audiobook', 'm4b')], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['abb1']['status'] = 'complete'; fake.files['abb1'] = M4B
    assert run(worker, now).state == 'importing'
    assert published(ingest) == ['m4b']


def test_an_ebook_request_never_imports_audio(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', None)], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['ddd']['status'] = 'complete'; fake.files['ddd'] = M4B
    result = run(worker, now)
    assert result.state == 'failed' and result.error_code == 'format_not_allowed' and not list(ingest.iterdir())


def test_unknown_format_is_decided_by_the_bytes(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', None)], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['ddd']['status'] = 'complete'; fake.files['ddd'] = PDF
    assert run(worker, now).state == 'importing' and published(ingest) == ['pdf']


def test_a_non_book_file_fails_instead_of_importing(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', 'pdf')], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['bbb']['status'] = 'complete'; fake.files['bbb'] = b'<html>login required</html>'
    result = run(worker, now)
    assert result.state == 'failed' and result.error_code == 'no_usable_book' and not list(ingest.iterdir())


def test_disallowed_format_is_refused_after_download(flow):
    repository, worker, fake, offers, ingest, now = flow
    worker.media_allowed = lambda media: media != 'application/pdf'
    repository.create_job(1, offers[('ebook', None)], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['ddd']['status'] = 'complete'; fake.files['ddd'] = PDF
    result = run(worker, now)
    assert result.state == 'failed' and result.error_code == 'format_not_allowed'


@pytest.mark.parametrize('status', ['error', 'cancelled'])
def test_engine_failure_fails_the_job(flow, status):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', 'epub')], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['aaa']['status'] = status
    result = run(worker, now)
    assert result.state == 'failed' and result.error_code == 'client_job_failed'


def test_a_download_that_never_finishes_is_given_up(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', 'epub')], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['aaa']['status'] = 'downloading'
    now[0] += worker.download_deadline_seconds
    result = run(worker, now)
    assert result.state == 'failed' and result.error_code == 'client_job_stalled'


def test_cancel_seen_by_the_worker_also_stops_the_engine_download(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers[('ebook', 'epub')], 'click', requires_approval=False)
    queue = FakeClient.queue
    def queue_then_cancel(self, payload):
        repository.request_cancel(1, job.id)  # while the worker holds the job
        return queue(self, payload)
    FakeClient.queue = queue_then_cancel
    try:
        run(worker, now)
    finally:
        FakeClient.queue = queue
    run(worker, now)
    assert repository.get_job(1, job.id).state == 'cancelled' and ('cancel', 'aaa') in fake.calls


def test_job_offer_reads_the_durable_offer_for_its_owner_only(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers[('ebook', 'epub')], 'click', requires_approval=False)
    now[0] += 86400
    assert repository.job_offer(1, job.id)['task_id'] == 'aaa'
    with pytest.raises(s.NotFound):
        repository.job_offer(2, job.id)


def test_completed_file_missing_fails_cleanly(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', 'epub')], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['aaa']['status'] = 'complete'
    result = run(worker, now)
    assert result.state == 'failed' and result.error_code == 'artifact_unavailable'


def test_restart_after_staging_never_downloads_again(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', 'pdf')], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['bbb']['status'] = 'complete'; fake.files['bbb'] = PDF
    assert run(worker, now).state == 'importing'
    downloads = [c for c in fake.calls if c[0] == 'download']
    assert run(worker, now).state == 'importing'
    assert [c for c in fake.calls if c[0] == 'download'] == downloads


def test_retry_after_failure_queues_the_release_again(flow):
    repository, worker, fake, offers, ingest, now = flow
    job = repository.create_job(1, offers[('ebook', 'epub')], 'click', requires_approval=False)
    run(worker, now)
    fake.tasks['aaa']['status'] = 'error'
    assert run(worker, now).state == 'failed'
    # The engine still lists the failed download; the retry must not fail on it.
    repository.retry(1, job.id)
    assert run(worker, now).state == 'downloading'
    assert fake.tasks['aaa']['status'] == 'queued'
    assert [c for c in fake.calls if c[0] == 'queue'] == [('queue', 'aaa'), ('queue', 'aaa')]
    fake.tasks['aaa']['status'] = 'error'
    # A failure seen while following this attempt's own download is final.
    assert run(worker, now).state == 'failed'


def test_a_new_request_after_a_failure_downloads_again(flow):
    repository, worker, fake, offers, ingest, now = flow
    repository.create_job(1, offers[('ebook', 'epub')], 'first', requires_approval=False)
    run(worker, now)
    fake.tasks['aaa']['status'] = 'error'
    assert run(worker, now).state == 'failed'
    repository.create_job(1, offers[('ebook', 'epub')], 'second', requires_approval=False)
    assert run(worker, now).state == 'downloading' and fake.tasks['aaa']['status'] == 'queued'
    fake.tasks['aaa']['status'] = 'complete'; fake.files['aaa'] = epub_bytes()
    assert run(worker, now).state == 'importing'
