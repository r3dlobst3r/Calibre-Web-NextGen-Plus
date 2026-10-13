# SPDX-License-Identifier: GPL-3.0-or-later
"""Shelfmark through the real Flask routes: search, requests, cancel and Admin → Downloads."""
import json
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import MetaData, create_engine, text

from tests.unit.test_acquisition_shelfmark import BOOK, FakeShelfmark

CONFIG = {'endpoint': 'http://shelfmark:8084', 'secret': 'KEY-123', 'allow_mobi': False}


@pytest.fixture
def api(tmp_path, monkeypatch):
    from cps import constants, ub
    from cps.api import api_v1
    import cps.api as blueprint
    from cps.api import acquisition as module
    from cps.api import downloads_admin as admin_module
    from cps.services.acquisition import shelfmark as sm
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
        connection.execute(text("INSERT INTO settings VALUES (1,'epub,pdf')"))
        connection.execute(text('CREATE TABLE acquisition_schema_migration (version INTEGER,status TEXT)'))
        connection.execute(text("INSERT INTO acquisition_schema_migration VALUES (1,'preserved')"))
    repo = Repository(engine, tables, SecretBox(load_or_create_key(tmp_path / 'acquisition.key')))
    connection = repo.create_connection('Shelfmark', 'shelfmark', dict(CONFIG), enabled=True)
    fake = FakeShelfmark()
    actor = SimpleNamespace(id=2, is_authenticated=True, is_anonymous=False)
    for target in (module, blueprint):
        monkeypatch.setattr(target, 'current_user', actor)
    monkeypatch.setattr(ub, 'app_DB_path', str(database))
    monkeypatch.setattr(blueprint, 'config', SimpleNamespace(config_allow_reverse_proxy_header_login=False, config_anonbrowse=0))
    available = lambda: {'available': True, 'reasons': []}
    monkeypatch.setattr(module, 'worker_available', available)
    monkeypatch.setattr(admin_module, 'worker_available', available)
    monkeypatch.setattr(module, 'ShelfmarkService', lambda repo: sm.ShelfmarkService(repo, client_factory=fake.client))
    monkeypatch.setattr(module, '_owned_book_id', lambda publication: None)
    app = Flask(__name__); app.config.update(SECRET_KEY='fixture-secret', RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False)
    if module.limiter is not None:
        module.limiter.init_app(app)
    app.register_blueprint(api_v1)
    yield SimpleNamespace(client=app.test_client(), repo=repo, actor=actor, module=module, admin=admin_module,
                          sm=sm, connection=connection, fake=fake, database=database, monkeypatch=monkeypatch)
    engine.dispose()


def search(api, query='dune'):
    return api.client.get('/api/v1/acquisition/catalog', query_string={'connection': api.connection.id, 'q': query})


def releases(api):
    book = search(api).get_json()['publications'][0]
    return api.client.get('/api/v1/acquisition/catalog', query_string={
        'connection': api.connection.id, 'selection': book['navigation'][0]['selection']}).get_json()


def test_bootstrap_lists_shelfmark_without_its_secret(api):
    response = api.client.get('/api/v1/acquisition')
    assert [c['adapter'] for c in response.get_json()['connections']] == ['shelfmark']
    assert 'KEY-123' not in response.get_data(as_text=True)


def test_search_returns_books_and_marks_owned_ones(api):
    api.monkeypatch.setattr(api.module, '_owned_book_id', lambda publication: 42 if publication['title'] == 'Dune' else None)
    response = search(api)
    assert response.status_code == 200
    book = response.get_json()['publications'][0]
    assert book['title'] == 'Dune' and book['library_book_id'] == 42
    assert 'KEY-123' not in response.get_data(as_text=True) and 'shelfmark:8084' not in response.get_data(as_text=True)


def test_a_failing_ownership_check_never_fails_search(api):
    def broken(publication):
        raise RuntimeError('library unavailable')
    api.monkeypatch.setattr(api.module, '_owned_book_id', broken)
    response = search(api)
    assert response.status_code == 200 and 'library_book_id' not in response.get_json()['publications'][0]


def test_release_offers_follow_the_upload_formats(api):
    result = releases(api)
    formats = sorted(o['format'] for p in result['publications'] for o in p['offers'])
    assert formats == ['EPUB', 'PDF', 'Unknown']
    with api.repo.engine.begin() as con:
        con.execute(text("UPDATE settings SET config_upload_formats='pdf'"))
    formats = sorted(o['format'] for p in releases(api)['publications'] for o in p['offers'])
    assert formats == ['PDF', 'Unknown']


