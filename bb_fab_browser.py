#!/usr/bin/env python3
"""
BB Fab Browser
A browser for zipped texture/material archives.

Texture packs downloaded from Fab, Quixel, Poliigon and similar stores ship as
zip files full of .jpg texture maps with a single .png preview render inside.
This tool walks a folder of those zips, pulls the preview image straight out of
each archive (without extracting anything to disk) and lays them out in a
thumbnail grid so you can find the material you want by eye.

Left pane : folder tree
Right pane: preview grid for the selected folder
"""

import errno
import io
import json
import hashlib
import os
import posixpath
import subprocess
import sys
import threading
import queue
import zipfile
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

from PIL import Image, ImageTk, ImageDraw

APP_NAME = "BB Fab Browser"
APP_SLUG = "bb-fab-browser"

# Archives we look inside
ARCHIVE_EXTS = ('.zip',)

# The preview is normally the only .png in an otherwise .jpg texture set, so png
# wins outright.  The rest are a fallback for packs that ship a jpg preview.
PREFERRED_EXTS = ('.png',)
FALLBACK_EXTS = ('.jpg', '.jpeg', '.webp', '.bmp')

# Filename fragments that mark an image as the pack's beauty render
PREVIEW_HINTS = (
    ('preview', 120),
    ('thumbnail', 115),
    ('thumb', 110),
    ('render', 95),
    ('cover', 85),
    ('sphere', 70),
    ('ball', 60),
)

# Texture-map suffixes: these are maps, never the preview, so push them down
MAP_HINTS = (
    'albedo', 'basecolor', 'base_color', 'diffuse', 'normal', 'roughness',
    'metallic', 'metalness', 'height', 'displacement', 'occlusion', '_ao',
    'opacity', 'specular', 'gloss', 'bump', 'cavity', 'curvature', 'emissive',
    'translucency', 'fuzz', 'transmission', 'orm',
)

THUMB_SIZES = (96, 128, 160, 192, 224, 256, 320)
DEFAULT_THUMB = 192

CARD_PAD = 14          # gap between cards
LABEL_H = 34           # room under each thumbnail for its name
CARD_BG = '#2c2c2c'
CARD_BG_SEL = '#2f5d9e'
GRID_BG = '#1e1e1e'
TEXT_FG = '#e6e6e6'
TEXT_FG_DIM = '#9a9a9a'
PANEL_BG = '#262626'
CHROME_BG = '#303030'
CONTROL_BG = '#3b3b3b'
CONTROL_ACTIVE = '#4a4a4a'
BORDER = '#454545'


# --------------------------------------------------------------------------
# Paths for cache + settings
# --------------------------------------------------------------------------

def user_cache_dir():
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'BBFabBrowser', 'cache')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Caches/BBFabBrowser')
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, APP_SLUG)


def user_config_path():
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'BBFabBrowser', 'settings.json')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/BBFabBrowser/settings.json')
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, APP_SLUG, 'settings.json')


