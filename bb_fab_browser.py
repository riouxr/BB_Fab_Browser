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


class ThumbnailCache:
    """Disk-backed thumbnail cache keyed on archive path + mtime + size."""

    def __init__(self, directory=None):
        self.dir = directory or user_cache_dir()
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            self.dir = None

    def _key(self, path, stat, thumb_size):
        raw = '%s|%d|%d|%d' % (os.path.abspath(path), stat.st_mtime_ns,
                               stat.st_size, thumb_size)
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()

    def _file(self, key):
        return os.path.join(self.dir, key[:2], key + '.png')

    def get(self, path, stat, thumb_size):
        if not self.dir:
            return None
        try:
            with Image.open(self._file(self._key(path, stat, thumb_size))) as img:
                return img.copy()
        except Exception:
            return None

    def put(self, path, stat, thumb_size, image):
        if not self.dir:
            return
        try:
            target = self._file(self._key(path, stat, thumb_size))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            image.save(target, 'PNG')
        except Exception:
            pass

    def clear(self):
        if not self.dir or not os.path.isdir(self.dir):
            return 0
        removed = 0
        for root, _dirs, files in os.walk(self.dir):
            for name in files:
                if name.endswith('.png'):
                    try:
                        os.remove(os.path.join(root, name))
                        removed += 1
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


