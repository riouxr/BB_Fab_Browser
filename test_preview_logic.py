#!/usr/bin/env python3
"""Self-test for the archive-scanning half of BB Fab Browser.

Builds a throwaway set of texture zips and checks that the right image is
picked out of each one.  No GUI and no dependencies beyond Pillow:

    python test_preview_logic.py
"""

import io
import os
import shutil
import tempfile
import zipfile

from PIL import Image

import bb_fab_browser as bb


def png_bytes(size=(64, 64), color=(120, 120, 120)):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'PNG')
    return buf.getvalue()


def jpg_bytes(size=(64, 64), color=(120, 120, 120)):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'JPEG')
    return buf.getvalue()


def build_zip(path, members):
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


CASES = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


@case('the single png wins over a pile of jpg maps')
def _(tmp):
    path = build_zip(os.path.join(tmp, 'a.zip'), [
        ('Rock/Rock_Albedo.jpg', jpg_bytes()),
        ('Rock/Rock_Normal.jpg', jpg_bytes()),
        ('Rock/Rock_Roughness.jpg', jpg_bytes()),
        ('Rock_Preview.png', png_bytes((256, 256))),
    ])
    with zipfile.ZipFile(path) as zf:
        assert bb.find_preview_member(zf).filename == 'Rock_Preview.png'


@case('__MACOSX resource forks are ignored')
def _(tmp):
    path = build_zip(os.path.join(tmp, 'b.zip'), [
        ('__MACOSX/._Preview.png', b'junk'),
        ('Preview.png', png_bytes()),
    ])
    with zipfile.ZipFile(path) as zf:
        assert bb.find_preview_member(zf).filename == 'Preview.png'


@case('a png named like a texture map loses to a png named preview')
def _(tmp):
    path = build_zip(os.path.join(tmp, 'c.zip'), [
        ('tex_Normal.png', png_bytes((512, 512))),
        ('preview.png', png_bytes((128, 128))),
    ])
    with zipfile.ZipFile(path) as zf:
        assert bb.find_preview_member(zf).filename == 'preview.png'


@case('a shallow png beats a deeply nested one when neither is hinted')
def _(tmp):
    path = build_zip(os.path.join(tmp, 'd.zip'), [
        ('deep/deeper/deepest/image.png', png_bytes()),
        ('image.png', png_bytes()),
    ])
    with zipfile.ZipFile(path) as zf:
        assert bb.find_preview_member(zf).filename == 'image.png'


@case('falls back to a jpg when the archive has no png at all')
def _(tmp):
    path = build_zip(os.path.join(tmp, 'e.zip'), [
        ('thumbnail.jpg', jpg_bytes()),
        ('notes.txt', b'hello'),
    ])
    with zipfile.ZipFile(path) as zf:
        assert bb.find_preview_member(zf).filename == 'thumbnail.jpg'


@case('an archive with no images at all reports none')
def _(tmp):
    path = build_zip(os.path.join(tmp, 'f.zip'), [('readme.txt', b'nothing')])
    with zipfile.ZipFile(path) as zf:
        assert bb.find_preview_member(zf) is None


@case('a corrupt archive returns an error instead of raising')
def _(tmp):
    path = os.path.join(tmp, 'g.zip')
    with open(path, 'wb') as fh:
        fh.write(b'definitely not a zip')
    cache = bb.ThumbnailCache(os.path.join(tmp, 'cache'))
    thumb, member, error = bb.load_preview(path, 128, cache)
    assert thumb is None and error, (thumb, error)


@case('thumbnails come back at the requested size and are cached')
def _(tmp):
    path = build_zip(os.path.join(tmp, 'h.zip'),
                     [('preview.png', png_bytes((800, 400)))])
    cache = bb.ThumbnailCache(os.path.join(tmp, 'cache'))
    thumb, member, error = bb.load_preview(path, 128, cache)
    assert error is None and member == 'preview.png'
    assert max(thumb.size) == 128, thumb.size

    # second read is served from disk, so it reports no member
    again, member2, error2 = bb.load_preview(path, 128, cache)
    assert error2 is None and member2 is None and again.size == thumb.size

    # editing the archive must invalidate the cached thumbnail
    build_zip(path, [('preview.png', png_bytes((400, 800)))])
    os.utime(path, (0, 0))
    fresh, member3, _ = bb.load_preview(path, 128, cache)
    assert member3 == 'preview.png', 'stale thumbnail served after edit'


@case('alpha previews are flattened, never left as RGBA')
def _(tmp):
    buf = io.BytesIO()
    Image.new('RGBA', (100, 100), (255, 0, 0, 90)).save(buf, 'PNG')
    path = build_zip(os.path.join(tmp, 'i.zip'), [('preview.png', buf.getvalue())])
    cache = bb.ThumbnailCache(os.path.join(tmp, 'cache'))
    thumb, _member, error = bb.load_preview(path, 96, cache)
    assert error is None and thumb.mode == 'RGB', thumb.mode


@case('subdirectories lists only folders, sorted, dotfiles skipped')
def _(tmp):
    base = os.path.join(tmp, 'tree')
    for name in ('Zebra', 'apple', '.hidden', 'Mango'):
        os.makedirs(os.path.join(base, name), exist_ok=True)
    with open(os.path.join(base, 'loose.txt'), 'w') as fh:
        fh.write('not a folder')
    names = [name for name, _path in bb.subdirectories(base)]
    assert names == ['apple', 'Mango', 'Zebra'], names


@case('subdirectories returns empty for missing or unreadable paths')
def _(tmp):
    assert bb.subdirectories(os.path.join(tmp, 'does-not-exist')) == []
    assert bb.subdirectories(os.path.join(tmp, 'tree', 'apple')) == []


@case('has_subdirectory agrees with subdirectories')
def _(tmp):
    base = os.path.join(tmp, 'tree')
    assert bb.has_subdirectory(base) is True
    assert bb.has_subdirectory(os.path.join(base, 'apple')) is False
    assert bb.has_subdirectory(os.path.join(tmp, 'nope')) is False


@case('a folder holding only dotfolders reports no children')
def _(tmp):
    base = os.path.join(tmp, 'dotty')
    os.makedirs(os.path.join(base, '.git'), exist_ok=True)
    assert bb.has_subdirectory(base) is False
    assert bb.subdirectories(base) == []


@case('tree roots exist and are absolute')
def _(tmp):
    roots = bb.list_tree_roots()
    assert roots, 'no filesystem roots reported'
    for root in roots:
        assert os.path.isabs(root), root


def main():
    tmp = tempfile.mkdtemp(prefix='bbfab-test-')
    failures = 0
    try:
        for name, fn in CASES:
            try:
                fn(tmp)
            except AssertionError as exc:
                failures += 1
                print('FAIL  %s\n      %s' % (name, exc))
            except Exception as exc:
                failures += 1
                print('ERROR %s\n      %r' % (name, exc))
            else:
                print('ok    %s' % name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n%d passed, %d failed' % (len(CASES) - failures, failures))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
