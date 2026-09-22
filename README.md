# BB Fab Browser

A visual browser for folders full of zipped texture/material packs.

Packs downloaded from Fab, Quixel, Poliigon and similar stores arrive as zip
files stuffed with `.jpg` texture maps and a single `.png` preview render.
That makes them impossible to browse: the file manager shows you a wall of
identical zip icons, and the only way to see what a pack looks like is to
unzip it.

BB Fab Browser reads the preview image straight out of each archive — nothing
is extracted to disk — and lays them all out in a thumbnail grid.

```
+----------------+--------------------------------------------+
|  Bricks        |  [img]   [img]   [img]   [img]             |
|  Fabric        |  Oak     Walnut  Pine    Driftwood         |
|  Metal         |                                            |
|  Wood       >  |  [img]   [img]   [img]   [img]             |
|                |  ...                                       |
+----------------+--------------------------------------------+
```

## Features

- **Folder tree on the left, previews on the right.** Click a folder, see every
  pack in it.
- **Nothing is extracted.** Previews are read from inside the zip and decoded in
  memory.
- **Smart preview detection.** In a pack of `.jpg` maps the lone `.png` is the
  preview, so png wins outright. Names containing `preview`, `thumbnail`,
  `render` or `cover` rank higher; anything named like a texture map
  (`_Normal`, `_Albedo`, `_Roughness`, ...) ranks lower. macOS `__MACOSX`
  resource forks are skipped. If a pack has no png at all, a jpg preview is
  used instead.
- **Disk-backed thumbnail cache**, keyed on the archive's path, size and
  modification time — the first pass through a big folder is the slow one, and
  re-editing an archive invalidates its thumbnail automatically.
- **Loads in the background**, four archives at a time, so the window stays
  responsive while a folder of hundreds fills in.
- **Include subfolders** to flatten an entire library into one grid.
- **Search** filters the visible grid by pack name as you type.
- **Adjustable thumbnail size**, 96 px to 320 px.
- **Double-click any pack** for a large preview, the full file listing with
  sizes, and buttons to reveal it in your file manager, copy its path, or save
  the preview image out as a png/jpg.
- Remembers your last folder, thumbnail size and subfolder setting between runs.

## Installation

Requires Python 3.8 or later.

```bash
pip install -r requirements.txt
```

Pillow is the only requirement. Tkinter ships with Python on Windows and macOS;
on Linux install it from your package manager if it's missing
(`sudo apt install python3-tk`).

Optionally `pip install tkinterdnd2` to enable dragging a folder onto the window
to open it. The app works fine without it.

## Trying it without a texture library

If you want to see it working before pointing it at real packs:

```bash
python make_sample_library.py
```

That writes 16 fake texture packs (jpg maps + a png preview each, ~600 KB
total) into `sample_library/`, laid out in category folders. Open that folder
in the app.

## Usage

```bash
python bb_fab_browser.py
```

or double-click `run_bb_fab_browser.bat` on Windows / run
`./run_bb_fab_browser.sh` on Linux and macOS.

Then click **Open folder...** and pick the folder holding your texture zips.

### Controls

| Action | Result |
| --- | --- |
| Click a folder in the tree | Show that folder's packs |
| Click a thumbnail | Select it; the status bar names the preview file used |
| Double-click / Enter | Open the detail window |
| Right-click | Details, show in file manager, copy path |
| Arrow keys | Move around the grid |
| Mouse wheel | Scroll the grid |
| Esc (detail window) | Close |

## How the preview is chosen

Every file in the archive is scored, highest wins:

| Rule | Effect |
| --- | --- |
| `.png` extension | +1000 |
| `.jpg` / `.jpeg` / `.webp` / `.bmp` | +0 (fallback only) |
| name contains `preview` / `thumbnail` / `thumb` / `render` / `cover` | +120 down to +85 |
| name contains `sphere` / `ball` | +70 / +60 |
| name looks like a texture map (`_normal`, `albedo`, `roughness`, `orm`, ...) | −200 |
| each folder level deep in the archive | −5 |
| tie-break | larger file wins |

Directories, dotfiles and `__MACOSX/` entries never qualify. Archives with no
usable image get a "no preview" placeholder card so you can still see they're
there — as do archives that fail to open, with the reason in the status bar.

## Tests

```bash
python test_preview_logic.py
```

Covers the scanning and caching logic (preview selection, resource-fork
skipping, cache invalidation, corrupt archives, alpha flattening) without
needing a display.

## Where settings and cache live

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Cache | `%LOCALAPPDATA%\BBFabBrowser\cache` | `~/Library/Caches/BBFabBrowser` | `~/.cache/bb-fab-browser` |
| Settings | `%APPDATA%\BBFabBrowser\settings.json` | `~/Library/Application Support/BBFabBrowser/settings.json` | `~/.config/bb-fab-browser/settings.json` |

**Clear thumbnail cache** in the toolbar empties the cache directory.
