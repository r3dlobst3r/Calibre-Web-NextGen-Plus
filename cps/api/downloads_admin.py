# SPDX-License-Identifier: GPL-3.0-or-later
"""Admin → Downloads: run the hidden Shelfmark sidecar from inside CWNG.

Shelfmark has no published port in this deployment, so everything an administrator
would have done in its own UI happens here: connecting it (from SHELFMARK_URL and
SHELFMARK_API_KEY), getting it ready to search, and its source/network settings.
The API key never leaves the server and stored passwords are never echoed back.
"""
from dataclasses import asdict
from pathlib import Path
import time

from flask import jsonify, request

from .. import ub
from ..services.acquisition import admission, runtime
from ..services.acquisition.shelfmark import (
    ShelfmarkClient,
    ShelfmarkError,
    connection_config,
    environment_config,
    settings_values,
    visible_settings,
)
from . import api_v1
from .acquisition import (
    _endpoint,
    _error,
    _json,
    _run_private_blocking,
    _shelfmark_error,
    worker_available,
)

LABEL = 'Shelfmark'


def _connection(repo):
    rows = [row for row in repo.list_connections(include_disabled=True) if row.adapter == 'shelfmark']
    return rows[0] if rows else None


def _client():
    """The configured sidecar client, from the stored connection or the environment."""
    database = ub.app_DB_path
    def load():
        if (Path(database).parent / 'acquisition.key').exists():
            with runtime.open_repository(database) as repo:
                row = _connection(repo)
                if row is not None:
                    return repo.connection_config(row.id, include_disabled=True).config
        return environment_config()
    config = _run_private_blocking(load)
    if config is None:
        raise ShelfmarkError('shelfmark_not_configured')
    return ShelfmarkClient(config)


def _status():
    environment = environment_config()
    state = dict(admission.instance_state(ub.app_DB_path), runtime=worker_available())
    result = {'environment': {'configured': environment is not None,
                              'endpoint': environment['endpoint'] if environment else None},
              'connection': None, 'reachable': False, 'error': None, 'acquisition': state}
    database = ub.app_DB_path
    def read():
        if not (Path(database).parent / 'acquisition.key').exists():
            return None
        with runtime.open_repository(database) as repo:
            row = _connection(repo)
            if row is None:
                return None
            config = repo.connection_config(row.id, include_disabled=True).config
            return asdict(row), config
    found = _run_private_blocking(read)
    config = environment
    if found is not None:
        row, config = found
        result['connection'] = dict(row, endpoint=config['endpoint'])
    if config is None:
        return result
    try:
        _run_private_blocking(lambda: ShelfmarkClient(config).config_info())
        result['reachable'] = True
    except ShelfmarkError as error:
        result['error'] = error.code
    return result


def ensure_search_ready(client, *, wait=None):
    """Make sure book search has an enabled metadata provider. Open Library needs no key.

    Shelfmark's settings listing reports a field's default where nothing is stored, so
    it cannot say which provider search really uses; its provider endpoint can.
    """
    def current():
        state = client.metadata_providers()
        enabled = [p['name'] for p in state.get('providers') or [] if isinstance(p, dict) and p.get('enabled') is True]
        return state.get('configured_provider'), enabled

    def save(tab, values):
        result = client.update_settings(tab, values)
        if isinstance(result, dict) and result.get('success') is False:
            error = ShelfmarkError('shelfmark_error')
            error.detail = result.get('message') if isinstance(result.get('message'), str) else None
            raise error

    configured, enabled = current()
    if configured and configured in enabled:
        return False
    if not enabled:
        save('openlibrary', {'OPENLIBRARY_ENABLED': True})
        enabled = ['openlibrary']
    target = 'openlibrary' if 'openlibrary' in enabled else enabled[0]
    # Selected explicitly: Shelfmark's own auto-selection can miss a provider
    # enabled a moment earlier.
    save('search_mode', {'SEARCH_MODE': 'universal', 'METADATA_PROVIDER': target})
    # Shelfmark's other workers pick a saved setting up a moment later.
    for _ in range(20):
        configured, enabled = current()
        if configured and configured in enabled:
            return True
        (wait or time.sleep)(0.25)
    raise ShelfmarkError('shelfmark_unavailable')


