#!/usr/bin/env python3
"""
calibre_monitor.py
Watches folders for new ebook files and adds them to Calibre automatically.
Simplified Chinese EPUB / FB2 / TXT files are converted to Traditional before import.

Usage:
    python3 calibre_monitor.py
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import tempfile
import threading
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / 'monitor_config.json'

with open(CONFIG_PATH) as _f:
    CONFIG = json.load(_f)

CALIBREDB             = CONFIG.get('calibredb', '/Applications/calibre.app/Contents/MacOS/calibredb')
CALIBRE_LIB           = os.path.expanduser(CONFIG['calibre_library'])
CONTENT_SERVER_URL    = CONFIG.get('content_server_url', 'http://localhost:8080')
CONTENT_SERVER_USER   = CONFIG.get('content_server_username', '')
CONTENT_SERVER_PASS   = CONFIG.get('content_server_password', '')
S2T_VARIANT    = CONFIG.get('s2t_variant', 's2twp')
WATCH_FOLDERS  = [os.path.expanduser(p) for p in CONFIG['watch_folders']]
WATCH_EXTS     = {e.lower() for e in CONFIG.get('extensions', [
    '.epub', '.txt', '.pdf', '.mobi', '.fb2', '.djvu',
    '.azw3', '.rtf', '.chm', '.azw', '.acsm'
])}
S2T_EXTS       = {'.epub', '.fb2', '.txt', '.html', '.htm'}
DONE_FOLDER    = CONFIG.get('done_folder', '_imported')   # '' to disable moving

# Resolved real paths for subdirectory filtering
WATCH_FOLDERS_REAL = {os.path.realpath(f) for f in WATCH_FOLDERS}


# ── Logging ───────────────────────────────────────────────────────────────────

log_path = os.path.expanduser(CONFIG.get('log_file', '~/Library/Logs/calibre_monitor.log'))
os.makedirs(os.path.dirname(log_path), exist_ok=True)

log = logging.getLogger('calibre_monitor')
log.setLevel(logging.INFO)

_file_handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3,
                                     encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s'))
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s'))
log.addHandler(_file_handler)
log.addHandler(_console_handler)


# ── macOS notifications ───────────────────────────────────────────────────────

def notify(title, message):
    """Send a macOS notification. Silently skipped if osascript is unavailable."""
    safe_msg   = message.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    try:
        subprocess.run(
            ['osascript', '-e',
             f'display notification "{safe_msg}" with title "{safe_title}"'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


# ── Plugin source (for lang_detect + chinese_engine) ─────────────────────────

_plugin_src = os.path.expanduser(CONFIG.get('plugin_source', ''))
if _plugin_src and os.path.isdir(_plugin_src) and _plugin_src not in sys.path:
    sys.path.insert(0, _plugin_src)


# ── Language detection ────────────────────────────────────────────────────────

def is_simplified_chinese(path):
    """Return True if the file is detected as Simplified Chinese."""
    ext = Path(path).suffix.lower()
    if ext not in S2T_EXTS:
        return False
    try:
        if ext == '.epub':
            from lang_detect import detect_book_language, detect_script_from_epub
            info = detect_book_language(path)
            if not info.get('is_chinese'):
                return False
            if info.get('is_simplified'):
                return True
            if info.get('is_traditional'):
                return False
            return detect_script_from_epub(path) == 'simplified'
        else:
            from lang_detect import detect_script_from_text
            # Try common Chinese encodings in order — many mainland TXT files
            # are GBK/GB18030 rather than UTF-8.
            sample = None
            for enc in ('utf-8', 'gb18030', 'big5'):
                try:
                    with open(path, 'r', encoding=enc, errors='strict') as f:
                        sample = f.read(8000)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if sample is None:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    sample = f.read(8000)
            has_han  = any(0x4E00 <= ord(c) <= 0x9FFF for c in sample)
            has_kana = any(0x3040 <= ord(c) <= 0x30FF for c in sample)
            if not has_han or has_kana:
                return False
            return detect_script_from_text(sample) == 'simplified'
    except Exception as e:
        log.warning(f'Language detection failed for {Path(path).name}: {e}')
        return False


# ── S→T conversion ────────────────────────────────────────────────────────────

def convert_s2t(src_path):
    """
    Convert file to Traditional Chinese.
    Returns (tmp_dir, tmp_file_path) — caller must shutil.rmtree(tmp_dir).
    The output file has the SAME name as the source so Calibre uses the right title.
    """
    src = Path(src_path)
    tmp_dir = tempfile.mkdtemp()
    tmp = os.path.join(tmp_dir, src.name)   # preserve original filename
    try:
        from chinese_engine import (convert_epub_s2t, convert_fb2_s2t,
                                     convert_txt_s2t, convert_html_s2t)
        ext = src.suffix.lower()
        if ext == '.epub':
            convert_epub_s2t(src_path, tmp, variant=S2T_VARIANT)
        elif ext == '.fb2':
            convert_fb2_s2t(src_path, tmp, variant=S2T_VARIANT)
        elif ext == '.txt':
            convert_txt_s2t(src_path, tmp, variant=S2T_VARIANT)
        elif ext in ('.html', '.htm'):
            convert_html_s2t(src_path, tmp, variant=S2T_VARIANT)
        return tmp_dir, tmp
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ── calibredb ─────────────────────────────────────────────────────────────────

def calibredb_add(path):
    """
    Add a file to the Calibre library.
    Tries the content server first (works when Calibre GUI / server is running),
    then falls back to direct library access (works when Calibre is closed).
    calibredb skips duplicates by default (same title+author); use --duplicates
    to override that behaviour.  We detect the skip via parse_added_id().
    """
    for library in [CONTENT_SERVER_URL, CALIBRE_LIB]:
        cmd = [CALIBREDB, 'add', '--with-library', library]
        if library == CONTENT_SERVER_URL and CONTENT_SERVER_USER:
            cmd += ['--username', CONTENT_SERVER_USER,
                    '--password', CONTENT_SERVER_PASS]
        cmd.append(path)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            via = 'content server' if library == CONTENT_SERVER_URL else 'direct'
            log.debug(f'calibredb connected via {via}')
            return (result.stdout + result.stderr).strip()
        # If the error is "another calibre program is running", try next option
        combined = (result.stdout + result.stderr).lower()
        if 'another calibre program' in combined or 'cannot lock' in combined:
            continue
        # Any other error — raise immediately, no point retrying
        raise RuntimeError((result.stdout + result.stderr).strip())
    raise RuntimeError('Could not connect to Calibre — tried content server and direct access')


def parse_added_id(output):
    """Extract the book ID from calibredb add output, e.g. 'Added book ids: 42'."""
    m = re.search(r'Added book ids:\s*(\d+)', output, re.I)
    return int(m.group(1)) if m else None


def calibredb_set_metadata(book_id, **fields):
    """
    Set one or more metadata fields on an already-added book.
    fields: keyword args like title='新タイトル', authors='Author Name'
    """
    for library in [CONTENT_SERVER_URL, CALIBRE_LIB]:
        cmd = [CALIBREDB, 'set_metadata', '--with-library', library]
        if library == CONTENT_SERVER_URL and CONTENT_SERVER_USER:
            cmd += ['--username', CONTENT_SERVER_USER,
                    '--password', CONTENT_SERVER_PASS]
        for key, val in fields.items():
            cmd += ['--field', f'{key}:{val}']
        cmd.append(str(book_id))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return
        combined = (result.stdout + result.stderr).lower()
        if 'another calibre program' in combined or 'cannot lock' in combined:
            continue
        raise RuntimeError((result.stdout + result.stderr).strip())


def title_from_path(path):
    """
    Derive a clean title from a file path.
    Strips the extension and trims trailing noise like '_12345' or ' -- Anna\'s Archive'.
    """
    stem = Path(path).stem
    # Remove Anna's Archive hash suffixes: ' -- <hash> -- Anna's Archive'
    stem = re.sub(r'\s*--\s*[0-9a-f]{32}\s*--.*$', '', stem, flags=re.I)
    # Remove trailing underscore+digits (common in download site filenames)
    stem = re.sub(r'_\d+$', '', stem)
    return stem.strip()


def _read_epub_title(epub_path):
    """Extract dc:title from an EPUB's OPF metadata, or None on failure."""
    import zipfile as _zf
    try:
        with _zf.ZipFile(epub_path) as z:
            container = z.read('META-INF/container.xml').decode('utf-8', errors='ignore')
            m = re.search(r'full-path=["\']([^"\']+\.opf)["\']', container, re.I)
            if not m:
                return None
            opf = z.read(m.group(1)).decode('utf-8', errors='ignore')
            m2 = re.search(r'<dc:title[^>]*>([^<]+)', opf, re.I)
            return m2.group(1).strip() if m2 else None
    except Exception:
        return None