def list_archive_contents(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return [(i.filename, i.file_size) for i in zf.infolist()
                    if not i.filename.endswith('/')]
    except Exception:
        return []


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
    """Large preview plus the archive's file listing."""

    MAX_PREVIEW = 720

    def __init__(self, master, path):
        super().__init__(master)
        self.path = path
        self.title(os.path.basename(path))
        self.geometry('1000x700')
        self.configure(bg=CHROME_BG)
        self.photo = None

        panes = ttk.PanedWindow(self, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=8, pady=8)

        left = ttk.Frame(panes)
        self.image_label = tk.Label(left, bg=GRID_BG, text='Loading preview...',
                                    fg=TEXT_FG_DIM)
        self.image_label.pack(fill='both', expand=True)
        panes.add(left, weight=3)

        right = ttk.Frame(panes)
        ttk.Label(right, text='Archive contents').pack(anchor='w', pady=(0, 4))
        columns = ('size',)
        self.tree = ttk.Treeview(right, columns=columns, show='tree headings')
        self.tree.heading('#0', text='File')
        self.tree.heading('size', text='Size')
        self.tree.column('size', width=90, anchor='e', stretch=False)
        vsb = ttk.Scrollbar(right, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        panes.add(right, weight=2)

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Button(bar, text='Show in file manager',
                   command=lambda: reveal_in_file_manager(self.path)).pack(side='left')
        ttk.Button(bar, text='Copy path',
                   command=self._copy_path).pack(side='left', padx=6)
        ttk.Button(bar, text='Save preview as...',
                   command=self._save_preview).pack(side='left')
        ttk.Button(bar, text='Close', command=self.destroy).pack(side='right')

        self.status = ttk.Label(self, text='', anchor='w')
        self.status.pack(fill='x', padx=8, pady=(0, 6))

        self.bind('<Escape>', lambda e: self.destroy())
        self.after(10, self._populate)

    def _populate(self):
        contents = list_archive_contents(self.path)
        for name, size in sorted(contents, key=lambda c: c[0].lower()):
            self.tree.insert('', 'end', text=name, values=(self._human(size),))

        self.preview_image = None
        try:
            with zipfile.ZipFile(self.path) as zf:
                member = find_preview_member(zf)
                if member is not None:
                    data = zf.read(member)
                    with Image.open(io.BytesIO(data)) as img:
                        img.load()
                        self.preview_image = img.copy()
                    self.status.configure(
                        text='%s  -  preview: %s  (%d x %d)'
                             % (self.path, member.filename,
                                self.preview_image.width,
                                self.preview_image.height))
        except Exception as exc:
            self.status.configure(text='%s  -  %s' % (self.path, exc))

        if self.preview_image is None:
            self.image_label.configure(text='No preview image in this archive')
            if not self.status.cget('text'):
                self.status.configure(text=self.path)
            return

        shown = make_thumbnail(self.preview_image, self.MAX_PREVIEW, bg=GRID_BG)
        self.photo = ImageTk.PhotoImage(shown)
        self.image_label.configure(image=self.photo, text='')

    @staticmethod
    def _human(size):
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024 or unit == 'GB':
                return '%.0f %s' % (size, unit) if unit == 'B' else '%.1f %s' % (size, unit)
            size /= 1024.0
        return '%d B' % size

    def _copy_path(self):
        self.clipboard_clear()
        self.clipboard_append(self.path)

    def _save_preview(self):
        if self.preview_image is None:
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            defaultextension='.png',
            initialfile=os.path.splitext(os.path.basename(self.path))[0] + '_preview.png',
            filetypes=[('PNG image', '*.png'), ('JPEG image', '*.jpg')])
        if not target:
            return
        try:
            image = self.preview_image
            if target.lower().endswith(('.jpg', '.jpeg')):
                image = image.convert('RGB')
            image.save(target)
        except Exception as exc:
            messagebox.showerror(APP_NAME, 'Could not save preview:\n%s' % exc,
                                 parent=self)


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
        self.root_dir = None
        self.current_dir = None
        self._scan_token = 0
        self._dummy_id = 0

        self.search_var = tk.StringVar()
        self.size_var = tk.IntVar(
            value=int(self.settings.get('thumb_size', DEFAULT_THUMB)))
        self.recursive_var = tk.BooleanVar(
            value=bool(self.settings.get('recursive', False)))
        self.status_var = tk.StringVar(value='Choose a folder of zipped textures to begin.')

        self._build_ui()
        self._enable_drop()

        last = self.settings.get('root_dir')
        if last and os.path.isdir(last):
            self.set_root(last)

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # -- ui --------------------------------------------------------------

    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(fill='x')

        ttk.Button(toolbar, text='Open folder...',
                   command=self.choose_root).pack(side='left')
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
        self.tree.configure(yscrollcommand=tvsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        tvsb.pack(side='right', fill='y')
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
            self.set_root(path)

    # -- folder tree -----------------------------------------------------

    def choose_root(self):
        initial = self.root_dir or os.path.expanduser('~')
        chosen = filedialog.askdirectory(title='Choose a folder of texture zips',
                                         initialdir=initial)
        if chosen:
            self.set_root(chosen)

    def set_root(self, path):
        self.root_dir = os.path.abspath(path)
        self.tree.delete(*self.tree.get_children())
        label = os.path.basename(self.root_dir) or self.root_dir
        node = self.tree.insert('', 'end', iid=self.root_dir, text=label,
                                open=True)
        self._populate_node(node)
        self.tree.selection_set(node)
        self.tree.focus(node)
        self.settings['root_dir'] = self.root_dir
        save_settings(self.settings)

    def _subdirs(self, path):
        try:
            entries = [e for e in os.scandir(path)
                       if e.is_dir(follow_symlinks=False)
                       and not e.name.startswith('.')]
        except OSError:
            return []
        entries.sort(key=lambda e: e.name.lower())
        return entries

    def _populate_node(self, node):
        """Fill a node with its subdirectories, one level deep."""
        for child in self.tree.get_children(node):
            self.tree.delete(child)
        for entry in self._subdirs(node):
            child = self.tree.insert(node, 'end', iid=entry.path, text=entry.name)
            if self._subdirs(entry.path):
                # placeholder child so the node shows an expand arrow; the real
                # children are read when the user opens it
                self._dummy_id += 1
                self.tree.insert(child, 'end',
                                 iid='\u2400dummy%d' % self._dummy_id, text='')

    def _on_tree_open(self, _event):
        node = self.tree.focus()
        children = self.tree.get_children(node)
        if len(children) == 1 and children[0].startswith('\u2400dummy'):
            self._populate_node(node)

    def _on_tree_select(self, _event):
        selection = self.tree.selection()
        node = selection[0] if selection else self.tree.focus()
        if node and os.path.isdir(node):
            self.show_folder(node)

    # -- grid ------------------------------------------------------------

    def _archives_in(self, folder, recursive):
        found = []
        if recursive:
            for base, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
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
        removed = self.cache.clear()
        messagebox.showinfo(APP_NAME, 'Removed %d cached thumbnails.' % removed)
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
