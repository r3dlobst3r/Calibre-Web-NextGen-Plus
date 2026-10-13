#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Vendor Shelfmark's download engine into cps/services/downloader/engine.

Calibre-Web NextGen+ downloads books with Shelfmark's engine (MIT licensed,
https://github.com/calibrain/shelfmark) running *inside* CWNG: its Anna's Archive
and LibGen sources, AudiobookBay, the download clients (including debrid
services), the Cloudflare bypass and the network layer. Shelfmark's web app,
accounts, requests and notifications are not part of it; CWNG provides those.

This script is how the copy is made and refreshed, so an upstream fix can be
taken by re-running it against a newer Shelfmark checkout:

    python scripts/sync_shelfmark_engine.py /path/to/shelfmark

It copies the module set below, rewrites ``shelfmark.`` imports to the vendored
package, converts Python 3.14-only syntax for CWNG's interpreter, swaps the
modules CWNG leaves out for small stand-ins, and applies a few asserted edits.
Every edit is listed here; the vendored files are never edited by hand.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / 'cps' / 'services' / 'downloader' / 'engine'
PACKAGE = 'cps.services.downloader.engine'

# The transitive import closure of the engine entry points (orchestrator, release
# search, the three sources, every download client, both bypassers and the settings), minus
# the modules replaced by STUBS. Recompute when syncing a newer Shelfmark.
MODULES = '''
bypass bypass.challenge bypass.cookie_store bypass.external_bypasser bypass.fingerprint
bypass.internal_bypasser bypass.waiting_room
config config.download_settings_handlers config.env config.migrations config.settings
core core.cache core.config core.languages core.logger core.mirrors core.models core.naming
core.path_mappings core.queue core.release_search core.request_helpers core.request_policy core.search_deadline
core.search_plan core.settings_registry core.utils
download download.activity download.archive download.doh_wireformat download.fs download.http
download.network download.orchestrator download.permissions_debug download.staging
download.outputs download.outputs.folder
download.postprocess download.postprocess.custom_script download.postprocess.destination
download.postprocess.packs download.postprocess.pipeline download.postprocess.policy
download.postprocess.prepare download.postprocess.router download.postprocess.scan
download.postprocess.steps download.postprocess.transfer download.postprocess.types
download.postprocess.workspace
download.clients download.clients._coercion download.clients.alldebrid
download.clients.base_handler download.clients.blackhole download.clients.debridlink
download.clients.deluge download.clients.nzbget download.clients.qbittorrent
download.clients.realdebrid download.clients.rtorrent download.clients.sabnzbd
download.clients.settings download.clients.torbox download.clients.torrent_utils
download.clients.transmission
metadata_providers
release_sources
release_sources.audiobookbay release_sources.audiobookbay.handler
release_sources.audiobookbay.scraper release_sources.audiobookbay.settings
release_sources.audiobookbay.source release_sources.audiobookbay.utils
release_sources.direct_download release_sources.direct_download.annas_archive
release_sources.direct_download.common release_sources.direct_download.handler
release_sources.direct_download.registry release_sources.direct_download.source
release_sources.libgen release_sources.libgen.handler release_sources.libgen.scraper
release_sources.libgen.settings release_sources.libgen.source
'''.split()

