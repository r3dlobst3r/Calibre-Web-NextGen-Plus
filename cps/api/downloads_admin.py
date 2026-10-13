# SPDX-License-Identifier: GPL-3.0-or-later
"""Admin → Downloads: switch the built-in downloader on and configure its sources.

The download engine runs inside CWNG (cps/services/downloader). This page turns it
into a book source, chooses the metadata provider search uses, and edits the
engine's own settings: sources, mirrors, download and debrid clients, the
Cloudflare bypass and the network. Stored passwords are never echoed back.
"""
from dataclasses import asdict
from pathlib import Path

from flask import jsonify, request

from . import api_v1
from .. import ub
from ..services.acquisition import admission, runtime
from ..services.acquisition.downloader import (ADAPTER, CONTENT_TYPES, DownloaderError, connection_config,
    settings_values, visible_settings)
from .acquisition import _endpoint, _error, _json, _run_private_blocking, worker_available

LABEL = 'Downloads'


def _connection(repo):
    rows = [row for row in repo.list_connections(include_disabled=True) if row.adapter == ADAPTER]
    return rows[0] if rows else None


def _engine():
    from ..services.downloader import runtime as engine_runtime
    engine_runtime.load_settings()
    return engine_runtime


def _status():
    database = ub.app_DB_path
    def read():
        if not (Path(database).parent / 'acquisition.key').exists():
            return None
        with runtime.open_repository(database) as repo:
            row = _connection(repo)
            if row is None:
                return None
            return asdict(row), repo.connection_config(row.id, include_disabled=True).config
    found = _run_private_blocking(read)
    def sources():
        from ..services.acquisition.downloader import local_engine
        engine = local_engine({})
        return {kind: engine.sources(kind) for kind in CONTENT_TYPES}
    return {
        'connection': dict(found[0], config=found[1]) if found else None,
        'enabled': bool(found and found[0]['enabled']),
        'sources': _run_private_blocking(sources),
        'acquisition': dict(admission.instance_state(ub.app_DB_path), runtime=worker_available()),
    }


@api_v1.route('/admin/downloads', methods=['GET'])
@_endpoint(admin=True)
def downloads_status():
    return jsonify(_status())


@api_v1.route('/admin/downloads', methods=['PATCH'])
@_endpoint(admin=True)
def downloads_update():
    """Turn the downloader on or off, and set its own options.

    The first switch-on creates the book source; later changes edit it. Requests
    already in progress keep running; the connection only refuses new ones.
    """
    body = _json({'enabled', 'allow_mobi', 'metadata_provider'})
    if 'enabled' in body and not isinstance(body['enabled'], bool):
        return _error('invalid_request', 400)
    if admission.instance_state(ub.app_DB_path)['migration_status'] != 'ready':
        return _error('needs_review', 409)
    database = ub.app_DB_path
    def store():
        with runtime.open_repository(database, initialize_key=True) as repo:
            row = _connection(repo)
            current = repo.connection_config(row.id, include_disabled=True).config if row else {}
            options = {key: body[key] for key in ('allow_mobi', 'metadata_provider') if key in body}
            config = connection_config(dict(current, **options))
            if row is None:
                row = repo.create_connection(LABEL, ADAPTER, config, enabled=bool(body.get('enabled', True)))
                return
            # Editing a connection switches it off; keep the state it had unless asked.
            enabled = body.get('enabled', row.enabled)
            if config != current:
                repo.update_connection(row.id, label=row.label, config=config, expected_revision=row.revision)
                row = _connection(repo)
            if row.enabled != enabled:
                repo.set_connection_enabled(row.id, enabled, expected_revision=row.revision)
    _run_private_blocking(store)
    return jsonify(_status())


@api_v1.route('/admin/downloads/settings', methods=['GET'])
@_endpoint(admin=True)
def downloads_settings():
    def read():
        engine = _engine()
        return visible_settings(engine.engine('core.settings_registry').serialize_all_settings(include_values=True))
    return jsonify(_run_private_blocking(read))


@api_v1.route('/admin/downloads/settings/<tab>', methods=['PUT'])
@_endpoint(admin=True)
def downloads_settings_update(tab):
    values = settings_values(tab, request.get_json(silent=True))
    def save():
        engine = _engine()
        return engine.engine('core.settings_registry').update_settings(tab, values)
    # The engine's own {success, message} verdict; the page shows the message either way.
    return jsonify(_run_private_blocking(save))


@api_v1.route('/admin/downloads/settings/<tab>/action/<action>', methods=['POST'])
@_endpoint(admin=True)
def downloads_settings_action(tab, action):
    values = settings_values(tab, request.get_json(silent=True) or {})
    if not action.replace('_', '').isalnum() or len(action) > 64:
        raise DownloaderError('invalid_request')
    def run():
        engine = _engine()
        return engine.engine('core.settings_registry').execute_action(tab, action, values)
    return jsonify(_run_private_blocking(run))


@api_v1.route('/admin/downloads/queue', methods=['GET'])
@_endpoint(admin=True)
def downloads_queue():
    """What the engine is working on, for an administrator watching a slow download."""
    def read():
        engine = _engine()
        status = engine.engine('download.orchestrator').queue_status()
        return [{'id': task_id, 'status': state, 'title': task.get('title'), 'source': task.get('source_display_name'),
                 'progress': task.get('progress'), 'message': task.get('status_message'),
                 'added': task.get('added_time')}
                for state, tasks in status.items() for task_id, task in tasks.items()]
    return jsonify({'tasks': _run_private_blocking(read)})