def test_requesting_a_release_creates_a_job(api):
    epub = next(p for p in releases(api)['publications'] if p['format'] == 'epub')
    response = api.client.post('/api/v1/acquisition/jobs', json={'connection_id': api.connection.id,
        'offer_id': epub['offers'][0]['offer_id'], 'idempotency_key': 'k1', 'add_to_my_library': True})
    assert response.status_code == 202
    job = response.get_json()
    assert job['state'] == 'queued' and job['title'] == 'Dune' and 'offer_id' not in job


def test_shelfmark_explains_why_search_failed(api):
    def failing(config):
        client = api.fake.client(config)
        def search(*args, **kwargs):
            error = api.sm.ShelfmarkError('shelfmark_unavailable')
            error.detail = 'No metadata provider configured. Enable one in Settings.'
            raise error
        client.search = search
        return client
    api.monkeypatch.setattr(api.module, 'ShelfmarkService', lambda repo: api.sm.ShelfmarkService(repo, client_factory=failing))
    response = search(api)
    assert response.status_code == 502
    assert response.get_json()['error'] == {'code': 'shelfmark_unavailable',
        'message': 'No metadata provider configured. Enable one in Settings.'}


def test_invalid_search_is_a_client_error(api):
    response = search(api, 'x' * 501)
    assert response.status_code == 400 and response.get_json()['error']['code'] == 'invalid_search'


def test_cancelling_an_idle_download_tells_shelfmark(api):
    cancelled = []

    class Client:
        def __init__(self, config):
            assert config['secret'] == 'KEY-123'

        def cancel(self, task_id):
            cancelled.append(task_id)
    api.monkeypatch.setattr(api.sm, 'ShelfmarkClient', Client)
    epub = next(p for p in releases(api)['publications'] if p['format'] == 'epub')
    job = api.client.post('/api/v1/acquisition/jobs', json={'connection_id': api.connection.id,
        'offer_id': epub['offers'][0]['offer_id'], 'idempotency_key': 'k1', 'add_to_my_library': True}).get_json()
    with api.repo.engine.begin() as con:
        con.execute(text("UPDATE acquisition_job SET state='downloading' WHERE id=:id"), {'id': job['id']})
    response = api.client.post('/api/v1/acquisition/jobs/' + job['id'] + '/cancel', json={})
    assert response.get_json()['state'] == 'cancelled' and cancelled == ['aaa']


def test_cancelling_a_job_never_sent_to_shelfmark_does_not_contact_it(api):
    api.monkeypatch.setattr(api.sm, 'ShelfmarkClient', lambda config: pytest.fail('contacted Shelfmark'))
    epub = next(p for p in releases(api)['publications'] if p['format'] == 'epub')
    job = api.client.post('/api/v1/acquisition/jobs', json={'connection_id': api.connection.id,
        'offer_id': epub['offers'][0]['offer_id'], 'idempotency_key': 'k1', 'add_to_my_library': True}).get_json()
    assert api.client.post('/api/v1/acquisition/jobs/' + job['id'] + '/cancel', json={}).get_json()['state'] == 'cancelled'


@pytest.mark.parametrize('value,forms', [('0441013597', {'0441013597', '9780441013593'}),
                                         ('978-0-441-01359-3', {'9780441013593', '0441013597'}),
                                         ('979-10-90636-07-1', {'9791090636071'}), ('123', set())])
def test_isbn_forms_cover_both_lengths(api, value, forms):
    assert api.module._isbn_forms(value) == forms


# -- Admin → Downloads -----------------------------------------------------------

SETTINGS = {'groups': [], 'tabs': [
    {'name': 'search_mode', 'fields': [{'key': 'SEARCH_MODE', 'type': 'SelectField', 'value': 'universal'},
                                        {'key': 'METADATA_PROVIDER', 'type': 'SelectField', 'value': 'openlibrary'}]},
    {'name': 'openlibrary', 'fields': [{'key': 'OPENLIBRARY_ENABLED', 'type': 'CheckboxField', 'value': False}]},
    {'name': 'security', 'fields': [{'key': 'AUTH_METHOD', 'type': 'SelectField', 'value': 'builtin'}]},
    {'name': 'prowlarr_config', 'fields': [{'key': 'PROWLARR_API_KEY', 'type': 'PasswordField', 'value': 'SECRET'}]}]}