@api_v1.route('/admin/downloads', methods=['GET'])
@_endpoint(admin=True)
def downloads_status():
    return jsonify(_status())


@api_v1.route('/admin/downloads/setup', methods=['POST'])
@_endpoint(admin=True)
def downloads_setup():
    """Connect Shelfmark as a download source and get it ready to search.

    The address and key come from the environment unless the administrator types
    them in. Re-running it is safe: an unchanged connection is only re-enabled.
    """
    body = _json({'endpoint', 'secret'}) if request.get_data() else {}
    environment = environment_config() or {}
    try:
        config = connection_config({'endpoint': body.get('endpoint') or environment.get('endpoint') or '',
                                    'secret': body.get('secret') or environment.get('secret') or ''})
    except ShelfmarkError as error:
        return _error(error.code if environment or body else 'shelfmark_not_configured', 400)
    if admission.instance_state(ub.app_DB_path)['migration_status'] != 'ready':
        return _error('needs_review', 409)
    client = ShelfmarkClient(config)
    # Prove the address and key before anything is stored.
    _run_private_blocking(client.config_info)
    database = ub.app_DB_path
    def store():
        with runtime.open_repository(database, initialize_key=True) as repo:
            row = _connection(repo)
            if row is None:
                return repo.create_connection(LABEL, 'shelfmark', config, enabled=True).id
            current = repo.connection_config(row.id, include_disabled=True).config
            if any(current.get(key) != config[key] for key in ('endpoint', 'secret')):
                # Only the address and key change; an administrator's MOBI opt-in stays.
                repo.update_connection(row.id, label=row.label, expected_revision=row.revision,
                    config=dict(current, endpoint=config['endpoint'], secret=config['secret']))
                row = _connection(repo)
            if not row.enabled:
                repo.set_connection_enabled(row.id, True, expected_revision=row.revision)
            return row.id
    _run_private_blocking(store)
    def prepare():
        client.skip_onboarding()
        return ensure_search_ready(client)
    provider_enabled = _run_private_blocking(prepare)
    return jsonify(dict(_status(), provider_enabled=provider_enabled))


@api_v1.route('/admin/downloads/settings', methods=['GET'])
@_endpoint(admin=True)
def downloads_settings():
    try:
        client = _client()
        return jsonify(visible_settings(_run_private_blocking(client.settings)))
    except ShelfmarkError as error:
        return _shelfmark_error(error)


@api_v1.route('/admin/downloads/settings/<tab>', methods=['PUT'])
@_endpoint(admin=True)
def downloads_settings_update(tab):
    try:
        values = settings_values(tab, request.get_json(silent=True))
        client = _client()
        result = _run_private_blocking(lambda: client.update_settings(tab, values))
    except ShelfmarkError as error:
        return _shelfmark_error(error)
    # Shelfmark's own {success, message} verdict; the page shows the message either way.
    return jsonify(result)


@api_v1.route('/admin/downloads/settings/<tab>/action/<action>', methods=['POST'])
@_endpoint(admin=True)
def downloads_settings_action(tab, action):
    try:
        values = settings_values(tab, request.get_json(silent=True) or {})
        if not action.replace('_', '').isalnum() or len(action) > 64:
            raise ShelfmarkError('invalid_request')
        client = _client()
        result = _run_private_blocking(lambda: client.settings_action(tab, action, values))
    except ShelfmarkError as error:
        return _shelfmark_error(error)
    # Shelfmark's own {success, message} verdict; the page shows the message either way.
    return jsonify(result)
