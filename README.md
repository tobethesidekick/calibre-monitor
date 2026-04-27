# Calibre Monitor

A macOS background service that watches folders for new ebook files and automatically imports them into Calibre — **even while Calibre is closed**. Works alongside the [FuriganaRuby Calibre plugin](https://github.com/tobethesidekick/furigana-ruby) — all behaviour settings (watch folders, auto Chinese conversion, auto ruby, keep original) are configured inside Calibre's own Preferences panel and read from there at startup.

> **Why a separate script?** Running as a macOS LaunchAgent means the monitor starts at login and keeps watching in the background regardless of whether Calibre is open. This is the key advantage over a built-in plugin, which can only watch while the app is running. If you use iCloud Drive to sync books across devices, files can arrive at any time — the monitor picks them up immediately.

---

## Features

- **Runs while Calibre is closed** — starts at login as a macOS LaunchAgent; no need to keep Calibre open
- **Watches iCloud Drive or local folders** — works across multiple devices via iCloud sync
- **Auto-imports** EPUB, TXT, PDF, MOBI, FB2, DJVU, AZW3, RTF, CHM, AZW, ACSM
- **Auto Simplified ↔ Traditional Chinese conversion** for EPUB, TXT, FB2, HTML (configurable direction and variant)
  - Detects script automatically (UTF-8 and GBK/GB18030 encoding support)
  - Converts title/author metadata to match
  - Fixes CHM titles (often GBK-encoded and misread by Calibre)
- **Auto furigana (ruby) annotation** for Japanese EPUBs — adds furigana for the JLPT levels you choose (N1–N5 + Unlisted) via the FuriganaRuby plugin engine
- **Keep original** — saves the unmodified file as `ORIGINAL_EPUB` format in Calibre before applying any conversion or annotation, so you can always recover the source
- **Duplicate detection** — searches the library by title before adding; leaves duplicates in the watch folder for manual review
- **Moves imported files** to an `_imported/` subfolder on success; errors and duplicates stay in the watch folder as a natural review queue
- **Fallback logic** — tries Calibre's content server first (when Calibre is open), falls back to direct library access (when Calibre is closed)
- **No double-processing** — deduplicates filesystem events that fire twice for the same file
- **macOS notifications** on success, duplicate, and error
- **Settings live in Calibre** — configure everything from the FuriganaRuby plugin's Preferences panel; the monitor reads the plugin's JSON file at startup so there is no need to edit `monitor_config.json` for behaviour settings

---

## Requirements

### System
- macOS (tested on macOS Sequoia)
- [Calibre](https://calibre-ebook.com) 6.0 or later
- Python 3.9 or later

### Python packages
```bash
pip3 install watchdog opencc-python-reimplemented
```

### FuriganaRuby source files
The script imports `lang_detect.py` and `chinese_engine.py` from the [FuriganaRuby plugin source](https://github.com/tobethesidekick/furigana-ruby). You do not need the full plugin — just these two files — but keeping the whole source folder is fine.

Clone or download the source and note the folder path for the `plugin_source` config key.

---

## Installation

### 1. Copy the script files

Place this folder somewhere permanent, e.g.:
```
~/Documents/ScriptForCalibre/CalibreMonitor/
```

Also place (or clone) the FuriganaRuby source at a sibling path, e.g.:
```
~/Documents/ScriptForCalibre/FuriganaRuby_source/
```

### 2. Create your config file

Copy the example config and fill in your values:
```bash
cp monitor_config.example.json monitor_config.json
```

Edit `monitor_config.json` — see the [Configuration reference](#configuration-reference) below.

### 3. Install Python dependencies
```bash
pip3 install watchdog opencc-python-reimplemented
```

### 4. Test it
```bash
python3 /path/to/calibre_monitor.py
```

Drop a file into your watch folder. You should see log output and a macOS notification.

### 5. Set up as a Launch Agent (auto-start at login)

Find your Python path:
```bash
which python3
```

Create the Launch Agent plist (replace paths to match your setup):
```bash
cat > ~/Library/LaunchAgents/com.user.calibremonitor.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.calibremonitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/python3</string>
        <string>/Users/yourname/Documents/ScriptForCalibre/CalibreMonitor/calibre_monitor.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/yourname/Library/Logs/calibre_monitor_agent.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/yourname/Library/Logs/calibre_monitor_agent.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/yourname/Documents/ScriptForCalibre/CalibreMonitor</string>
</dict>
</plist>
EOF
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.user.calibremonitor.plist
```

Confirm it's running (first column is the PID):
```bash
launchctl list | grep calibremonitor
```

#### Managing the Launch Agent

| Task | Command |
|------|---------|
| Stop | `launchctl unload ~/Library/LaunchAgents/com.user.calibremonitor.plist` |
| Start | `launchctl load ~/Library/LaunchAgents/com.user.calibremonitor.plist` |
| Restart | unload then load |
| Check status | `launchctl list \| grep calibremonitor` |

---

## Configuration reference

All settings live in `monitor_config.json` (gitignored — never committed). Copy from `monitor_config.example.json` to get started.

> **Behaviour settings** (`keep_original`, `auto_chinese_*`, `auto_ruby_*`) are read from the **FuriganaRuby plugin's JSON file** at startup. Configure them inside Calibre → Preferences → Plugins → FuriganaRuby → Customize plugin. You do not need to set them in `monitor_config.json` — values there act only as a fallback if the plugin file is absent.

### Infrastructure keys (edit in `monitor_config.json`)

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `watch_folders` | ✅ | — | Array of folder paths to watch. Supports `~`. iCloud paths work: `~/Library/Mobile Documents/com~apple~CloudDocs/FolderName` |
| `calibre_library` | ✅ | — | Full path to your Calibre library folder. Used as fallback when Calibre is closed. |
| `content_server_url` | — | `http://localhost:8080` | URL of Calibre's content server. Used when Calibre is open. |
| `content_server_username` | — | `""` | Content server username. Required if "Require username and password" is enabled in Calibre's sharing preferences. |
| `content_server_password` | — | `""` | Content server password. |
| `calibredb` | — | `/Applications/calibre.app/Contents/MacOS/calibredb` | Path to the `calibredb` binary. Only change if Calibre is installed in a non-standard location. |
| `plugin_source` | — | `""` | Path to the FuriganaRuby source folder. Required for Chinese detection/conversion and auto ruby annotation. |
| `log_file` | — | `~/Library/Logs/calibre_monitor.log` | Path to the rotating log file (2 MB max, 3 backups). |
| `done_folder` | — | `_imported` | Subfolder name inside the watch folder where successfully imported files are moved. Set to `""` to disable moving. |
| `extensions` | — | See below | Array of file extensions to watch. |

**Default extensions:** `.epub`, `.txt`, `.pdf`, `.mobi`, `.fb2`, `.djvu`, `.azw3`, `.rtf`, `.chm`, `.azw`, `.acsm`

### Behaviour keys (set via Calibre plugin UI — or fallback in `monitor_config.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `keep_original` | `false` | Save the unmodified file as `ORIGINAL_EPUB` in Calibre before any conversion or annotation. |
| `auto_chinese_enabled` | `false` | Enable automatic Simplified ↔ Traditional Chinese conversion on import. |
| `auto_chinese_direction` | `"s2t"` | `"s2t"` (Simplified → Traditional) or `"t2s"` (Traditional → Simplified). |
| `s2t_variant` | `"s2twp"` | OpenCC variant for S→T: `s2t`, `s2tw`, `s2twp` (Taiwan, recommended), `s2hk` (Hong Kong). |
| `t2s_variant` | `"t2s"` | OpenCC variant for T→S: `t2s`, `tw2s`, `tw2sp`, `hk2s`. |
| `auto_ruby_enabled` | `false` | Enable automatic furigana annotation for Japanese EPUBs on import. Requires `plugin_source`. |
| `auto_ruby_levels` | `["N1","N2","N3"]` | JLPT levels to annotate. Valid values: `"N1"`, `"N2"`, `"N3"`, `"N4"`, `"N5"`, `"unlisted"`. |

### Calibre content server setup

For `calibredb add` to work via the content server, you must enable authentication in Calibre:

1. Calibre → **Preferences** → **Sharing over the net**
2. **Main tab**: enable **"Require username and password"**
3. **User accounts tab**: create a user and tick **"Allow making changes via the API"**
4. Set the same username/password in `monitor_config.json`

---

## File behaviour

| Outcome | What happens to the source file |
|---------|--------------------------------|
| Successfully imported | Moved to `_imported/` inside the watch folder |
| Duplicate (title already in library) | Left in the watch folder — review and delete manually |
| Import error | Left in the watch folder — retry by restarting the monitor |
| Timeout (file never stabilised) | Left in the watch folder |

---

## How Simplified Chinese detection works

For **EPUB** files: reads the `dc:language` tag from the OPF metadata. If the language is `zh-Hans`, `zh-CN`, or `zh-SG` the book is simplified. For bare `zh` tags, falls back to character-set sampling of the content files.

For **TXT / FB2** files: reads the first 8,000 characters (trying UTF-8, then GB18030, then Big5), checks for Han characters, then counts simplified-only vs traditional-only characters. Requires a 2:1 ratio and at least 10 discriminating characters to make a call.

---

## Logs

Two log locations:

- `~/Library/Logs/calibre_monitor.log` — main rotating log (all detection, conversion, import activity)
- `~/Library/Logs/calibre_monitor_agent.log` — Launch Agent stdout/stderr (startup errors only)

---

## Moving to a new machine

1. Copy `CalibreMonitor/` and `FuriganaRuby_source/` to the new machine
2. Install dependencies: `pip3 install watchdog opencc-python-reimplemented`
3. Copy `monitor_config.example.json` → `monitor_config.json` and fill in the new machine's paths
4. Create the Launch Agent plist with the correct Python and script paths
5. `launchctl load` the plist

The iCloud watch folder is shared automatically — no extra setup needed.

---

## Troubleshooting

**Auto ruby or auto Chinese not triggering**
The startup log line shows the active settings — check `~/Library/Logs/calibre_monitor.log` for a line like `keep_original=True  auto_chinese=True(s2t)  auto_ruby=True(...)`. If values are wrong, open the FuriganaRuby plugin preferences in Calibre, set the values, click OK, then restart the monitor (`launchctl unload` / `load`).

**S→T conversion not triggering for TXT files**
Check `monitor_config.json` — `plugin_source` must point to a folder containing `lang_detect.py` and `chinese_engine.py`. Check the log for `No module named 'lang_detect'`.

**"Forbidden" error when adding via content server**
Authentication is not set up correctly. See [Calibre content server setup](#calibre-content-server-setup).

**Files added as duplicates**
The duplicate pre-check searches by title. For PDFs with malformed internal metadata (title/author swapped), the stored title may not match the filename-derived search title. Delete the duplicate from Calibre manually.

**Monitor not starting at login**
Check `~/Library/Logs/calibre_monitor_agent.log` for startup errors. Most common cause: wrong Python path in the plist.

**Restarting after config changes**
```bash
launchctl unload ~/Library/LaunchAgents/com.user.calibremonitor.plist
launchctl load ~/Library/LaunchAgents/com.user.calibremonitor.plist
```