class AdminClient:
    """Shelfmark as found on first start: Open Library off, nothing selected."""
    instances = []
    providers = {}

    def __init__(self, config):
        self.config, self.calls = config, []
        AdminClient.instances.append(self)

    def metadata_providers(self):
        state = AdminClient.providers
        if state.get('lag'):
            # Shelfmark answers from another worker that has not reloaded yet.
            state['lag'] -= 1
            return {'providers': [{'name': n, 'enabled': on} for n, on in state['enabled'].items()],
                    'configured_provider': None}
        return {'providers': [{'name': n, 'enabled': on} for n, on in state['enabled'].items()],
                'configured_provider': state['configured'] or None}

    def config_info(self):
        if self.config['secret'] == 'WRONG':
            from cps.services.acquisition.shelfmark import ShelfmarkError
            raise ShelfmarkError('needs_auth')
        return {}

    def settings(self):
        return json.loads(json.dumps(SETTINGS))

    def update_settings(self, tab, values):
        self.calls.append(('update', tab, values))
        state = AdminClient.providers
        if state.get('reject') == tab:
            return {'success': False, 'message': 'Shelfmark refused'}
        if 'OPENLIBRARY_ENABLED' in values:
            state['enabled']['openlibrary'] = values['OPENLIBRARY_ENABLED']
        if 'METADATA_PROVIDER' in values:
            state['configured'] = values['METADATA_PROVIDER']
            state['lag'] = state.get('lag_after_save', 0)
        return {'success': True, 'updated': list(values)}

    def settings_action(self, tab, action, values):
        self.calls.append(('action', tab, action, values))
        return {'success': False, 'message': 'Connection failed'}

    def skip_onboarding(self):
        self.calls.append(('skip_onboarding',))
        return {'success': True}


@pytest.fixture
def admin(api, monkeypatch):
    api.actor.id = 1
    AdminClient.instances = []
    AdminClient.providers = {'enabled': {'openlibrary': False, 'hardcover': False}, 'configured': ''}
    monkeypatch.setattr(api.admin, 'ShelfmarkClient', AdminClient)
    monkeypatch.setenv('SHELFMARK_URL', 'http://shelfmark:8084')
    monkeypatch.setenv('SHELFMARK_API_KEY', 'KEY-NEW')
    return api


def test_downloads_admin_is_admin_only(api):
    for method, url in (('get', '/api/v1/admin/downloads'), ('post', '/api/v1/admin/downloads/setup'),
                        ('get', '/api/v1/admin/downloads/settings'), ('put', '/api/v1/admin/downloads/settings/network')):
        assert getattr(api.client, method)(url, json={}).status_code == 403


def test_status_reports_the_connection_without_its_key(admin):
    response = admin.client.get('/api/v1/admin/downloads')
    body = response.get_json()
    assert body['reachable'] is True and body['environment'] == {'configured': True, 'endpoint': 'http://shelfmark:8084'}
    assert body['connection']['adapter'] == 'shelfmark' and body['connection']['endpoint'] == 'http://shelfmark:8084'
    assert 'KEY' not in response.get_data(as_text=True)


def test_setup_updates_the_key_keeps_options_and_readies_search(admin):
    admin.repo.set_connection_enabled(admin.connection.id, False)
    admin.repo.update_connection(admin.connection.id, label='Shelfmark', config=dict(CONFIG, allow_mobi=True))
    response = admin.client.post('/api/v1/admin/downloads/setup')
    assert response.status_code == 200 and response.get_json()['provider_enabled'] is True
    rows = [r for r in admin.repo.list_connections(include_disabled=True) if r.adapter == 'shelfmark']
    assert len(rows) == 1 and rows[0].enabled
    stored = admin.repo.connection_config(rows[0].id).config
    assert stored == {'endpoint': 'http://shelfmark:8084', 'secret': 'KEY-NEW', 'allow_mobi': True}
    calls = AdminClient.instances[0].calls
    # Open Library first, then selected explicitly: Shelfmark reports a default
    # provider it has not stored, so search fails until one is really chosen.
    assert calls == [('skip_onboarding',), ('update', 'openlibrary', {'OPENLIBRARY_ENABLED': True}),
                     ('update', 'search_mode', {'SEARCH_MODE': 'universal', 'METADATA_PROVIDER': 'openlibrary'})]


def test_setup_leaves_a_working_provider_alone(admin):
    AdminClient.providers = {'enabled': {'openlibrary': False, 'hardcover': True}, 'configured': 'hardcover'}
    response = admin.client.post('/api/v1/admin/downloads/setup')
    assert response.status_code == 200 and response.get_json()['provider_enabled'] is False
    assert AdminClient.instances[0].calls == [('skip_onboarding',)]