def get_search_title(src_path, add_path, was_converted):
    """
    Return the title string that Calibre will store for this file —
    used to pre-check for duplicates before adding.
      src_path     : original file in the watch folder
      add_path     : file that will actually be passed to calibredb
                     (may be a converted temp copy with the same name)
      was_converted: True if S→T conversion ran
    """
    ext = Path(src_path).suffix.lower()
    if ext == '.epub':
        # Calibre reads the title from the OPF; use the (possibly converted) file
        t = _read_epub_title(add_path)
        return t if t else title_from_path(src_path)
    if ext in ('.txt', '.fb2') and was_converted:
        # We post-process the Calibre title to Traditional Chinese
        try:
            from chinese_engine import convert_string_s2t
            return convert_string_s2t(title_from_path(src_path), variant=S2T_VARIANT)
        except Exception:
            pass
    return title_from_path(src_path)


def calibredb_title_exists(title):
    """
    Return True if Calibre already has a book with this exact title.
    Uses calibredb search so it works with both content server and direct access.
    """
    escaped = title.replace('"', '\\"')
    for library in [CONTENT_SERVER_URL, CALIBRE_LIB]:
        cmd = [CALIBREDB, 'search', '--with-library', library]
        if library == CONTENT_SERVER_URL and CONTENT_SERVER_USER:
            cmd += ['--username', CONTENT_SERVER_USER,
                    '--password', CONTENT_SERVER_PASS]
        cmd.append(f'title:"{escaped}"')
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return True   # found matching books
        combined = (result.stdout + result.stderr).lower()
        if 'another calibre program' in combined or 'cannot lock' in combined:
            continue
        return False      # not found (exit 1) or other error
    return False


