# SPDX-License-Identifier: GPL-3.0-or-later
"""The vendored download engine inside CWNG: configuration, the in-process API, settings.

Runs the real engine modules offline. Its queue is driven directly, so nothing is
downloaded; the network-facing sources have their own upstream test suite.
"""
import os
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(tempfile.mkdtemp(prefix='cwng-downloads-'))
os.environ.setdefault('CWNG_DOWNLOADS_CONFIG_DIR', str(_ROOT))

from cps.services.acquisition.downloader import SETTINGS_FIELDS, visible_settings  # noqa: E402
from cps.services.downloader import runtime  # noqa: E402


@pytest.fixture(scope='module')
def engine():
    runtime.configure()
    runtime.load_settings()
    return runtime


def complete(engine, task_id, path):
    models = engine.engine('core.models')
    queue = engine.engine('core.queue').book_queue
    task = models.DownloadTask(task_id=task_id, source='direct_download', title='Dune', download_path=str(path))
    assert queue.add(task) is True
    queue.update_status(task_id, models.QueueStatus.COMPLETE)
    return queue


def test_engine_paths_and_output_are_fixed_by_cwng(engine):
    env = engine.engine('config.env')
    root = Path(os.environ['CWNG_DOWNLOADS_CONFIG_DIR'])
    assert env.CONFIG_DIR == root and env.TMP_DIR == root / 'tmp' and env.ENABLE_LOGGING is False
    config = engine.engine('core.config').config
    assert config.get('BOOKS_OUTPUT_MODE') == 'folder'
    assert config.get('DESTINATION') == str(root / 'completed' / 'books')


def test_only_the_offered_sources_are_registered(engine):
    names = {source['name'] for source in engine.engine('release_sources').list_available_sources()}
    assert names == {'direct_download', 'libgen', 'audiobookbay'}


def test_every_offered_setting_still_exists(engine):
    """Guards a future sync: a renamed upstream key would silently vanish from the page."""
    document = engine.engine('core.settings_registry').serialize_all_settings(include_values=True)
    tabs = {tab['name']: {f.get('key') for f in tab['fields']} for tab in document['tabs']}
    for name, keys in SETTINGS_FIELDS.items():
        assert name in tabs, name
        assert keys is None or keys <= tabs[name], (name, keys - tabs[name])
    shown = visible_settings(document)
    assert {tab['name'] for tab in shown['tabs']} == set(SETTINGS_FIELDS)
    assert 'BOOKS_OUTPUT_MODE' not in str(shown)


def test_book_metadata_carries_isbn_and_authors(engine):
    book = runtime.book_metadata({'provider': 'openlibrary', 'provider_id': 'OL1W', 'title': 'Dune',
                                  'authors': ['Frank Herbert'], 'isbn_13': '9780441013593', 'publish_year': 1965})
    assert (book.title, book.authors, book.isbn_13, book.search_author) == (
        'Dune', ['Frank Herbert'], '9780441013593', 'Frank Herbert')


def test_finished_file_is_copied_then_discarded_and_forgotten(engine, tmp_path):
    local = runtime.LocalEngine()
    payload = tmp_path / 'dune.epub'
    payload.write_bytes(b'book bytes')
    queue = complete(engine, 'task-file', payload)
    status, details = local.task('task-file')
    assert status == 'complete' and details['download_path'] == str(payload)
    target = tmp_path / 'copy.part'
    digest = local.download('task-file', target, max_bytes=1024)
    assert target.read_bytes() == b'book bytes' and len(digest) == 64
    with pytest.raises(runtime.EngineError) as raised:
        local.download('task-file', tmp_path / 'small.part', max_bytes=3)
    assert raised.value.code == 'file_too_large' and not (tmp_path / 'small.part').exists()
    local.discard('task-file')
    assert not payload.exists() and local.task('task-file') is None and queue.get_task('task-file') is None


def test_an_audiobook_folder_yields_its_single_audio_file(engine, tmp_path):
    folder = tmp_path / 'audiobook'
    (folder / 'cover').mkdir(parents=True)
    (folder / 'cover' / 'cover.jpg').write_bytes(b'jpeg')
    (folder / 'Book.m4b').write_bytes(b'\x00\x00\x00\x20ftypM4B ')
    complete(engine, 'task-folder', folder)
    local = runtime.LocalEngine()
    target = tmp_path / 'audio.part'
    local.download('task-folder', target, max_bytes=1024)
    assert target.read_bytes().startswith(b'\x00\x00\x00\x20ftyp')
    local.discard('task-folder')
    assert not folder.exists()


def test_a_folder_of_chapters_is_refused(engine, tmp_path):
    folder = tmp_path / 'chapters'
    folder.mkdir()
    for n in (1, 2):
        (folder / f'part{n}.m4a').write_bytes(b'audio')
    complete(engine, 'task-chapters', folder)
    with pytest.raises(runtime.EngineError) as raised:
        runtime.LocalEngine().download('task-chapters', tmp_path / 'x.part', max_bytes=1024)
    assert raised.value.code == 'audiobook_needs_single_file'


def test_a_missing_task_is_reported_as_unavailable(engine, tmp_path):
    local = runtime.LocalEngine()
    assert local.task('never-queued') is None
    with pytest.raises(runtime.EngineError) as raised:
        local.download('never-queued', tmp_path / 'x.part', max_bytes=10)
    assert raised.value.code == 'artifact_unavailable'


def test_loading_the_engine_keeps_cwngs_logger_class(engine):
    import logging
    from cps import logger as cwng_logger
    cwng_logger.create()  # registers CWNG's class
    engine.engine('core.logger').setup_logger('cwng.test.engine')
    assert logging.getLoggerClass() is cwng_logger._Logger


def test_every_engine_import_resolves_to_a_vendored_module():
    """Lazy imports inside functions included: a module the sync left out would
    otherwise only fail when that code path first runs, mid-download."""
    import ast
    root = Path(runtime.__file__).parent / 'engine'
    package = 'cps.services.downloader.engine'

    def exists(module):
        parts = module.split('.')
        if parts[:4] != package.split('.'):
            return True  # not an engine module
        target = root.joinpath(*parts[4:])
        return target.with_suffix('.py').exists() or (target / '__init__.py').exists()

    missing = []
    for path in root.rglob('*.py'):
        base = '.'.join([package] + list(path.relative_to(root).parent.parts))
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = base.rsplit('.', node.level - 1)[0] if node.level > 1 else base
                    module = prefix + ('.' + node.module if node.module else '')
                else:
                    module = node.module or ''
                if not exists(module):
                    missing.append(f'{path.name}: {module}')
                elif not node.module:
                    for alias in node.names:
                        candidate = module + '.' + alias.name
                        target = root.joinpath(*candidate.split('.')[4:])
                        init = root.joinpath(*module.split('.')[4:]) / '__init__.py'
                        defined = init.exists() and any(
                            isinstance(item, (ast.FunctionDef, ast.ClassDef)) and item.name == alias.name
                            or isinstance(item, ast.Assign) and any(getattr(t, 'id', None) == alias.name for t in item.targets)
                            for item in ast.parse(init.read_text(encoding='utf-8')).body)
                        if not (target.with_suffix('.py').exists() or (target / '__init__.py').exists() or defined):
                            missing.append(f'{path.name}: {candidate}')
            elif isinstance(node, ast.Import):
                missing.extend(f'{path.name}: {a.name}' for a in node.names if not exists(a.name))
    assert missing == []
