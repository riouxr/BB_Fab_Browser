#!/usr/bin/env python3
"""Generate a folder of fake texture-pack zips to try BB Fab Browser on.

Each zip mimics what a real pack looks like: a handful of .jpg texture maps
plus a single .png preview render.  Useful when you want to see the browser
working but your real library is somewhere else.

    python make_sample_library.py             -> ./sample_library
    python make_sample_library.py C:\\somewhere -> that folder
"""

import io
import os
import random
import sys
import zipfile

from PIL import Image, ImageDraw

MAPS = ('Albedo', 'Normal', 'Roughness', 'AO', 'Displacement')

LIBRARY = {
    'Bricks': [('Red Brick Wall', (150, 70, 55)),
               ('Old Brick Herringbone', (120, 60, 50)),
               ('Painted Brick White', (200, 195, 190))],
    'Wood': [('Oak Planks Worn', (140, 100, 60)),
             ('Walnut Parquet', (90, 60, 40)),
             ('Pine Boards Raw', (190, 150, 100)),
             ('Driftwood Grey', (130, 130, 120))],
    'Metal': [('Rusted Steel Plate', (130, 80, 50)),
              ('Brushed Aluminium', (170, 172, 175)),
              ('Painted Metal Blue', (50, 80, 140))],
    'Fabric': [('Linen Natural', (205, 195, 170)),
               ('Velvet Deep Green', (30, 70, 50)),
               ('Denim Indigo', (60, 75, 110))],
    'Stone': [('Granite Polished', (140, 138, 135)),
              ('Slate Roof Tiles', (70, 75, 80)),
              ('Marble Carrara', (225, 225, 220))],
}


def noisy(size, color, label=None):
    img = Image.new('RGB', size, color)
    draw = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = random.randrange(size[0]), random.randrange(size[1])
        r = random.randrange(6, max(8, size[0] // 8))
        draw.ellipse((x, y, x + r, y + r),
                     fill=tuple(max(0, min(255, c + random.randrange(-45, 45)))
                                for c in color))
    if label:
        draw.rectangle((0, size[1] - 30, size[0], size[1]), fill=(0, 0, 0))
        draw.text((8, size[1] - 20), label, fill=(255, 255, 255))
    return img


def encode(img, fmt):
    buf = io.BytesIO()
    img.save(buf, fmt, quality=80) if fmt == 'JPEG' else img.save(buf, fmt)
    return buf.getvalue()


def main(root='sample_library'):
    random.seed(1)
    made = 0
    for category, packs in LIBRARY.items():
        folder = os.path.join(root, category)
        os.makedirs(folder, exist_ok=True)
        for name, color in packs:
            slug = name.replace(' ', '_')
            target = os.path.join(folder, slug + '_4K.zip')
            with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
                for kind in MAPS:
                    tint = color if kind == 'Albedo' else (128, 128, 128)
                    if kind == 'Normal':
                        tint = (128, 128, 255)
                    zf.writestr('%s/%s_%s.jpg' % (slug, slug, kind),
                                encode(noisy((256, 256), tint), 'JPEG'))
                zf.writestr('%s_Preview.png' % slug,
                            encode(noisy((512, 512), color, name), 'PNG'))
            made += 1
    print('Created %d sample packs in %s' % (made, os.path.abspath(root)))
    print('Now run:  python bb_fab_browser.py')
    print('and point "Open folder..." at that folder.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'sample_library')
