#!/usr/bin/env python3
"""
calibre_monitor.py
Watches folders for new ebook files and adds them to Calibre automatically.
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


def _load_plugin_prefs():
    """
    Read the Calibre plugin's JSONConfig file directly.
    This is the source of truth for behavior settings set via the plugin UI.
    """
    if sys.platform == 'darwin':
        p = Path.home() / 'Library/Preferences/calibre/plugins/furigana_ruby.json'
    elif sys.platform.startswith('linux'):
        p = Path.home() / '.config/calibre/plugins/furigana_ruby.json'
    else:
        p = Path.home() / 'AppData/Roaming/calibre/plugins/furigana_ruby.json'
    try:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _pref(plugin_prefs, *keys, default):
    """
    Look up a setting, trying multiple key names in order.
    Checks plugin_prefs first, then monitor_config.json CONFIG, then returns default.
    Multiple keys let us handle old and new naming conventions.
    """
    for k in keys:
        if k in plugin_prefs:
            return plugin_prefs[k]
    for k in keys:
        if k in CONFIG:
            return CONFIG[k]
    return default


_PP = _load_plugin_prefs()

CALIBREDB          = CONFIG.get('calibredb', '/Applications/calibre.app/Contents/MacOS/calibredb')
CALIBRE_LIB        = os.path.expanduser(CONFIG['calibre_library'])
CONTENT_SERVER_URL  = CONFIG.get('content_server_url', 'http://localhost:8080')
CONTENT_SERVER_USER = CONFIG.get('content_server_username', '')
CONTENT_SERVER_PASS = CONFIG.get('content_server_password', '')
WATCH_FOLDERS       = [os.path.expanduser(p) for p in
                       _pref(_PP, 'watch_folders', default=CONFIG['watch_folders'])]
WATCH_EXTS          = {e.lower() for e in CONFIG.get('extensions', [
    '.epub', '.txt', '.pdf', '.mobi', '.fb2', '.djvu',
    '.azw3', '.rtf', '.chm', '.azw', '.acsm',
])}
DONE_FOLDER         = _pref(_PP, 'done_folder', default=CONFIG.get('done_folder', '_imported'))

# Behaviour settings — plugin JSONConfig takes precedence over monitor_config.json.
# Old key names (auto_s2t_*) are checked alongside new names for backward compat.
KEEP_ORIGINAL        = _pref(_PP, 'keep_original',                          default=False)
AUTO_CHINESE_ENABLED = _pref(_PP, 'auto_chinese_enabled', 'auto_s2t_enabled', default=True)
AUTO_CHINESE_DIR     = _pref(_PP, 'auto_chinese_direction','auto_s2t_direction', default='s2t')
S2T_VARIANT          = _pref(_PP, 's2t_variant', 'auto_s2t_variant',
                              default=CONFIG.get('s2t_variant', 's2twp'))
T2S_VARIANT          = 't2s'   # T→S always produces standard Mainland Simplified
AUTO_RUBY_ENABLED    = _pref(_PP, 'auto_ruby_enabled',                       default=False)
AUTO_RUBY_LEVELS     = set(_pref(_PP, 'auto_ruby_levels', default=['N1', 'N2', 'N3']))

CHINESE_EXTS = {'.epub', '.fb2', '.txt', '.html', '.htm'}

# Resolved real paths for subdirectory filtering
WATCH_FOLDERS_REAL = {os.path.realpath(f) for f in WATCH_FOLDERS}


# ── Logging ───────────────────────────────────────────────────────────────────

log_path = os.path.expanduser(CONFIG.get('log_file', '~/Library/Logs/calibre_monitor.log'))
os.makedirs(os.path.dirname(log_path), exist_ok=True)

log = logging.getLogger('calibre_monitor')
log.setLevel(logging.INFO)

_fh = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
_fh.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s'))
_ch = logging.StreamHandler()
_ch.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s'))
log.addHandler(_fh)
log.addHandler(_ch)


# ── macOS notifications ───────────────────────────────────────────────────────

def notify(title, message):
    safe_msg   = message.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    try:
        subprocess.run(
            ['osascript', '-e',
             f'display notification "{safe_msg}" with title "{safe_title}"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


# ── Plugin source (lang_detect, chinese_engine, furigana_engine) ──────────────

_plugin_src = os.path.expanduser(CONFIG.get('plugin_source', ''))
if _plugin_src and os.path.isdir(_plugin_src) and _plugin_src not in sys.path:
    sys.path.insert(0, _plugin_src)


# ── Language detection ────────────────────────────────────────────────────────

def _read_text_sample(path):
    """Return up to 8 000 chars from a text file, trying common CJK encodings."""
    for enc in ('utf-8', 'gb18030', 'big5'):
        try:
            with open(path, 'r', encoding=enc, errors='strict') as f:
                return f.read(8000)
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        return f.read(8000)


def detect_chinese_script(path):
    """
    Return 'simplified', 'traditional', or None.
    Returns None if the file is not Chinese or detection fails.
    """
    ext = Path(path).suffix.lower()
    if ext not in CHINESE_EXTS:
        return None
    try:
        if ext == '.epub':
            from lang_detect import detect_book_language, detect_script_from_epub
            info = detect_book_language(path)
            if not info.get('is_chinese'):
                return None
            if info.get('is_simplified'):
                return 'simplified'
            if info.get('is_traditional'):
                return 'traditional'
            return detect_script_from_epub(path)   # 'simplified' | 'traditional' | None
        else:
            from lang_detect import detect_script_from_text
            sample = _read_text_sample(path)
            has_han  = any(0x4E00 <= ord(c) <= 0x9FFF for c in sample)
            has_kana = any(0x3040 <= ord(c) <= 0x30FF for c in sample)
            if not has_han or has_kana:
                return None
            return detect_script_from_text(sample)  # 'simplified' | 'traditional' | None
    except Exception as e:
        log.warning(f'Chinese script detection failed for {Path(path).name}: {e}')
        return None


# ── Chinese conversion ────────────────────────────────────────────────────────

def convert_chinese(src_path, variant):
    """
    Convert file between Simplified and Traditional Chinese using the given variant.
    Works for both S→T and T→S variants.
    Returns (tmp_dir, tmp_file_path) — caller must shutil.rmtree(tmp_dir).
    """
    src = Path(src_path)
    tmp_dir = tempfile.mkdtemp()
    tmp = os.path.join(tmp_dir, src.name)
    try:
        from chinese_engine import (convert_epub_s2t, convert_fb2_s2t,
                                    convert_txt_s2t, convert_html_s2t)
        ext = src.suffix.lower()
        if ext == '.epub':
            convert_epub_s2t(src_path, tmp, variant=variant)
        elif ext == '.fb2':
            convert_fb2_s2t(src_path, tmp, variant=variant)
        elif ext == '.txt':
            convert_txt_s2t(src_path, tmp, variant=variant)
        elif ext in ('.html', '.htm'):
            convert_html_s2t(src_path, tmp, variant=variant)
        return tmp_dir, tmp
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ── Ruby annotation ───────────────────────────────────────────────────────────

def add_ruby_to_epub(src_path, levels):
    """
    Add furigana to a Japanese EPUB.
    Returns (tmp_dir, tmp_path) on success, (None, None) if skipped.
    Caller must shutil.rmtree(tmp_dir) when done.
    Raises on hard failure (pykakasi unavailable, not a Japanese EPUB).
    """
    try:
        from deps_loader import ensure_deps
        if not ensure_deps():
            raise RuntimeError('pykakasi not available — check plugin_source in config')
        from lang_detect import detect_book_language
        info = detect_book_language(src_path)
        if not info.get('is_japanese'):
            return None, None
    except Exception as e:
        raise RuntimeError(f'Ruby pre-check failed: {e}')

    src = Path(src_path)
    tmp_dir = tempfile.mkdtemp()
    tmp = os.path.join(tmp_dir, src.name)
    try:
        from furigana_engine import process_epub_file
        process_epub_file(src_path, tmp, mode='add', annotate_levels=list(levels))
        return tmp_dir, tmp
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ── calibredb helpers ─────────────────────────────────────────────────────────

def calibredb_add(path):
    for library in [CONTENT_SERVER_URL, CALIBRE_LIB]:
        cmd = [CALIBREDB, 'add', '--with-library', library]
        if library == CONTENT_SERVER_URL and CONTENT_SERVER_USER:
            cmd += ['--username', CONTENT_SERVER_USER, '--password', CONTENT_SERVER_PASS]
        cmd.append(path)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            via = 'content server' if library == CONTENT_SERVER_URL else 'direct'
            log.debug(f'calibredb connected via {via}')
            return (result.stdout + result.stderr).strip()
        combined = (result.stdout + result.stderr).lower()
        if any(s in combined for s in (
                'another calibre program', 'cannot lock',
                'connection refused', 'urlopen error', 'errno 61')):
            continue
        raise RuntimeError((result.stdout + result.stderr).strip())
    raise RuntimeError('Could not connect to Calibre — tried content server and direct access')


def calibredb_add_format(book_id, format_name, file_path):
    """
    Add a file as a named format to an existing Calibre book.
    Calibre derives the format name from the file extension, so we rename
    to a temp file with the correct extension if needed.
    """
    ext = f'.{format_name.lower()}'
    src = Path(file_path)
    if src.suffix.lower() == ext:
        actual_path = file_path
        tmp_dir = None
    else:
        tmp_dir = tempfile.mkdtemp()
        actual_path = os.path.join(tmp_dir, f'original{ext}')
        shutil.copy2(file_path, actual_path)

    try:
        for library in [CONTENT_SERVER_URL, CALIBRE_LIB]:
            cmd = [CALIBREDB, 'add_format', '--with-library', library]
            if library == CONTENT_SERVER_URL and CONTENT_SERVER_USER:
                cmd += ['--username', CONTENT_SERVER_USER, '--password', CONTENT_SERVER_PASS]
            cmd += [str(book_id), actual_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                return
            combined = (result.stdout + result.stderr).lower()
            if any(s in combined for s in (
                    'another calibre program', 'cannot lock',
                    'connection refused', 'urlopen error', 'errno 61')):
                continue
            raise RuntimeError((result.stdout + result.stderr).strip())
        raise RuntimeError('Could not connect to Calibre for add_format')
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def calibredb_set_metadata(book_id, **fields):
    for library in [CONTENT_SERVER_URL, CALIBRE_LIB]:
        cmd = [CALIBREDB, 'set_metadata', '--with-library', library]
        if library == CONTENT_SERVER_URL and CONTENT_SERVER_USER:
            cmd += ['--username', CONTENT_SERVER_USER, '--password', CONTENT_SERVER_PASS]
        for key, val in fields.items():
            cmd += ['--field', f'{key}:{val}']
        cmd.append(str(book_id))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return
        combined = (result.stdout + result.stderr).lower()
        if any(s in combined for s in (
                'another calibre program', 'cannot lock',
                'connection refused', 'urlopen error', 'errno 61')):
            continue
        raise RuntimeError((result.stdout + result.stderr).strip())


def parse_added_id(output):
    m = re.search(r'Added book ids:\s*(\d+)', output, re.I)
    return int(m.group(1)) if m else None


# ── Title helpers ─────────────────────────────────────────────────────────────

def title_from_path(path):
    stem = Path(path).stem
    stem = re.sub(r'\s*--\s*[0-9a-f]{32}\s*--.*$', '', stem, flags=re.I)
    stem = re.sub(r'_\d+$', '', stem)
    return stem.strip()


def _read_epub_title(epub_path):
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


def get_search_title(src_path, add_path, chinese_variant):
    """
    Return the title Calibre will store for this file, for duplicate detection.
    chinese_variant: the variant string used for conversion, or None if not converted.
    """
    ext = Path(src_path).suffix.lower()
    if ext == '.epub':
        t = _read_epub_title(add_path)
        return t if t else title_from_path(src_path)
    if ext in ('.txt', '.fb2') and chinese_variant:
        try:
            from chinese_engine import convert_string_s2t
            return convert_string_s2t(title_from_path(src_path), variant=chinese_variant)
        except Exception:
            pass
    return title_from_path(src_path)


def calibredb_title_exists(title):
    escaped = title.replace('"', '\\"')
    for library in [CONTENT_SERVER_URL, CALIBRE_LIB]:
        cmd = [CALIBREDB, 'search', '--with-library', library]
        if library == CONTENT_SERVER_URL and CONTENT_SERVER_USER:
            cmd += ['--username', CONTENT_SERVER_USER, '--password', CONTENT_SERVER_PASS]
        cmd.append(f'title:"{escaped}"')
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return True
        combined = (result.stdout + result.stderr).lower()
        if any(s in combined for s in (
                'another calibre program', 'cannot lock',
                'connection refused', 'urlopen error', 'errno 61')):
            continue
        return False
    return False


# ── File stability ────────────────────────────────────────────────────────────

def wait_for_stable(path, timeout=120, interval=2.0):
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
        self._in_progress = set()
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self._process(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._process(event.dest_path)

    def _process(self, path):
        p = Path(path)

        if p.name.startswith('.'):
            return

        ext = p.suffix.lower()
        if ext not in WATCH_EXTS:
            return

        if os.path.realpath(str(p.parent)) not in WATCH_FOLDERS_REAL:
            return

        canonical = str(p.resolve())
        with self._lock:
            if canonical in self._in_progress:
                return
            self._in_progress.add(canonical)

        log.info(f'Detected: {p.name}')

        tmp_dir = None
        try:
            if not wait_for_stable(path):
                log.error(f'Timed out waiting for file to finish: {p.name}')
                notify('Calibre Monitor ⚠️', f'Timeout — {p.name}')
                return

            add_path         = path
            chinese_variant  = None   # set if Chinese conversion ran
            action_parts     = []

            # ── Chinese conversion ────────────────────────────────
            if AUTO_CHINESE_ENABLED and ext in CHINESE_EXTS:
                script = detect_chinese_script(path)
                variant = None
                if script == 'simplified' and AUTO_CHINESE_DIR == 's2t':
                    variant = S2T_VARIANT
                    direction_label = f'S→T ({variant})'
                elif script == 'traditional' and AUTO_CHINESE_DIR == 't2s':
                    variant = T2S_VARIANT
                    direction_label = f'T→S ({variant})'

                if variant:
                    log.info(f'Chinese detected ({script}) — converting {direction_label}: {p.name}')
                    tmp_dir, add_path = convert_chinese(path, variant)
                    chinese_variant   = variant
                    action_parts.append(direction_label)

            # ── Auto ruby ─────────────────────────────────────────
            if AUTO_RUBY_ENABLED and ext == '.epub' and AUTO_RUBY_LEVELS:
                try:
                    ruby_tmp_dir, ruby_path = add_ruby_to_epub(add_path, AUTO_RUBY_LEVELS)
                    if ruby_path:
                        if tmp_dir:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        tmp_dir   = ruby_tmp_dir
                        add_path  = ruby_path
                        levels_str = '+'.join(
                            l for l in ['N1', 'N2', 'N3', 'N4', 'N5', 'unlisted']
                            if l in AUTO_RUBY_LEVELS
                        )
                        action_parts.append(f'ruby ({levels_str})')
                except Exception as e:
                    log.warning(f'Auto-ruby failed for {p.name}: {e}')

            # ── Duplicate check ───────────────────────────────────
            search_title = get_search_title(path, add_path, chinese_variant)
            if calibredb_title_exists(search_title):
                log.info(f'Duplicate skipped (already in library): {p.name}')
                notify('Calibre Monitor ℹ️', f'Duplicate — {p.name}')
                return

            # ── Add to Calibre ────────────────────────────────────
            output  = calibredb_add(add_path)
            book_id = parse_added_id(output)
            log.debug(f'calibredb output: {output}')

            action = 'Added' + ((' + ' + ' + '.join(action_parts)) if action_parts else '')
            log.info(f'{action}: {p.name}')

            # ── Keep original ─────────────────────────────────────
            if KEEP_ORIGINAL and book_id and ext == '.epub' and action_parts:
                try:
                    calibredb_add_format(book_id, 'ORIGINAL_EPUB', path)
                    log.info(f'Saved ORIGINAL_EPUB for book {book_id}')
                except Exception as e:
                    log.warning(f'Could not save ORIGINAL_EPUB: {e}')

            # ── Post-import metadata fixes ────────────────────────

            # CHM: title is often GBK-encoded internally; override with filename
            if ext == '.chm':
                clean = title_from_path(path)
                try:
                    calibredb_set_metadata(book_id, title=clean)
                    log.info(f'Fixed CHM title → "{clean}"')
                except Exception as me:
                    log.warning(f'Could not fix CHM title: {me}')

            # TXT/FB2 with Chinese conversion: convert the Calibre title too
            if ext in ('.txt', '.fb2') and chinese_variant:
                try:
                    from chinese_engine import convert_string_s2t
                    simp_title = title_from_path(path)
                    conv_title = convert_string_s2t(simp_title, variant=chinese_variant)
                    if conv_title != simp_title:
                        calibredb_set_metadata(book_id, title=conv_title)
                        log.info(f'Converted TXT/FB2 title: "{simp_title}" → "{conv_title}"')
                except Exception as me:
                    log.warning(f'Could not convert TXT/FB2 title: {me}')

            # ── Move to done folder ───────────────────────────────
            if DONE_FOLDER:
                try:
                    done_dir = p.parent / DONE_FOLDER
                    done_dir.mkdir(exist_ok=True)
                    dest = done_dir / p.name
                    if dest.exists():
                        dest = done_dir / f'{p.stem}_{int(time.time())}{p.suffix}'
                    shutil.move(path, dest)
                    log.info(f'Moved to {DONE_FOLDER}/: {p.name}')
                except FileNotFoundError:
                    if dest.exists():
                        log.info(f'Moved to {DONE_FOLDER}/: {p.name} (iCloud completed move)')
                    else:
                        log.warning(f'Could not move to {DONE_FOLDER}/ (file no longer local — iCloud eviction?): {p.name}')

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
    log.info(f'keep_original={KEEP_ORIGINAL}  '
             f'auto_chinese={AUTO_CHINESE_ENABLED}({AUTO_CHINESE_DIR})  '
             f'auto_ruby={AUTO_RUBY_ENABLED}({sorted(AUTO_RUBY_LEVELS)})')
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
