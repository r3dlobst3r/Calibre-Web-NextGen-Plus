# SPDX-License-Identifier: GPL-3.0-or-later
"""The built-in downloader through the real Flask routes: search, requests, cancel, Admin → Downloads."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, text

from tests.unit.api_app_world import make_app
from tests.unit.test_acquisition_downloader import RECORD, FakeEngine

CONFIG = {'allow_mobi': False, 'metadata_provider': 'openlibrary'}


@pytest.fixture
def api(tmp_path, monkeypatch):
    from cps import constants, ub
    from cps.api import api_v1
    import cps.api as blueprint
    from cps.api import acquisition as module
    from cps.api import downloads_admin as admin_module
    from cps.services.acquisition import downloader as dl
    from cps.services.acquisition.storage import Repository, define_tables
    from cps.services.acquisition.secrets import SecretBox, load_or_create_key
    database = tmp_path / 'app.db'; engine = create_engine('sqlite:///' + str(database))
    tables = define_tables(MetaData()); tables.jobs.metadata.create_all(engine)
    access = constants.ROLE_ACQUISITION_ACCESS
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE user (id INTEGER PRIMARY KEY,name TEXT,role INTEGER)'))
        connection.execute(text("INSERT INTO user VALUES (1,'Admin',:admin),(2,'Reader',:access)"),
                           {'admin': constants.ROLE_ADMIN | access, 'access': access | constants.ROLE_ACQUISITION_AUTO_APPROVE})
        connection.execute(text('CREATE TABLE settings (config_acquisition_enabled INTEGER,config_upload_formats TEXT)'))
        connection.execute(text("INSERT INTO settings VALUES (1,'epub,pdf,m4b')"))
        connection.execute(text('CREATE TABLE acquisition_schema_migration (version INTEGER,status TEXT)'))
        connection.execute(text("INSERT INTO acquisition_schema_migration VALUES (1,'preserved')"))
    repo = Repository(engine, tables, SecretBox(load_or_create_key(tmp_path / 'acquisition.key')))
    connection = repo.create_connection('Downloads', 'downloader', dict(CONFIG), enabled=True)
    fake = FakeEngine()
    searches = []
    def book_search(locale):
        def search(query, provider):
            searches.append((query, provider, locale))
            return [dict(RECORD), dict(RECORD, id='OL2W', title='Owned Book')]
        return search
    actor = SimpleNamespace(id=2, is_authenticated=True, is_anonymous=False, locale='fr')
    for target in (module, blueprint):
        monkeypatch.setattr(target, 'current_user', actor)
    monkeypatch.setattr(ub, 'app_DB_path', str(database))
    monkeypatch.setattr(blueprint, 'config', SimpleNamespace(config_allow_reverse_proxy_header_login=False, config_anonbrowse=0))
    available = lambda: {'available': True, 'reasons': []}
    monkeypatch.setattr(module, 'worker_available', available)
    monkeypatch.setattr(admin_module, 'worker_available', available)
    monkeypatch.setattr(module, '_book_search', book_search)
    feeds = []
    def discover(locale):
        def records(feed):
            feeds.append((feed, locale))
            return [dict(RECORD, id='OL9W', title='Popular Book'), dict(RECORD, id='OL2W', title='Owned Book')]
        return records
    monkeypatch.setattr(module, '_discover', discover)
    monkeypatch.setattr(module, 'DownloaderService',
        lambda repo, book_search=None, discover=None: dl.DownloaderService(
            repo, book_search=book_search, discover=discover, engine_factory=fake.factory))
    monkeypatch.setattr(dl, 'local_engine', fake.factory)
    monkeypatch.setattr(module, '_owned_book_id', lambda publication: None)
    app = make_app(SECRET_KEY='fixture-secret', RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False)
    if module.limiter is not None:
        module.limiter.init_app(app)
    app.register_blueprint(api_v1)
    yield SimpleNamespace(client=app.test_client(), repo=repo, actor=actor, module=module, admin=admin_module,
                          dl=dl, connection=connection, fake=fake, searches=searches, feeds=feeds, monkeypatch=monkeypatch)
    engine.dispose()


def search(api, query='dune'):
    return api.client.get('/api/v1/acquisition/catalog', query_string={'connection': api.connection.id, 'q': query})


def releases(api, kind='ebook'):
    book = search(api).get_json()['publications'][0]
    nav = next(n for n in book['navigation'] if n['relations'] == ['downloader:' + kind])
    return api.client.get('/api/v1/acquisition/catalog', query_string={
        'connection': api.connection.id, 'selection': nav['selection']}).get_json()


def request_release(api, release, key='k1'):
    return api.client.post('/api/v1/acquisition/jobs', json={'connection_id': api.connection.id,
        'offer_id': release['offers'][0]['offer_id'], 'idempotency_key': key, 'add_to_my_library': True})


def test_bootstrap_lists_the_downloader(api):
    assert [c['adapter'] for c in api.client.get('/api/v1/acquisition').get_json()['connections']] == ['downloader']


def test_search_uses_the_readers_locale_and_marks_owned_books(api):
    api.monkeypatch.setattr(api.module, '_owned_book_id', lambda p: 42 if p['title'] == 'Owned Book' else None)
    response = search(api)
    assert response.status_code == 200 and api.searches == [('dune', 'openlibrary', 'fr')]
    books = {b['title']: b for b in response.get_json()['publications']}
    assert books['Owned Book']['library_book_id'] == 42 and 'library_book_id' not in books['Dune']


def feed(api, name):
    return api.client.get('/api/v1/acquisition/catalog', query_string={'connection': api.connection.id, 'feed': name})


def test_a_popular_feed_lists_books_that_can_be_downloaded(api):
    api.monkeypatch.setattr(api.module, '_owned_book_id', lambda p: 42 if p['title'] == 'Owned Book' else None)
    response = feed(api, 'genre:fantasy:week')
    assert response.status_code == 200 and api.feeds == [('genre:fantasy:week', 'fr')]
    body = response.get_json()
    assert body['feed'] == 'genre:fantasy:week'
    books = {b['title']: b for b in body['publications']}
    assert books['Owned Book']['library_book_id'] == 42
    nav = books['Popular Book']['navigation'][0]
    assert nav['relations'] == ['downloader:ebook']
    # The book opens its releases exactly like a searched one.
    found = api.client.get('/api/v1/acquisition/catalog', query_string={
        'connection': api.connection.id, 'selection': nav['selection']}).get_json()
    assert found['publications']


def test_an_unknown_feed_is_refused_before_anything_is_fetched(api):
    for name in ('genre:astrology:week', 'popular:month', 'genre:fantasy', '../etc'):
        assert feed(api, name).status_code == 400, name
    assert api.feeds == []


def test_a_feed_that_cannot_be_read_says_so(api):
    def broken(locale):
        def records(feed):
            raise OSError('openlibrary down')
        return records
    api.monkeypatch.setattr(api.module, '_discover', broken)
    response = feed(api, 'popular:week')
    assert response.status_code == 502 and response.get_json()['error']['code'] == 'search_unavailable'


def test_a_failing_ownership_check_never_fails_search(api):
    def broken(publication):
        raise RuntimeError('library unavailable')
    api.monkeypatch.setattr(api.module, '_owned_book_id', broken)
    assert search(api).status_code == 200


def test_release_offers_follow_the_upload_formats(api):
    formats = sorted(o['format'] for p in releases(api)['publications'] for o in p['offers'])
    assert formats == ['EPUB', 'PDF', 'Unknown']
    audio = sorted(o['format'] for p in releases(api, 'audiobook')['publications'] for o in p['offers'])
    assert audio == ['M4B', 'Unknown']
    with api.repo.engine.begin() as con:
        con.execute(text("UPDATE settings SET config_upload_formats='epub'"))
    assert sorted(o['format'] for p in releases(api, 'audiobook')['publications'] for o in p['offers']) == ['Unknown']


def test_requesting_ebook_and_audiobook_releases_creates_jobs(api):
    epub = next(p for p in releases(api)['publications'] if p['format'] == 'epub')
    response = request_release(api, epub)
    assert response.status_code == 202 and response.get_json()['state'] == 'queued'
    m4b = next(p for p in releases(api, 'audiobook')['publications'] if p['format'] == 'm4b')
    assert request_release(api, m4b, 'k2').status_code == 202


def test_invalid_search_is_a_client_error(api):
    response = search(api, 'x' * 501)
    assert response.status_code == 400 and response.get_json()['error']['code'] == 'invalid_search'


def test_cancelling_an_idle_download_stops_the_engine(api):
    epub = next(p for p in releases(api)['publications'] if p['format'] == 'epub')
    job = request_release(api, epub).get_json()
    with api.repo.engine.begin() as con:
        con.execute(text("UPDATE acquisition_job SET state='downloading' WHERE id=:id"), {'id': job['id']})
    response = api.client.post('/api/v1/acquisition/jobs/' + job['id'] + '/cancel', json={})
    assert response.get_json()['state'] == 'cancelled' and ('cancel', 'aaa') in api.fake.calls


def test_cancelling_a_job_never_sent_to_the_engine_does_not_touch_it(api):
    epub = next(p for p in releases(api)['publications'] if p['format'] == 'epub')
    job = request_release(api, epub).get_json()
    assert api.client.post('/api/v1/acquisition/jobs/' + job['id'] + '/cancel', json={}).get_json()['state'] == 'cancelled'
    assert not any(call[0] == 'cancel' for call in api.fake.calls)


@pytest.mark.parametrize('value,forms', [('0441013597', {'0441013597', '9780441013593'}),
                                         ('978-0-441-01359-3', {'9780441013593', '0441013597'}),
                                         ('979-10-90636-07-1', {'9791090636071'}), ('123', set())])
def test_isbn_forms_cover_both_lengths(api, value, forms):
    assert api.module._isbn_forms(value) == forms


@pytest.mark.parametrize('library,result,same', [
    ('Moby-Dick; or, The Whale', 'Moby Dick', True), ('Dune', 'Dune', True),
    ('The Hobbit: or There and Back Again', 'The Hobbit', True), ('Café Society', 'Cafe Society', True),
    ('Dune Messiah', 'Dune', False), ('Volume 1', 'Volume 2', False), ('', 'Anything', False)])
def test_owned_titles_allow_a_subtitle_but_not_a_different_book(api, library, result, same):
    assert api.module.same_title(library, result) is same


# -- Admin → Downloads -----------------------------------------------------------

class FakeRegistry:
    def __init__(self):
        self.calls = []

    def serialize_all_settings(self, include_values=True):
        return {'groups': [], 'tabs': [
            {'name': 'security', 'fields': [{'key': 'AUTH_METHOD', 'type': 'SelectField', 'value': 'builtin'}]},
            {'name': 'download_sources', 'fields': [
                {'key': 'DIRECT_DOWNLOAD_ENABLED', 'type': 'CheckboxField', 'value': True},
                {'key': 'AA_DONATOR_KEY', 'type': 'PasswordField', 'value': 'SECRET'}]}]}

    def update_settings(self, tab, values):
        self.calls.append(('update', tab, values))
        return {'success': True, 'updated': list(values)}

    def execute_action(self, tab, action, values):
        self.calls.append(('action', tab, action, values))
        return {'success': False, 'message': 'Mirror did not answer'}


class FakeOrchestrator:
    def queue_status(self):
        return {'downloading': {'aaa': {'title': 'Dune', 'source_display_name': "Anna's Archive",
                                        'progress': 40.0, 'status_message': 'Downloading', 'added_time': 5}}}


@pytest.fixture
def admin(api, monkeypatch):
    api.actor.id = 1
    registry = FakeRegistry()
    modules = {'core.settings_registry': registry, 'download.orchestrator': FakeOrchestrator()}
    monkeypatch.setattr(api.admin, '_engine', lambda: SimpleNamespace(engine=modules.__getitem__))
    api.registry = registry
    return api


def test_downloads_admin_is_admin_only(api):
    for method, url in (('get', '/api/v1/admin/downloads'), ('patch', '/api/v1/admin/downloads'),
                        ('get', '/api/v1/admin/downloads/settings'), ('put', '/api/v1/admin/downloads/settings/network'),
                        ('get', '/api/v1/admin/downloads/queue')):
        assert getattr(api.client, method)(url, json={}).status_code == 403


def test_status_reports_the_connection_and_enabled_sources(admin):
    body = admin.client.get('/api/v1/admin/downloads').get_json()
    assert body['enabled'] is True and body['connection']['config'] == CONFIG
    assert body['sources'] == {'ebook': ['direct_download', 'libgen'], 'audiobook': ['audiobookbay']}
    assert body['source_problems'] == []


def test_status_names_a_switched_on_source_that_cannot_be_searched(admin):
    admin.fake.problems = [{'source': 'direct_download', 'reason': 'needs_mirror'}]
    body = admin.client.get('/api/v1/admin/downloads').get_json()
    assert body['source_problems'] == [{'source': 'direct_download', 'reason': 'needs_mirror'}]


def test_switching_off_and_on_keeps_one_connection(admin):
    assert admin.client.patch('/api/v1/admin/downloads', json={'enabled': False}).get_json()['enabled'] is False
    body = admin.client.patch('/api/v1/admin/downloads', json={'enabled': True}).get_json()
    assert body['enabled'] is True
    assert len([r for r in admin.repo.list_connections(include_disabled=True) if r.adapter == 'downloader']) == 1


def test_changing_options_keeps_downloads_switched_on(admin):
    body = admin.client.patch('/api/v1/admin/downloads', json={'metadata_provider': 'hardcover', 'allow_mobi': True}).get_json()
    assert body['enabled'] is True and body['connection']['config'] == {'allow_mobi': True, 'metadata_provider': 'hardcover'}
    assert admin.client.patch('/api/v1/admin/downloads', json={'metadata_provider': 'amazon'}).status_code == 400


def test_first_switch_on_creates_the_source(admin):
    admin.repo.delete_connection(admin.connection.id)
    body = admin.client.patch('/api/v1/admin/downloads', json={'enabled': True}).get_json()
    assert body['enabled'] is True and body['connection']['label'] == 'Downloads'


def test_settings_are_filtered_and_redacted(admin):
    response = admin.client.get('/api/v1/admin/downloads/settings')
    assert [t['name'] for t in response.get_json()['tabs']] == ['download_sources']
    assert 'SECRET' not in response.get_data(as_text=True)


def test_settings_writes_reach_the_engine_for_shown_fields_only(admin):
    ok = admin.client.put('/api/v1/admin/downloads/settings/network', json={'USE_DOH': True})
    assert ok.status_code == 200 and admin.registry.calls == [('update', 'network', {'USE_DOH': True})]
    assert admin.client.put('/api/v1/admin/downloads/settings/security', json={'AUTH_METHOD': 'none'}).status_code == 404
    assert admin.client.put('/api/v1/admin/downloads/settings/downloads', json={'BOOKS_OUTPUT_MODE': 'email'}).status_code == 400
    assert admin.registry.calls == [('update', 'network', {'USE_DOH': True})]


def test_settings_actions_pass_through_their_verdict(admin):
    response = admin.client.post('/api/v1/admin/downloads/settings/mirrors/action/test_mirrors', json={})
    assert response.get_json() == {'success': False, 'message': 'Mirror did not answer'}


def test_queue_shows_what_the_engine_is_doing(admin):
    tasks = admin.client.get('/api/v1/admin/downloads/queue').get_json()['tasks']
    assert tasks == [{'id': 'aaa', 'status': 'downloading', 'title': 'Dune', 'source': "Anna's Archive",
                      'progress': 40.0, 'message': 'Downloading', 'added': 5}]
