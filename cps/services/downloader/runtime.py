# SPDX-License-Identifier: GPL-3.0-or-later
"""The download engine inside this process: configuration, start-up and a small API.

The engine (vendored from Shelfmark in ``engine/``) keeps its own settings, queue
and worker threads. CWNG decides where its files go and how they leave it: every
finished download lands in a private folder under ``/config/downloads`` and is
then copied into acquisition staging and imported through ingest, credited to
the account that asked. Nothing here is reachable from outside the process.
"""
import os
import shutil
import threading
from pathlib import Path

from ..acquisition.http import TransportError

_lock = threading.Lock()
_configured = None
_started = False


def config_dir():
    """``/config/downloads`` beside CWNG's own app.db, unless set explicitly."""
    explicit = os.environ.get('CWNG_DOWNLOADS_CONFIG_DIR')
    if explicit:
        return Path(explicit)
    from ... import ub
    base = Path(ub.app_DB_path).parent if getattr(ub, 'app_DB_path', None) else Path('/config')
    return base / 'downloads'


def configure():
    """Point the engine at CWNG's paths before any engine module is imported.

    Values set through the environment are authoritative in the engine and are
    shown locked in its settings, which is the intent: where files go and how they
    are handed over is CWNG's decision, not a setting.
    """
    global _configured
    with _lock:
        if _configured is not None:
            return _configured
        root = config_dir()
        completed = root / 'completed'
        for path in (root, root / 'tmp', completed / 'books', completed / 'audiobooks'):
            path.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('CWNG_DOWNLOADS_CONFIG_DIR', str(root))
        fixed = {
            'BOOKS_OUTPUT_MODE': 'folder',
            # The engine reads DESTINATION from its legacy variable name.
            'DESTINATION': str(completed / 'books'),
            'INGEST_DIR': str(completed / 'books'),
            'DESTINATION_AUDIOBOOK': str(completed / 'audiobooks'),
            'AA_CONTENT_TYPE_ROUTING': 'false',
            'USE_CONTENT_TYPE_DIRECTORIES': 'false',
            'ONBOARDING': 'false',
            # Run the Cloudflare bypass in its own helper process, away from CWNG's.
            'DOCKERMODE': 'true',
        }
        for key, value in fixed.items():
            os.environ[key] = value
        _configured = root
        return root


def engine(module):
    """Import an engine module after configuring it, e.g. ``engine('download.orchestrator')``."""
    configure()
    import importlib
    return importlib.import_module('cps.services.downloader.engine.' + module)


def start():
    """Start the engine's download workers once; later calls are no-ops."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    orchestrator = engine('download.orchestrator')
    load_settings()
    orchestrator.start()


def load_settings():
    """Register every engine settings tab (download clients register on import)."""
    engine('config.settings')
    engine('download.clients.settings')
    engine('release_sources')._ensure_builtin_sources_registered()


class LocalEngine:
    """The in-process engine, behind the small API the acquisition worker uses.

    Download tasks are named by the release's own id and the queue refuses a
    release it already holds, so a resubmission after an interruption cannot
    start a second download.
    """

    def __init__(self, config=None):
        self.config = config or {}

    # -- search -----------------------------------------------------------------

    def sources(self, content_type='ebook'):
        return [source['name'] for source in engine('release_sources').list_available_sources()
                if source.get('enabled') and content_type in (source.get('supported_content_types') or ())]

    def releases(self, book, *, content_type='ebook'):
        """Search every enabled source for one book, sharing one time budget.

        ``book`` is the stored search result (title, authors, ISBNs, year, series).
        """
        book = book_metadata(book)
        search = engine('core.release_search')
        deadline = engine('core.search_deadline')
        found, errors = [], []
        names = self.sources(content_type)
        with deadline.search_deadline():
            for name in names:
                if deadline.expired():
                    errors.append(f'{name}: {deadline.deadline_message()}')
                    continue
                source, releases, error = search.search_source_releases(
                    name, book, content_type=content_type)
                found.extend(releases)
                if error:
                    errors.append(error)
        return found, errors, names

    # -- downloads --------------------------------------------------------------

    def queue(self, release):
        start()
        orchestrator = engine('download.orchestrator')
        ok, error = orchestrator.queue_release(release, 0, user_id=None, username=None)
        if not ok and 'already in the download queue' not in (error or ''):
            raise EngineError('download_failed', error)
        return True

    def task(self, task_id):
        """(status, details) for one queued release, or None when the engine has no record.

        Reading needs no running workers: after a restart the queue is empty, and
        the caller queues the release again, which starts them.
        """
        queue = engine('core.queue').book_queue
        status = queue.get_task_status(task_id)
        task = queue.get_task(task_id)
        if status is None or task is None:
            return None
        return str(getattr(status, 'value', status)), {
            'added_time': task.added_time, 'progress': task.progress,
            'status_message': task.status_message, 'download_path': task.download_path}

    def download(self, task_id, destination, *, max_bytes, checkpoint=lambda: None):
        """Copy a finished download into a new private path; return its sha256."""
        import hashlib
        source = self._payload(task_id)
        if source.stat().st_size > max_bytes:
            raise EngineError('file_too_large')
        digest = hashlib.sha256()
        try:
            with source.open('rb') as incoming, destination.open('xb') as outgoing:
                os.chmod(destination, 0o600)
                for chunk in iter(lambda: incoming.read(256 * 1024), b''):
                    checkpoint()
                    outgoing.write(chunk)
                    digest.update(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        if not destination.stat().st_size:
            destination.unlink(missing_ok=True)
            raise EngineError('empty_response')
        return digest.hexdigest()

    def _payload(self, task_id):
        """The one file a finished download produced.

        An audiobook torrent can finish as a folder. CWNG imports one audio file per
        book, so a folder must hold exactly one m4b/m4a/mp4; a pack of chapters
        cannot be imported as a single book and is refused rather than guessed.
        """
        orchestrator = engine('download.orchestrator')
        _path, task = orchestrator.get_book_path(task_id)
        path = _path or (task.download_path if task is not None else None)
        if not path or not Path(path).exists():
            raise EngineError('artifact_unavailable')
        path = Path(path)
        if path.is_file():
            return path
        audio = [p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in ('.m4b', '.m4a', '.mp4')]
        if len(audio) != 1:
            raise EngineError('audiobook_needs_single_file')
        return audio[0]

    def discard(self, task_id):
        """CWNG has its own copy: the engine drops the file and forgets the task."""
        engine('download.orchestrator').discard_delivered_file(task_id)

    def cancel(self, task_id):
        engine('download.orchestrator').cancel_download(task_id)


def book_metadata(book):
    """The engine's description of one book, from a stored search result."""
    BookMetadata = engine('metadata_providers').BookMetadata
    authors = [a for a in book.get('authors') or [] if isinstance(a, str)]
    return BookMetadata(provider=book.get('provider') or 'cwng', provider_id=str(book.get('provider_id') or ''),
        title=book.get('title') or '', authors=authors, isbn_10=book.get('isbn_10'), isbn_13=book.get('isbn_13'),
        language=book.get('language'), publish_year=book.get('publish_year'),
        series_name=book.get('series_name'), series_position=book.get('series_position'),
        search_title=book.get('title'), search_author=authors[0] if authors else None)


class EngineError(TransportError):
    """A public-safe failure code, with the engine's own explanation when it gave one."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.detail = detail


def disk_usage():
    root = config_dir()
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return None