# ── File stability ────────────────────────────────────────────────────────────

def wait_for_stable(path, timeout=120, interval=2.0):
    """
    Wait until the file size stops changing for two consecutive checks.
    Handles iCloud downloads that can take time to complete.
    Returns True if stable, False if timed out.
    """
    deadline  = time.time() + timeout
    last_size = -1
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
            if size > 0 and size == last_size:
                return True
            last_size = size
        except OSError:
            pass
        time.sleep(interval)
    return False


# ── Event handler ─────────────────────────────────────────────────────────────

class BookHandler(FileSystemEventHandler):

    def __init__(self):
        super().__init__()
        self._in_progress = set()   # canonical paths currently being processed
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self._process(event.src_path)

    def on_moved(self, event):
        # Catches files moved/renamed into the watched folder
        if not event.is_directory:
            self._process(event.dest_path)

    def _process(self, path):
        p = Path(path)

        # Skip hidden files and iCloud placeholder stubs (.filename.icloud)
        if p.name.startswith('.'):
            return

        ext = p.suffix.lower()
        if ext not in WATCH_EXTS:
            return

        # Skip files inside subdirectories of the watch folder (e.g. _imported/).
        # Moving a file triggers an on_moved event; this prevents re-processing it.
        if os.path.realpath(str(p.parent)) not in WATCH_FOLDERS_REAL:
            return

        # Deduplicate: iCloud often fires both created + moved for the same
        # file.  Use the canonical path as the key; skip if already running.
        canonical = str(p.resolve())
        with self._lock:
            if canonical in self._in_progress:
                return
            self._in_progress.add(canonical)

        log.info(f'Detected: {p.name}')

        tmp_dir = None
        try:
            # Wait for the file to finish downloading / writing
            if not wait_for_stable(path):
                log.error(f'Timed out waiting for file to finish: {p.name}')
                notify('Calibre Monitor ⚠️', f'Timeout — {p.name}')
                return

            # S→T conversion for eligible Simplified Chinese files
            if ext in S2T_EXTS and is_simplified_chinese(path):
                log.info(f'Simplified Chinese detected — converting ({S2T_VARIANT}): {p.name}')
                tmp_dir, add_path = convert_s2t(path)
                action = f'Added + converted S→T ({S2T_VARIANT})'
            else:
                add_path = path
                action   = 'Added'

            # Pre-check: search the library by title before adding.
            # calibredb add via the content server does not check for duplicates,
            # so we do it ourselves to avoid double entries.
            search_title = get_search_title(path, add_path, tmp_dir is not None)
            if calibredb_title_exists(search_title):
                log.info(f'Duplicate skipped (already in library): {p.name}')
                notify('Calibre Monitor ℹ️', f'Duplicate — {p.name}')
                return

            output  = calibredb_add(add_path)
            book_id = parse_added_id(output)
            log.debug(f'calibredb output: {output}')

            log.info(f'{action}: {p.name}')

            # CHM files store their title in GBK internally; Calibre misreads it.
            # Override with the (always-UTF-8) filename so the title is readable.
            if ext == '.chm':
                clean = title_from_path(path)
                try:
                    calibredb_set_metadata(book_id, title=clean)
                    log.info(f'Fixed CHM title → "{clean}"')
                except Exception as me:
                    log.warning(f'Could not fix CHM title: {me}')

            # TXT / FB2 have no embedded metadata — Calibre uses the filename as
            # the title, which stays in Simplified after S→T conversion.
            # Convert it to Traditional so the library entry matches the content.
            if ext in ('.txt', '.fb2') and tmp_dir is not None:
                try:
                    from chinese_engine import convert_string_s2t
                    simp_title = title_from_path(path)
                    trad_title = convert_string_s2t(simp_title, variant=S2T_VARIANT)
                    if trad_title != simp_title:
                        calibredb_set_metadata(book_id, title=trad_title)
                        log.info(f'Converted TXT/FB2 title S→T: "{simp_title}" → "{trad_title}"')
                except Exception as me:
                    log.warning(f'Could not convert TXT/FB2 title: {me}')

            # Move the source file into the done subfolder on success.
            if DONE_FOLDER:
                done_dir = p.parent / DONE_FOLDER
                done_dir.mkdir(exist_ok=True)
                dest = done_dir / p.name
                if dest.exists():
                    dest = done_dir / f'{p.stem}_{int(time.time())}{p.suffix}'
                shutil.move(path, dest)
                log.info(f'Moved to {DONE_FOLDER}/: {p.name}')

            notify('Calibre Monitor ✅', f'{action}: {p.name}')

        except Exception as e:
            log.error(f'Failed to import {p.name}: {e}')
            notify('Calibre Monitor ⚠️', f'Error — {p.name}: {e}')

        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            with self._lock:
                self._in_progress.discard(canonical)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    observer = Observer()
    handler  = BookHandler()
    watched  = 0

    for folder in WATCH_FOLDERS:
        if not os.path.isdir(folder):
            log.warning(f'Watch folder not found, skipping: {folder}')
            continue
        observer.schedule(handler, folder, recursive=False)
        log.info(f'Watching: {folder}')
        watched += 1

    if watched == 0:
        log.error('No valid watch folders found. Check monitor_config.json.')
        sys.exit(1)

    log.info(f'Calibre Monitor started  |  library: {CALIBRE_LIB}')
    log.info(f'Extensions: {", ".join(sorted(WATCH_EXTS))}')
    notify('Calibre Monitor', f'Started — watching {watched} folder(s)')

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info('Stopping…')
    finally:
        observer.stop()
        observer.join()
        log.info('Calibre Monitor stopped.')


if __name__ == '__main__':
    main()