# Modules the engine imports that belong to Shelfmark's web app. Each stand-in
# keeps the engine's existing fallback path (they already tolerate the module
# being unavailable, or only use it for a feature CWNG does not expose).
STUBS = {
    'api/__init__.py': '',
    'api/websocket.py': (
        '"""Shelfmark pushes queue updates to its own UI; CWNG polls instead."""\n\n'
        'ws_manager = None\n'),
    'core/user_db.py': (
        '"""Shelfmark accounts are not part of CWNG; per-user download settings fall back."""\n\n\n'
        'class UserDB:\n'
        '    def __init__(self, *args, **kwargs):\n'
        '        raise ImportError("Shelfmark user accounts are not used in Calibre-Web NextGen+")\n'),
    'core/image_cache.py': (
        '"""Shelfmark\'s cover cache serves its own UI; CWNG has none to clear."""\n\n\n'
        'def get_image_cache():\n'
        '    return None\n\n\n'
        'def reset_image_cache():\n'
        '    return None\n'),
    'config/security.py': '"""Sign-in is CWNG\'s; nothing to register."""\n',
    'config/users_settings.py': '"""Accounts are CWNG\'s; nothing to register."""\n',
    'config/notifications_settings.py': '"""Notifications are CWNG\'s; nothing to register."""\n',
    'config/booklore_settings.py': (
        '"""CWNG always imports through its own ingest; other outputs are not offered."""\n\n\n'
        'def check_booklore_connection(*args, **kwargs):\n'
        '    return {"success": False, "message": "Not available in Calibre-Web NextGen+"}\n\n\n'
        'def get_booklore_library_options(*args, **kwargs):\n'
        '    return []\n\n\n'
        'def get_booklore_path_options(*args, **kwargs):\n'
        '    return []\n'),
    'config/email_settings.py': (
        '"""CWNG always imports through its own ingest; other outputs are not offered."""\n\n\n'
        'def check_email_connection(*args, **kwargs):\n'
        '    return {"success": False, "message": "Not available in Calibre-Web NextGen+"}\n'),
}

# (relative path, old, new): exact edits, each asserted to apply exactly once.
EDITS = [
    # Paths: a private corner of CWNG's /config, never Shelfmark's defaults.
    ('config/env.py', 'config_dir = Path(os.getenv("CONFIG_DIR", "/config"))',
     'config_dir = Path(os.getenv("CWNG_DOWNLOADS_CONFIG_DIR", "/config/downloads"))'),
    ('config/env.py', 'CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))',
     'CONFIG_DIR = Path(os.getenv("CWNG_DOWNLOADS_CONFIG_DIR", "/config/downloads"))'),
    ('config/env.py', 'LOG_ROOT = Path(os.getenv("LOG_ROOT", "/var/log/"))',
     'LOG_ROOT = Path(os.getenv("CWNG_DOWNLOADS_LOG_ROOT", str(CONFIG_DIR / "log")))'),
    ('config/env.py',
     'TMP_DIR = Path(os.getenv("TMP_DIR", (Path(tempfile.gettempdir()) / "shelfmark").as_posix()))',
     'TMP_DIR = Path(os.getenv("CWNG_DOWNLOADS_TMP_DIR", str(CONFIG_DIR / "tmp")))'),
    ('config/env.py', 'INGEST_DIR = Path(os.getenv("INGEST_DIR", "/books"))',
     'INGEST_DIR = Path(os.getenv("CWNG_DOWNLOADS_COMPLETED_DIR", str(CONFIG_DIR / "completed")))'),
    # Logs go to the container log with CWNG's; a second log file is opt-in.
    ('config/env.py', 'ENABLE_LOGGING = string_to_bool(os.getenv("ENABLE_LOGGING", "true"))',
     'ENABLE_LOGGING = string_to_bool(os.getenv("CWNG_DOWNLOADS_FILE_LOG", "false"))'),
    # Shelfmark keeps its data folder beside the package; the copy keeps it inside.
    ('core/languages.py',
     'LANGUAGE_DATA_PATH = Path(__file__).resolve().parents[1].parent / "data" / "book-languages.json"',
     'LANGUAGE_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "book-languages.json"'),
    # Only the sources Calibre-Web NextGen+ offers. CWNG has its own Newznab,
    # Torznab and Prowlarr support; IRC is not offered.
    ('release_sources/__init__.py', '''    "shelfmark.release_sources.audiobookbay",
    "shelfmark.release_sources.direct_download",
    "shelfmark.release_sources.irc",
    "shelfmark.release_sources.libgen",
    "shelfmark.release_sources.newznab",
    "shelfmark.release_sources.prowlarr",
''', '''    "shelfmark.release_sources.audiobookbay",
    "shelfmark.release_sources.direct_download",
    "shelfmark.release_sources.libgen",
'''),
]

HEADER = ('# Vendored from Shelfmark (MIT License, Copyright (c) 2024 CaliBrain), see LICENSE in\n'
          '# this directory. Generated by scripts/sync_shelfmark_engine.py; do not edit by hand.\n')

