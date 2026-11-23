"""Optional local room replacement for a synchronized presenter video."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from PIL import Image, ImageOps

from kadrian_reels import _run


def room_matte_available():
    return bool(os.environ.get('KADRIAN_PERSON_MATTE') or
                sys.platform == 'darwin' and shutil.which('swiftc'))


def prepare_room_matte(root, room, video, total):
    from io import BytesIO

    binary = os.environ.get('KADRIAN_PERSON_MATTE')
    if not binary:
        if not room_matte_available():
            raise ValueError('Локальная обработка фона доступна при установленном модуле сегментации.')
        source = Path(__file__).with_name('tools') / 'kadrian_person_matte.swift'
        binary = str(root / 'person-matte')
        cache = root.parent / 'kadrian-swift-module-cache'
        subprocess.run(['swiftc','-O','-module-cache-path',str(cache),str(source),'-o',binary],
                       cwd=root, capture_output=True, check=True, timeout=240)

    # Normalize once to the same clock and framing used by the edit.
    raw = root/'matte-input.bgra'
    _run('-i','clip.mp4','-map',f'0:{video}','-an','-vf',
         'fps=30,scale=270:480:force_original_aspect_ratio=increase,crop=270:480,setsar=1',
         '-frames:v',str(total),'-pix_fmt','bgra','-f','rawvideo',raw.name,cwd=root)
    with raw.open('rb') as frames:
        result = subprocess.run([binary,'270','480','presenter.gray'],stdin=frames,cwd=root, check=True,
                                capture_output=True, text=True, timeout=420)
    raw.unlink()
    width, height, count = map(int, result.stdout.split())
    if width < 1 or height < 1 or count != total or (root/'presenter.gray').stat().st_size != width*height*count:
        raise ValueError('Маска ведущего не совпала с кадрами видео.')
    with Image.open(BytesIO(room)) as source:
        image = ImageOps.fit(ImageOps.exif_transpose(source).convert('RGB'), (1080,1920))
        image.save(root/'room.jpg', quality=94)
    return width, height