def test_setup_selects_an_enabled_provider_nobody_chose(admin):
    AdminClient.providers = {'enabled': {'openlibrary': False, 'hardcover': True}, 'configured': ''}
    assert admin.client.post('/api/v1/admin/downloads/setup').status_code == 200
    assert AdminClient.instances[0].calls[1:] == [
        ('update', 'search_mode', {'SEARCH_MODE': 'universal', 'METADATA_PROVIDER': 'hardcover'})]


def test_setup_waits_for_shelfmark_to_pick_up_the_selection(admin, monkeypatch):
    waits = []
    monkeypatch.setattr(admin.admin.time, 'sleep', waits.append)
    AdminClient.providers['lag_after_save'] = 2
    response = admin.client.post('/api/v1/admin/downloads/setup')
    assert response.status_code == 200 and response.get_json()['provider_enabled'] is True
    assert waits == [0.25, 0.25]


def test_setup_gives_up_when_the_selection_never_appears(admin, monkeypatch):
    monkeypatch.setattr(admin.admin.time, 'sleep', lambda seconds: None)
    AdminClient.providers['lag_after_save'] = 100
    response = admin.client.post('/api/v1/admin/downloads/setup')
    assert response.status_code == 502 and response.get_json()['error']['code'] == 'shelfmark_unavailable'


def test_setup_reports_a_refused_provider_change(admin):
    AdminClient.providers['reject'] = 'openlibrary'
    response = admin.client.post('/api/v1/admin/downloads/setup')
    assert response.status_code == 502 and response.get_json()['error']['message'] == 'Shelfmark refused'


def test_setup_creates_the_connection_when_missing(admin):
    admin.repo.delete_connection(admin.connection.id)
    assert admin.client.post('/api/v1/admin/downloads/setup').status_code == 200
    rows = [r for r in admin.repo.list_connections() if r.adapter == 'shelfmark']
    assert len(rows) == 1 and rows[0].label == 'Shelfmark'


def test_setup_refuses_a_key_shelfmark_rejects(admin):
    response = admin.client.post('/api/v1/admin/downloads/setup', json={'endpoint': 'http://shelfmark:8084', 'secret': 'WRONG'})
    assert response.status_code == 502 and response.get_json()['error']['code'] == 'shelfmark_auth'
    assert admin.repo.connection_config(admin.connection.id).config['secret'] == 'KEY-123'


def test_setup_without_configuration_explains_what_is_missing(admin, monkeypatch):
    monkeypatch.delenv('SHELFMARK_URL'); monkeypatch.delenv('SHELFMARK_API_KEY')
    response = admin.client.post('/api/v1/admin/downloads/setup')
    assert response.status_code == 400 and response.get_json()['error']['code'] == 'shelfmark_not_configured'


def test_settings_are_filtered_and_redacted(admin):
    response = admin.client.get('/api/v1/admin/downloads/settings')
    names = [tab['name'] for tab in response.get_json()['tabs']]
    assert names == ['search_mode', 'openlibrary', 'prowlarr_config']
    assert 'SECRET' not in response.get_data(as_text=True)


def test_settings_writes_reach_shelfmark_for_shown_tabs_only(admin):
    ok = admin.client.put('/api/v1/admin/downloads/settings/network', json={'USE_DOH': True})
    assert ok.status_code == 200 and AdminClient.instances[-1].calls == [('update', 'network', {'USE_DOH': True})]
    refused = admin.client.put('/api/v1/admin/downloads/settings/security', json={'AUTH_METHOD': 'none'})
    assert refused.status_code == 404
    assert all(call[1] != 'security' for instance in AdminClient.instances for call in instance.calls)


def test_settings_actions_pass_through_their_result(admin):
    response = admin.client.post('/api/v1/admin/downloads/settings/prowlarr_config/action/test_connection',
                                 json={'PROWLARR_URL': 'http://prowlarr'})
    assert response.status_code == 200
    assert response.get_json() == {'success': False, 'message': 'Connection failed'}
    assert admin.client.post('/api/v1/admin/downloads/settings/network/action/../x', json={}).status_code == 404


@pytest.mark.parametrize('library,result,same', [
    ('Moby-Dick; or, The Whale', 'Moby Dick', True),
    ('Dune', 'Dune', True),
    ('The Hobbit: or There and Back Again', 'The Hobbit', True),
    ('Café Society', 'Cafe Society', True),
    ('Dune Messiah', 'Dune', False),
    ('Volume 1', 'Volume 2', False),
    ('', 'Anything', False),
])
def test_owned_titles_allow_a_subtitle_but_not_a_different_book(api, library, result, same):
    assert api.module.same_title(library, result) is same