def load_settings():
    try:
        with open(user_config_path(), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    path = user_config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass  # settings are a convenience, never worth an error dialog


# --------------------------------------------------------------------------
# Reading previews out of archives
# --------------------------------------------------------------------------

def score_member(name, size):
    """Rank a file inside an archive by how likely it is to be the preview.

    Returns None for entries that can't be a preview at all.
    """
    if name.endswith('/'):
        return None
    lower = name.lower()
    if lower.startswith('__macosx/') or '/__macosx/' in lower:
        return None
    base = posixpath.basename(lower)
    if not base or base.startswith('.'):
        return None

    ext = os.path.splitext(base)[1]
    if ext in PREFERRED_EXTS:
        score = 1000
    elif ext in FALLBACK_EXTS:
        score = 0
    else:
        return None

    stem = os.path.splitext(base)[0]
    for hint, bonus in PREVIEW_HINTS:
        if hint in stem:
            score += bonus
            break

    if any(hint in stem for hint in MAP_HINTS):
        score -= 200

    # Files near the top of the archive are more likely to be the pack preview
    score -= name.count('/') * 5

    return (score, size)


def find_preview_member(zf):
    """Pick the best preview candidate from an open ZipFile, or None."""
    best = None
    best_key = None
    for info in zf.infolist():
        key = score_member(info.filename, info.file_size)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key, best = key, info
    return best


def make_thumbnail(image, size, bg=CARD_BG):
    """Fit an image inside a size x size box, flattening alpha onto bg."""
    image = image.copy()
    image.thumbnail((size, size), Image.LANCZOS)
    if image.mode in ('RGBA', 'LA', 'P'):
        image = image.convert('RGBA')
        flat = Image.new('RGB', image.size, bg)
        flat.paste(image, mask=image.split()[-1])
        image = flat
    else:
        image = image.convert('RGB')
    return image


def placeholder_thumbnail(size, text='no preview'):
    img = Image.new('RGB', (size, size), CARD_BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((4, 4, size - 5, size - 5), outline='#4a4a4a')
    tw = draw.textlength(text)
    draw.text(((size - tw) / 2, size / 2 - 6), text, fill=TEXT_FG_DIM)
    return img


# Folder written next to the archives. It's left visible in the file manager
# but kept out of the app's own folder tree and archive scan.
SHARED_CACHE_DIR = 'bbfab_cache'


class ThumbnailCache:
    """Disk-backed thumbnail cache.

    Thumbnails are written to a bbfab_cache folder beside the archives,
    so everyone browsing the same network share reuses them instead of each
    rebuilding their own. Folders that can't be written to fall back to a
    per-user cache in `directory`.
    """

    def __init__(self, directory=None, shared=True):
        self.shared = shared
        self.dir = directory or user_cache_dir()
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            self.dir = None
        self._read_only = set()   # folders whose shared cache we can't write

    # -- naming ----------------------------------------------------------

    @staticmethod
    def _version(stat):
        # Whole seconds: SMB clients on different OSes disagree about
        # sub-second mtime precision on the same file.
        raw = '%d|%d' % (int(stat.st_mtime), stat.st_size)
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]

    @staticmethod
    def _shared_prefix(path, thumb_size):
        # Keyed on the file name, never the full path: the same share shows up
        # as Z:\, \\server\share or /mnt/share depending on who is looking.
        return '%s.%d.' % (os.path.basename(path), thumb_size)

    def _shared_file(self, path, stat, thumb_size):
        folder = os.path.join(os.path.dirname(os.path.abspath(path)),
                              SHARED_CACHE_DIR)
        name = self._shared_prefix(path, thumb_size) + self._version(stat) + '.png'
        return os.path.join(folder, name)

    def _local_file(self, path, stat, thumb_size):
        raw = '%s|%d|%d|%d' % (os.path.abspath(path), stat.st_mtime_ns,
                               stat.st_size, thumb_size)
        key = hashlib.sha1(raw.encode('utf-8')).hexdigest()
        return os.path.join(self.dir, key[:2], key + '.png')

    # -- read / write ----------------------------------------------------

    @staticmethod
    def _read(target):
        try:
            with Image.open(target) as img:
                img.load()
                return img.copy()
        except Exception:
            return None

    def get(self, path, stat, thumb_size):
        if self.shared:
            image = self._read(self._shared_file(path, stat, thumb_size))
            if image is not None:
                return image
        if not self.dir:
            return None
        image = self._read(self._local_file(path, stat, thumb_size))
        if image is not None and self.shared:
            # Built before sharing existed, or while the folder was read-only:
            # hand it to everyone else now.
            self._put_shared(path, stat, thumb_size, image)
        return image

    def _put_shared(self, path, stat, thumb_size, image):
        """Write to the folder's shared cache; False if that isn't possible."""
        folder = os.path.dirname(os.path.abspath(path))
        if folder in self._read_only:
            return False
        target = self._shared_file(path, stat, thumb_size)
        try:
            self._write(target, image)
        except OSError as exc:
            if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
                self._read_only.add(folder)   # don't retry every archive
            return False
        self._prune(target, self._shared_prefix(path, thumb_size))
        return True

    def put(self, path, stat, thumb_size, image):
        if self.shared and self._put_shared(path, stat, thumb_size, image):
            return
        if self.dir:
            try:
                self._write(self._local_file(path, stat, thumb_size), image)
            except OSError:
                pass

    @staticmethod
    def _write(target, image):
        folder = os.path.dirname(target)
        if not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        # Write then rename, so another user reading the share never sees a
        # half-written png.
        tmp = '%s.%s.tmp' % (target, os.urandom(4).hex())
        try:
            image.save(tmp, 'PNG')
            try:
                os.replace(tmp, target)
            except OSError:
                # Someone else finished the same thumbnail first and has it
                # open; theirs is just as good.
                if not os.path.isfile(target):
                    raise
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @staticmethod
    def _prune(target, prefix):
        """Drop thumbnails of older versions of the same archive."""
        folder, keep = os.path.split(target)
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            rest = name[len(prefix):]
            if (name != keep and name.startswith(prefix)
                    and name.endswith('.png') and rest.count('.') == 1):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass

    def clear(self, folders=()):
        """Empty the per-user cache and the shared caches of `folders`."""
        removed = 0
        roots = [self.dir] if self.dir else []
        roots += [os.path.join(f, SHARED_CACHE_DIR) for f in folders]
        for top in roots:
            if not os.path.isdir(top):
                continue
            for root, _dirs, files in os.walk(top):
                for name in files:
                    if name.endswith('.png'):
                        try:
                            os.remove(os.path.join(root, name))
                            removed += 1
                        except OSError:
                            pass
        for folder in folders:
            try:
                os.rmdir(os.path.join(folder, SHARED_CACHE_DIR))
            except OSError:
                pass
        return removed


def load_preview(path, thumb_size, cache):
    """Return (PIL thumbnail, member name or None, error or None)."""
    try:
        stat = os.stat(path)
    except OSError as exc:
        return None, None, str(exc)

    cached = cache.get(path, stat, thumb_size)
    if cached is not None:
        return cached, None, None

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in ARCHIVE_EXTS:
            with zipfile.ZipFile(path) as zf:
                member = find_preview_member(zf)
                if member is None:
                    return None, None, 'no image inside archive'
                data = zf.read(member)
            member_name = member.filename
        else:
            with open(path, 'rb') as fh:
                data = fh.read()
            member_name = None

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            thumb = make_thumbnail(img, thumb_size)
    except zipfile.BadZipFile:
        return None, None, 'not a readable zip'
    except Exception as exc:
        return None, None, str(exc)

    cache.put(path, stat, thumb_size, thumb)
    return thumb, member_name, None


# Anything Pillow will open from inside an archive. Files outside this list
# are listed but shown as "not an image" rather than attempted.
VIEWABLE_EXTS = PREFERRED_EXTS + FALLBACK_EXTS + (
    '.tga', '.tif', '.tiff', '.gif', '.ico', '.ppm', '.pgm', '.dds')


def is_viewable(name):
    return os.path.splitext(name.lower())[1] in VIEWABLE_EXTS


def human_size(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return '%d %s' % (size, unit) if unit == 'B' else '%.1f %s' % (size, unit)
        size /= 1024.0
    return '%d B' % size


def reveal_in_file_manager(path):
    """Open the OS file manager with `path` selected, best effort."""
    try:
        if sys.platform == 'win32':
            # explorer only honours /select, when the path is glued to the flag
            subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path))
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', path])
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(path)])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Thumbnail grid
# --------------------------------------------------------------------------