# `except A, B:` (PEP 758, Python 3.14) -> `except (A, B):` for Python 3.13.
EXCEPT_TUPLE = re.compile(r'^(\s*)except ((?:[A-Za-z_][\w.]*)(?:\s*,\s*[A-Za-z_][\w.]*)+)(\s+as\s+\w+)?\s*:', re.M)


def module_file(source_root, module):
    path = source_root.joinpath(*module.split('.'))
    if (path / '__init__.py').is_file():
        return path / '__init__.py'
    if path.with_suffix('.py').is_file():
        return path.with_suffix('.py')
    raise SystemExit(f'Module missing from the Shelfmark checkout: {module}')


def future_annotations(text):
    """Python 3.14 evaluates annotations lazily (PEP 649); 3.13 needs the opt-in.

    Shelfmark annotates with names it only imports for type checking, which 3.13
    would evaluate at import time. The future import is placed after the module
    docstring, where the language requires it.
    """
    if re.search(r'^from __future__ import annotations', text, re.M):
        return text
    import ast
    tree = ast.parse(text)
    if not tree.body:
        return text
    first = tree.body[0]
    lines = text.splitlines(keepends=True)
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        at = first.end_lineno
    else:
        at = 0
        while at < len(lines) and (lines[at].startswith('#') or not lines[at].strip()):
            at += 1
    return ''.join(lines[:at]) + ('\n' if at else '') + 'from __future__ import annotations\n' + ('' if at else '\n') + ''.join(lines[at:])


def transform(text):
    text = re.sub(r'\bshelfmark\.(?=[a-z_])', PACKAGE + '.', text)
    text = re.sub(r'^(\s*)from shelfmark import ', r'\1from ' + PACKAGE + ' import ', text, flags=re.M)
    text = re.sub(r'^(\s*)import shelfmark\b', r'\1import ' + PACKAGE, text, flags=re.M)
    return EXCEPT_TUPLE.sub(lambda m: f'{m.group(1)}except ({m.group(2)}){m.group(3) or ""}:', text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('shelfmark', type=Path, help='path to a Shelfmark checkout')
    args = parser.parse_args()
    source_root = args.shelfmark.resolve() / 'shelfmark'
    if not (source_root / 'download' / 'orchestrator.py').is_file():
        raise SystemExit('Not a Shelfmark checkout: ' + str(args.shelfmark))

    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)
    copied = {}
    for module in MODULES:
        source = module_file(source_root, module)
        relative = source.relative_to(source_root)
        copied[relative.as_posix()] = future_annotations(transform(source.read_text(encoding='utf-8')))
    copied['__init__.py'] = '"""Shelfmark\'s download engine, running inside Calibre-Web NextGen+."""\n'
    for relative, body in STUBS.items():
        if relative in copied:
            raise SystemExit(f'{relative} is both copied and stubbed')
        copied[relative] = body
    for relative, old, new in EDITS:
        old, new = transform(old), transform(new)
        count = copied[relative].count(old)
        if count != 1:
            raise SystemExit(f'Edit for {relative} matched {count} times; update EDITS for this Shelfmark version')
        copied[relative] = copied[relative].replace(old, new)
    for relative, body in sorted(copied.items()):
        path = TARGET / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER + body, encoding='utf-8')
    for directory in TARGET.rglob('*'):
        if directory.is_dir() and directory.name != 'data' and not (directory / '__init__.py').exists():
            (directory / '__init__.py').write_text(HEADER, encoding='utf-8')
    (TARGET / 'data').mkdir()
    shutil.copyfile(args.shelfmark / 'data' / 'book-languages.json', TARGET / 'data' / 'book-languages.json')
    shutil.copyfile(args.shelfmark / 'LICENSE', TARGET / 'LICENSE')
    try:
        commit = subprocess.run(['git', '-C', str(args.shelfmark), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = 'unknown'
    (TARGET / 'UPSTREAM').write_text(
        f'https://github.com/calibrain/shelfmark\ncommit {commit}\n', encoding='utf-8')
    print(f'Vendored {len(MODULES)} modules and {len(STUBS)} stand-ins from Shelfmark {commit[:12]}')


if __name__ == '__main__':
    sys.exit(main())