class PreviewGrid(ttk.Frame):
    """Canvas-backed grid of preview cards, loaded on a worker pool."""

    def __init__(self, master, cache, thumb_size=DEFAULT_THUMB,
                 on_select=None, on_activate=None, on_progress=None):
        super().__init__(master)
        self.cache = cache
        self.thumb_size = thumb_size
        self.on_select = on_select
        self.on_activate = on_activate
        self.on_progress = on_progress

        self.items = []          # every archive in the folder
        self.visible = []        # items passing the current filter
        self.selected = None     # index into self.visible
        self.generation = 0
        self.pending = 0
        self._columns = 0
        self._resize_job = None

        self.canvas = tk.Canvas(self, bg=GRID_BG, highlightthickness=0,
                                takefocus=True)
        self.scroll = ttk.Scrollbar(self, orient='vertical',
                                    command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scroll.pack(side='right', fill='y')

        self.font = tkfont.nametofont('TkDefaultFont')

        self.canvas.bind('<Configure>', self._on_resize)
        self.canvas.bind('<Button-1>', self._on_click)
        self.canvas.bind('<Double-Button-1>', self._on_double_click)
        self.canvas.bind('<Button-3>', self._on_right_click)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self.canvas.bind('<Button-4>', lambda e: self.canvas.yview_scroll(-3, 'units'))
        self.canvas.bind('<Button-5>', lambda e: self.canvas.yview_scroll(3, 'units'))
        self.canvas.bind('<Left>', lambda e: self._move_selection(-1))
        self.canvas.bind('<Right>', lambda e: self._move_selection(1))
        self.canvas.bind('<Up>', lambda e: self._move_selection(-max(1, self._columns)))
        self.canvas.bind('<Down>', lambda e: self._move_selection(max(1, self._columns)))
        self.canvas.bind('<Return>', lambda e: self._activate_selected())

        self.menu = tk.Menu(self, tearoff=0, bg=CONTROL_BG, fg=TEXT_FG,
                            activebackground=CARD_BG_SEL,
                            activeforeground='#ffffff', borderwidth=1)
        self.menu.add_command(label='Open details', command=self._activate_selected)
        self.menu.add_command(label='Show in file manager',
                              command=self._reveal_selected)
        self.menu.add_command(label='Copy path', command=self._copy_selected_path)

        self.pool = ThreadPoolExecutor(max_workers=4)
        self.results = queue.Queue()
        self.after(60, self._drain_results)

    # -- public API ------------------------------------------------------

    def set_paths(self, paths):
        self.generation += 1
        self.pending = 0
        self.items = [{'path': p, 'name': os.path.basename(p), 'thumb': None,
                       'photo': None, 'member': None, 'error': None,
                       'loaded': False} for p in paths]
        self.selected = None
        self.apply_filter('')
        self._submit_loads()

    def apply_filter(self, text):
        text = (text or '').strip().lower()
        if text:
            self.visible = [i for i in self.items if text in i['name'].lower()]
        else:
            self.visible = list(self.items)
        self.selected = None
        self.canvas.yview_moveto(0)
        self.redraw()

    def set_thumb_size(self, size):
        if size == self.thumb_size:
            return
        self.thumb_size = size
        self.generation += 1
        self.pending = 0
        for item in self.items:
            item.update(thumb=None, photo=None, loaded=False, error=None)
        self.redraw()
        self._submit_loads()

    def selected_item(self):
        if self.selected is None or self.selected >= len(self.visible):
            return None
        return self.visible[self.selected]

    def shutdown(self):
        self.pool.shutdown(wait=False)

    # -- loading ---------------------------------------------------------

    def _submit_loads(self):
        gen = self.generation
        size = self.thumb_size
        for item in self.items:
            self.pending += 1
            self.pool.submit(self._load_one, gen, item, size)
        self._report_progress()

    def _load_one(self, gen, item, size):
        if gen != self.generation:
            return
        thumb, member, error = load_preview(item['path'], size, self.cache)
        self.results.put((gen, item, thumb, member, error))

    def _drain_results(self):
        dirty = False
        for _ in range(24):                     # keep the UI responsive
            try:
                gen, item, thumb, member, error = self.results.get_nowait()
            except queue.Empty:
                break
            if gen != self.generation:
                continue          # superseded; its pending count is gone too
            self.pending = max(0, self.pending - 1)
            item['thumb'] = thumb
            item['member'] = member
            item['error'] = error
            item['loaded'] = True
            if thumb is not None:
                item['photo'] = ImageTk.PhotoImage(thumb)
            else:
                item['photo'] = ImageTk.PhotoImage(
                    placeholder_thumbnail(self.thumb_size))
            dirty = True
        if dirty:
            self._refresh_images()
            self._report_progress()
        self.after(60, self._drain_results)

    def _report_progress(self):
        if self.on_progress:
            self.on_progress(len(self.items), self.pending)

    # -- layout ----------------------------------------------------------

    def _metrics(self):
        card_w = self.thumb_size + 12
        card_h = self.thumb_size + LABEL_H + 12
        return card_w, card_h, card_w + CARD_PAD, card_h + CARD_PAD

    def _on_resize(self, _event):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(80, self.redraw)

    def redraw(self):
        self._resize_job = None
        self.canvas.delete('all')
        card_w, card_h, cell_w, cell_h = self._metrics()
        width = max(self.canvas.winfo_width(), cell_w)
        cols = max(1, (width - CARD_PAD) // cell_w)
        self._columns = cols

        if not self.visible:
            self.canvas.create_text(
                width // 2, 60, text='No archives in this folder',
                fill=TEXT_FG_DIM, font=self.font)
            self.canvas.configure(scrollregion=(0, 0, width, 120))
            return

        for index, item in enumerate(self.visible):
            row, col = divmod(index, cols)
            x = CARD_PAD + col * cell_w
            y = CARD_PAD + row * cell_h
            tag = 'card%d' % index
            bg = CARD_BG_SEL if index == self.selected else CARD_BG
            self.canvas.create_rectangle(x, y, x + card_w, y + card_h,
                                         fill=bg, outline=bg, tags=(tag, 'card'))
            cx = x + card_w / 2
            if item['photo'] is not None:
                self.canvas.create_image(cx, y + 6 + self.thumb_size / 2,
                                         image=item['photo'],
                                         tags=(tag, 'card', 'thumb%d' % index))
            else:
                self.canvas.create_text(cx, y + 6 + self.thumb_size / 2,
                                        text='...', fill=TEXT_FG_DIM,
                                        tags=(tag, 'card', 'thumb%d' % index))
            label = self._elide(os.path.splitext(item['name'])[0], card_w - 10)
            self.canvas.create_text(cx, y + self.thumb_size + 14, text=label,
                                    fill=TEXT_FG, font=self.font, anchor='n',
                                    tags=(tag, 'card'))

        rows = (len(self.visible) + cols - 1) // cols
        self.canvas.configure(scrollregion=(0, 0, width,
                                            rows * cell_h + CARD_PAD))

    def _refresh_images(self):
        """Swap in newly loaded thumbnails without re-laying out the grid."""
        for index, item in enumerate(self.visible):
            if item['photo'] is None:
                continue
            ids = self.canvas.find_withtag('thumb%d' % index)
            if not ids:
                continue
            item_id = ids[0]
            if self.canvas.type(item_id) == 'image':
                self.canvas.itemconfigure(item_id, image=item['photo'])
            else:
                coords = self.canvas.coords(item_id)
                self.canvas.delete(item_id)
                self.canvas.create_image(coords[0], coords[1],
                                         image=item['photo'],
                                         tags=('card%d' % index, 'card',
                                               'thumb%d' % index))

    def _elide(self, text, max_width):
        if self.font.measure(text) <= max_width:
            return text
        ellipsis = '...'
        while text and self.font.measure(text + ellipsis) > max_width:
            text = text[:-1]
        return text + ellipsis

    # -- interaction -----------------------------------------------------

    def _index_at(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        for item_id in self.canvas.find_overlapping(x, y, x, y):
            for tag in self.canvas.gettags(item_id):
                if tag.startswith('card') and tag != 'card':
                    return int(tag[4:])
        return None

    def _select(self, index):
        if index == self.selected:
            return
        self.selected = index
        self.redraw()
        if self.on_select:
            self.on_select(self.selected_item())

    def _on_click(self, event):
        self.canvas.focus_set()
        self._select(self._index_at(event))

    def _on_double_click(self, event):
        index = self._index_at(event)
        if index is not None:
            self._select(index)
            self._activate_selected()

    def _on_right_click(self, event):
        index = self._index_at(event)
        if index is None:
            return
        self._select(index)
        self.menu.tk_popup(event.x_root, event.y_root)

    def _on_wheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, 'units')

    def _move_selection(self, step):
        if not self.visible:
            return
        current = 0 if self.selected is None else self.selected
        self._select(max(0, min(len(self.visible) - 1, current + step)))
        self._scroll_to_selection()

    def _scroll_to_selection(self):
        if self.selected is None:
            return
        _cw, _ch, _cellw, cell_h = self._metrics()
        row = self.selected // max(1, self._columns)
        top = CARD_PAD + row * cell_h
        region = str(self.canvas.cget('scrollregion')).split()
        total = float(region[-1]) if region else 0.0
        if total <= 0:
            return
        view_top = self.canvas.canvasy(0)
        view_bottom = view_top + self.canvas.winfo_height()
        if top < view_top:
            self.canvas.yview_moveto(top / total)
        elif top + cell_h > view_bottom:
            self.canvas.yview_moveto((top + cell_h - self.canvas.winfo_height()) / total)

    def _activate_selected(self):
        item = self.selected_item()
        if item and self.on_activate:
            self.on_activate(item)

    def _reveal_selected(self):
        item = self.selected_item()
        if item:
            reveal_in_file_manager(item['path'])

    def _copy_selected_path(self):
        item = self.selected_item()
        if item:
            self.clipboard_clear()
            self.clipboard_append(item['path'])


# --------------------------------------------------------------------------
# Detail window
# --------------------------------------------------------------------------

class DetailWindow(tk.Toplevel):
    """Large image view plus a clickable listing of everything in the archive.

    Selecting any row loads that file from the zip and shows it, so you can
    flip through the texture maps, not just the preview render.
    """

    def __init__(self, master, path):
        super().__init__(master)
        self.path = path
        self.title(os.path.basename(path))
        self.geometry('1100x760')
        self.configure(bg=CHROME_BG)

        self.photo = None            # keeps the PhotoImage alive
        self.current_image = None    # full-size PIL image on show
        self.current_name = None
        self.row_member = {}         # tree iid -> member name
        self.preview_member = None
        self._load_token = 0
        self._render_job = None
        self._rendered_for = None

        panes = ttk.PanedWindow(self, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=8, pady=8)

        left = ttk.Frame(panes)
        self.image_frame = tk.Frame(left, bg=GRID_BG, highlightthickness=0)
        self.image_frame.pack(fill='both', expand=True)
        self.image_label = tk.Label(self.image_frame, bg=GRID_BG, fg=TEXT_FG_DIM,
                                    text='Loading...')
        self.image_label.pack(fill='both', expand=True)
        self.image_frame.bind('<Configure>', self._on_pane_resize)
        panes.add(left, weight=3)

        right = ttk.Frame(panes)
        ttk.Label(right, text='Archive contents  -  click a file to view it'
                  ).pack(anchor='w', pady=(0, 4))
        self.tree = ttk.Treeview(right, columns=('size',), show='tree headings',
                                 selectmode='browse')
        self.tree.heading('#0', text='File')
        self.tree.heading('size', text='Size')
        self.tree.column('size', width=90, anchor='e', stretch=False)
        self.tree.tag_configure('preview', foreground='#8fc7ff')
        self.tree.tag_configure('other', foreground=TEXT_FG_DIM)
        vsb = ttk.Scrollbar(right, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._on_row_select)
        panes.add(right, weight=2)

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Button(bar, text='Show in file manager',
                   command=lambda: reveal_in_file_manager(self.path)).pack(side='left')
        ttk.Button(bar, text='Copy path',
                   command=self._copy_path).pack(side='left', padx=6)
        ttk.Button(bar, text='Save image as...',
                   command=self._save_image).pack(side='left')
        ttk.Button(bar, text='Close', command=self.destroy).pack(side='right')

        self.status = ttk.Label(self, text=self.path, anchor='w')
        self.status.pack(fill='x', padx=8, pady=(0, 6))

        self.bind('<Escape>', lambda e: self.destroy())
        self.after(10, self._populate)

    # -- contents listing ------------------------------------------------

    def _populate(self):
        try:
            with zipfile.ZipFile(self.path) as zf:
                member = find_preview_member(zf)
                self.preview_member = member.filename if member else None
                contents = [(i.filename, i.file_size) for i in zf.infolist()
                            if not i.filename.endswith('/')]
        except Exception as exc:
            self.image_label.configure(text='Cannot open this archive:\n%s' % exc)
            self.status.configure(text='%s  -  %s' % (self.path, exc))
            return

        if not contents:
            self.image_label.configure(text='This archive is empty')
            return

        preview_row = None
        for name, size in sorted(contents, key=lambda c: c[0].lower()):
            viewable = is_viewable(name)
            tag = 'preview' if name == self.preview_member else (
                'other' if not viewable else '')
            label = name + ('   <- preview' if name == self.preview_member else '')
            iid = self.tree.insert('', 'end', text=label,
                                   values=(human_size(size),),
                                   tags=(tag,) if tag else ())
            self.row_member[iid] = name
            if name == self.preview_member:
                preview_row = iid

        first = preview_row or self.tree.get_children('')[0]
        self.tree.selection_set(first)
        self.tree.focus(first)
        self.tree.see(first)

    def _on_row_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        name = self.row_member.get(selection[0])
        if name and name != self.current_name:
            self._show_member(name)

    # -- loading one member ----------------------------------------------

    def _show_member(self, name):
        self.current_name = name
        self._load_token += 1
        token = self._load_token

        if not is_viewable(name):
            self.current_image = None
            self.photo = None
            self.image_label.configure(
                image='', text='%s\n\nNot an image file.' % os.path.basename(name))
            self.status.configure(text='%s  -  %s' % (self.path, name))
            return

        self.image_label.configure(image='', text='Loading %s...'
                                   % os.path.basename(name))

        def work():
            try:
                with zipfile.ZipFile(self.path) as zf:
                    data = zf.read(name)
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
                    loaded = img.copy()
            except Exception as exc:
                loaded, error = None, str(exc)
            else:
                error = None
            self.after(0, lambda: deliver(loaded, error))

        def deliver(loaded, error):
            if token != self._load_token:
                return               # user clicked something else meanwhile
            if loaded is None:
                self.current_image = None
                self.photo = None
                self.image_label.configure(image='', text='Cannot display this '
                                           'file:\n%s' % error)
                self.status.configure(text='%s  -  %s' % (self.path, error))
                return
            self.current_image = loaded
            self._rendered_for = None
            self._render()
            self.status.configure(
                text='%s  -  %s  (%d x %d, %s)'
                     % (self.path, name, loaded.width, loaded.height,
                        loaded.mode))

        threading.Thread(target=work, daemon=True).start()

    # -- rendering to fit the pane ---------------------------------------

    def _on_pane_resize(self, _event):
        if self._render_job:
            self.after_cancel(self._render_job)
        self._render_job = self.after(120, self._render)

    def _render(self):
        self._render_job = None
        if self.current_image is None:
            return
        width = self.image_frame.winfo_width() - 8
        height = self.image_frame.winfo_height() - 8
        if width < 40 or height < 40:
            return
        if self._rendered_for == (width, height):
            return
        self._rendered_for = (width, height)
        # thumbnail() only ever shrinks, so small previews are never blown up
        shown = self.current_image.copy()
        shown.thumbnail((width, height), Image.LANCZOS)
        if shown.mode in ('RGBA', 'LA', 'P'):
            shown = shown.convert('RGBA')
            flat = Image.new('RGB', shown.size, GRID_BG)
            flat.paste(shown, mask=shown.split()[-1])
            shown = flat
        else:
            shown = shown.convert('RGB')
        self.photo = ImageTk.PhotoImage(shown)
        self.image_label.configure(image=self.photo, text='')

    # -- buttons ----------------------------------------------------------

    def _copy_path(self):
        self.clipboard_clear()
        self.clipboard_append(self.path)

    def _save_image(self):
        if self.current_image is None:
            messagebox.showinfo(APP_NAME, 'No image is being shown.', parent=self)
            return
        stem = os.path.splitext(os.path.basename(self.current_name or 'image'))[0]
        target = filedialog.asksaveasfilename(
            parent=self,
            defaultextension='.png',
            initialfile=stem + '.png',
            filetypes=[('PNG image', '*.png'), ('JPEG image', '*.jpg')])
        if not target:
            return
        try:
            image = self.current_image
            if target.lower().endswith(('.jpg', '.jpeg')):
                image = image.convert('RGB')
            image.save(target)
        except Exception as exc:
            messagebox.showerror(APP_NAME, 'Could not save image:\n%s' % exc,
                                 parent=self)


# --------------------------------------------------------------------------
# Filesystem tree helpers
# --------------------------------------------------------------------------

# Folders Windows keeps at the root of a drive that are noise in a browser
WINDOWS_SKIP = {
    '$recycle.bin', 'system volume information', 'config.msi', 'recovery',
    '$winreagent', '$sysreset', 'msocache', 'perflogs',
}


def list_tree_roots():
    """Top-level nodes for the folder tree: every drive, or / and home."""
    if sys.platform == 'win32':
        roots = []
        try:
            import ctypes
            bits = ctypes.windll.kernel32.GetLogicalDrives()
        except Exception:
            bits = 0
        if bits:
            # No I/O here: expanding a drive is what actually touches it, so a
            # disconnected network drive costs nothing until you click it.
            for i in range(26):
                if bits >> i & 1:
                    roots.append('%s:\\' % chr(ord('A') + i))
        if not roots:
            roots = [d for d in ('%s:\\' % c for c in 'CDEFGH')
                     if os.path.isdir(d)]
        return roots

    home = os.path.expanduser('~')
    roots = ['/']
    if os.path.isdir(home):
        roots.append(home)
    return roots


def root_label(path):
    if sys.platform == 'win32':
        return path.rstrip('\\')            # "C:"
    if path == '/':
        return '/  (filesystem)'
    return 'Home  (%s)' % os.path.basename(path.rstrip('/'))


def _skip_entry(entry):
    if entry.name.startswith('.') or entry.name == SHARED_CACHE_DIR:
        return True
    if sys.platform == 'win32' and entry.name.lower() in WINDOWS_SKIP:
        return True
    return False


def subdirectories(path):
    """Immediate subdirectories of path, sorted, unreadable ones skipped."""
    found = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if _skip_entry(entry):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append((entry.name, entry.path))
                except OSError:
                    continue
    except (OSError, ValueError):
        return []
    found.sort(key=lambda pair: pair[0].lower())
    return found


def has_subdirectory(path):
    """Like subdirectories() but stops at the first hit — used for arrows."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                if _skip_entry(entry):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        return True
                except OSError:
                    continue
    except (OSError, ValueError):
        return False
    return False


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

def apply_dark_theme(root):
    """Give the ttk chrome the same dark palette as the thumbnail grid."""
    style = ttk.Style(root)
    if 'clam' in style.theme_names():
        style.theme_use('clam')

    root.configure(bg=CHROME_BG)
    root.option_add('*TCombobox*Listbox.background', CONTROL_BG)
    root.option_add('*TCombobox*Listbox.foreground', TEXT_FG)
    root.option_add('*TCombobox*Listbox.selectBackground', CARD_BG_SEL)
    root.option_add('*TCombobox*Listbox.selectForeground', TEXT_FG)

    style.configure('.', background=CHROME_BG, foreground=TEXT_FG,
                    fieldbackground=CONTROL_BG, bordercolor=BORDER,
                    lightcolor=CHROME_BG, darkcolor=CHROME_BG,
                    focuscolor=CARD_BG_SEL)
    style.configure('TFrame', background=CHROME_BG)
    style.configure('TLabel', background=CHROME_BG, foreground=TEXT_FG)
    style.configure('TPanedwindow', background=CHROME_BG)
    style.configure('Sash', sashthickness=6, gripcount=0)

    style.configure('TButton', background=CONTROL_BG, foreground=TEXT_FG,
                    borderwidth=1, padding=(10, 4))
    style.map('TButton',
              background=[('pressed', CARD_BG_SEL), ('active', CONTROL_ACTIVE)],
              foreground=[('disabled', TEXT_FG_DIM)])

    style.configure('TCheckbutton', background=CHROME_BG, foreground=TEXT_FG,
                    indicatorcolor=CONTROL_BG)
    style.map('TCheckbutton',
              background=[('active', CHROME_BG)],
              indicatorcolor=[('selected', CARD_BG_SEL)])

    style.configure('TEntry', fieldbackground=CONTROL_BG, foreground=TEXT_FG,
                    insertcolor=TEXT_FG, borderwidth=1, padding=3)
    style.configure('TCombobox', fieldbackground=CONTROL_BG,
                    background=CONTROL_BG, foreground=TEXT_FG,
                    arrowcolor=TEXT_FG, borderwidth=1, padding=3)
    style.map('TCombobox',
              fieldbackground=[('readonly', CONTROL_BG)],
              foreground=[('readonly', TEXT_FG)])

    style.configure('Treeview', background=PANEL_BG, fieldbackground=PANEL_BG,
                    foreground=TEXT_FG, borderwidth=0, rowheight=22)
    style.map('Treeview',
              background=[('selected', CARD_BG_SEL)],
              foreground=[('selected', '#ffffff')])
    style.configure('Treeview.Heading', background=CONTROL_BG,
                    foreground=TEXT_FG, borderwidth=1, relief='flat')
    style.map('Treeview.Heading', background=[('active', CONTROL_ACTIVE)])

    for orient in ('Vertical', 'Horizontal'):
        style.configure('%s.TScrollbar' % orient, background=CONTROL_BG,
                        troughcolor=PANEL_BG, bordercolor=PANEL_BG,
                        arrowcolor=TEXT_FG, borderwidth=0)
        style.map('%s.TScrollbar' % orient,
                  background=[('active', CONTROL_ACTIVE)])

    return style


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class FabBrowser:

    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry('1280x820')
        self.root.minsize(760, 480)

        self.settings = load_settings()
        self.cache = ThumbnailCache()
        self.current_dir = None
        self._scan_token = 0
        self._node_seq = 0
        self.node_path = {}      # tree iid -> absolute path
        self.populated = set()   # iids whose children have been read
        self.placeholders = set()

        self.search_var = tk.StringVar()
        self.size_var = tk.IntVar(
            value=int(self.settings.get('thumb_size', DEFAULT_THUMB)))
        self.recursive_var = tk.BooleanVar(
            value=bool(self.settings.get('recursive', False)))
        self.status_var = tk.StringVar(value='Choose a folder of zipped textures to begin.')

        self._build_ui()
        self._enable_drop()

        self._build_tree()
        last = self.settings.get('last_dir')
        if last and os.path.isdir(last):
            self.reveal_path(last)

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # -- ui --------------------------------------------------------------

    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(fill='x')

        ttk.Button(toolbar, text='Go to folder...',
                   command=self.choose_folder).pack(side='left')
        ttk.Button(toolbar, text='Refresh',
                   command=self.refresh).pack(side='left', padx=(6, 12))

        ttk.Label(toolbar, text='Search').pack(side='left')
        search = ttk.Entry(toolbar, textvariable=self.search_var, width=24)
        search.pack(side='left', padx=(4, 12))
        self.search_var.trace_add('write', lambda *_: self._apply_filter())

        ttk.Checkbutton(toolbar, text='Include subfolders',
                        variable=self.recursive_var,
                        command=self.refresh).pack(side='left', padx=(0, 12))

        ttk.Label(toolbar, text='Size').pack(side='left')
        size_box = ttk.Combobox(toolbar, width=5, state='readonly',
                                values=[str(s) for s in THUMB_SIZES])
        size_box.set(str(self.size_var.get()))
        size_box.pack(side='left', padx=(4, 12))
        size_box.bind('<<ComboboxSelected>>',
                      lambda e: self._set_thumb_size(int(size_box.get())))

        ttk.Button(toolbar, text='Clear thumbnail cache',
                   command=self.clear_cache).pack(side='right')

        panes = ttk.PanedWindow(self.root, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=8, pady=(0, 6))

        tree_frame = ttk.Frame(panes)
        self.tree = ttk.Treeview(tree_frame, show='tree', selectmode='browse')
        tvsb = ttk.Scrollbar(tree_frame, orient='vertical',
                             command=self.tree.yview)
        thsb = ttk.Scrollbar(tree_frame, orient='horizontal',
                             command=self.tree.xview)
        self.tree.configure(yscrollcommand=tvsb.set, xscrollcommand=thsb.set)
        # deep trees indent past the pane width, so the tree scrolls sideways
        self.tree.column('#0', width=420, minwidth=160, stretch=False)
        tvsb.pack(side='right', fill='y')
        thsb.pack(side='bottom', fill='x')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<<TreeviewOpen>>', self._on_tree_open)
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        panes.add(tree_frame, weight=1)

        self.grid = PreviewGrid(panes, self.cache,
                                thumb_size=self.size_var.get(),
                                on_select=self._on_card_select,
                                on_activate=self._on_card_activate,
                                on_progress=self._on_progress)
        panes.add(self.grid, weight=4)
        self.panes = panes

        status = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        status.pack(fill='x')
        ttk.Label(status, textvariable=self.status_var, anchor='w').pack(fill='x')

    def _enable_drop(self):
        """Accept a dropped folder if tkinterdnd2 happens to be installed."""
        try:
            from tkinterdnd2 import DND_FILES
        except Exception:
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event):
        raw = event.data.strip()
        if raw.startswith('{') and raw.endswith('}'):
            raw = raw[1:-1]
        path = raw.split('} {')[0].strip()
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if os.path.isdir(path):
            self.reveal_path(path)

    # -- folder tree -----------------------------------------------------

    def _build_tree(self):
        """Seed the tree with every drive (Windows) or / and home."""
        self.tree.delete(*self.tree.get_children())
        self.node_path.clear()
        self.populated.clear()
        self.placeholders.clear()
        for path in list_tree_roots():
            # roots always get an arrow without being scanned, so a slow or
            # disconnected drive never stalls startup
            self._add_node('', path, root_label(path), probe=False)

    def _add_node(self, parent, path, text, probe=True):
        self._node_seq += 1
        iid = 'n%d' % self._node_seq
        self.tree.insert(parent, 'end', iid=iid, text=' ' + text)
        self.node_path[iid] = path
        if not probe or has_subdirectory(path):
            self._add_placeholder(iid)
        return iid

    def _add_placeholder(self, parent):
        self._node_seq += 1
        iid = 'p%d' % self._node_seq
        self.tree.insert(parent, 'end', iid=iid, text='')
        self.placeholders.add(iid)

    def _populate(self, iid, force=False):
        """Read one level of children, once, unless forced to re-read."""
        if iid in self.populated and not force:
            return
        self.populated.add(iid)
        for child in self.tree.get_children(iid):
            if child in self.placeholders or force:
                self.tree.delete(child)
                self.placeholders.discard(child)
                self.node_path.pop(child, None)
                self.populated.discard(child)
        for name, path in subdirectories(self.node_path[iid]):
            self._add_node(iid, path, name)

    def _on_tree_open(self, _event):
        iid = self.tree.focus()
        if iid in self.node_path:
            self._populate(iid)

    def _on_tree_select(self, _event):
        selection = self.tree.selection()
        iid = selection[0] if selection else None
        path = self.node_path.get(iid) if iid else None
        if path and os.path.isdir(path):
            self.show_folder(path)
            self.settings['last_dir'] = path
            save_settings(self.settings)

    def choose_folder(self):
        """Pick a folder in a dialog, then reveal it in the tree."""
        initial = self.current_dir or os.path.expanduser('~')
        chosen = filedialog.askdirectory(title='Go to folder',
                                         initialdir=initial)
        if chosen:
            self.reveal_path(chosen)

    def _root_for(self, path):
        """The tree root that contains path — the longest one that matches."""
        best = None
        lowered = path.lower() if sys.platform == 'win32' else path
        for iid in self.tree.get_children(''):
            root = self.node_path[iid]
            probe = root.lower() if sys.platform == 'win32' else root
            stem = probe.rstrip('\\/')
            if lowered == stem or lowered.startswith(stem + os.sep) or probe == '/':
                if best is None or len(self.node_path[best]) < len(root):
                    best = iid
        return best

    def reveal_path(self, path):
        """Expand the tree down to path and select it."""
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return None
        root = self._root_for(path)
        if root is None:
            return None

        try:
            relative = os.path.relpath(path, self.node_path[root])
        except ValueError:           # different drive on Windows
            return None
        parts = [] if relative == '.' else [
            part for part in relative.split(os.sep) if part not in ('', '.')]
        if any(part == '..' for part in parts):
            return None

        node = root
        for part in parts:
            self._populate(node)
            self.tree.item(node, open=True)
            match = None
            for child in self.tree.get_children(node):
                child_path = self.node_path.get(child)
                if child_path is None:
                    continue
                name = os.path.basename(child_path.rstrip('\\/'))
                if name == part or (sys.platform == 'win32'
                                    and name.lower() == part.lower()):
                    match = child
                    break
            if match is None:
                break                # folder vanished or is unreadable
            node = match

        self.tree.item(node, open=True)
        self._populate(node)
        self.tree.see(node)
        self.tree.selection_set(node)
        self.tree.focus(node)
        return node

    # -- grid ------------------------------------------------------------

    def _archives_in(self, folder, recursive):
        found = []
        if recursive:
            for base, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs
                           if not d.startswith('.') and d != SHARED_CACHE_DIR]
                for name in files:
                    if name.lower().endswith(ARCHIVE_EXTS) and not name.startswith('.'):
                        found.append(os.path.join(base, name))
        else:
            try:
                for entry in os.scandir(folder):
                    if (entry.is_file(follow_symlinks=False)
                            and entry.name.lower().endswith(ARCHIVE_EXTS)
                            and not entry.name.startswith('.')):
                        found.append(entry.path)
            except OSError:
                return []
        found.sort(key=lambda p: os.path.basename(p).lower())
        return found

    def show_folder(self, folder):
        self.current_dir = folder
        self._scan_token += 1
        token = self._scan_token
        recursive = self.recursive_var.get()
        self.status_var.set('Scanning %s ...' % folder)

        def work():
            paths = self._archives_in(folder, recursive)
            self.root.after(0, lambda: deliver(paths))

        def deliver(paths):
            if token != self._scan_token:
                return
            self.grid.set_paths(paths)
            self._apply_filter()

        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        """Re-read the selected folder, on disk and in the tree."""
        selection = self.tree.selection()
        if selection and selection[0] in self.node_path:
            self._populate(selection[0], force=True)
        if self.current_dir:
            self.show_folder(self.current_dir)

    def _apply_filter(self):
        self.grid.apply_filter(self.search_var.get())
        self._on_progress(len(self.grid.items), self.grid.pending)

    def _set_thumb_size(self, size):
        self.size_var.set(size)
        self.settings['thumb_size'] = size
        save_settings(self.settings)
        self.grid.set_thumb_size(size)

    def _on_progress(self, total, pending):
        shown = len(self.grid.visible)
        parts = []
        if self.current_dir:
            parts.append(self.current_dir)
        if total == 0:
            parts.append('no zip archives found')
        elif shown != total:
            parts.append('%d of %d archives' % (shown, total))
        else:
            parts.append('%d archives' % total)
        if pending:
            parts.append('loading %d previews...' % pending)
        self.status_var.set('   -   '.join(parts))

    def _on_card_select(self, item):
        if not item:
            return
        detail = item['name']
        if item['error']:
            detail += '   (%s)' % item['error']
        elif item['member']:
            detail += '   preview: %s' % item['member']
        self.status_var.set(detail)

    def _on_card_activate(self, item):
        DetailWindow(self.root, item['path'])

    def clear_cache(self):
        folders = {os.path.dirname(item['path']) for item in self.grid.items}
        if self.current_dir:
            folders.add(self.current_dir)
        removed = self.cache.clear(folders)
        messagebox.showinfo(
            APP_NAME, 'Removed %d cached thumbnails from your local cache and '
                      'the shared cache of the folders on screen.' % removed)
        self.refresh()

    def _on_close(self):
        self.settings['recursive'] = self.recursive_var.get()
        save_settings(self.settings)
        self.grid.shutdown()
        self.root.destroy()


def main():
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()

    apply_dark_theme(root)
    FabBrowser(root)
    root.mainloop()


if __name__ == '__main__':
    main()
