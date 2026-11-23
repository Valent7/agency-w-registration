"""Render timed, transparent gold accents for the editorial Reels layout."""
from math import hypot, sin
from random import Random

from PIL import Image, ImageDraw, ImageFilter

SIZE = (540, 960)


def comet_frame(progress, path):
    def point(u):
        u = max(0, min(1, u))
        a, b, c = path
        return tuple((1-u)**2 * a[i] + 2*(1-u)*u*b[i] + u*u*c[i] for i in range(2))

    opacity = min(1, progress / .18, (1-progress) / .24)
    ink = Image.new('RGBA', SIZE)
    draw = ImageDraw.Draw(ink)
    for index in reversed(range(52)):
        weight = 1 - index / 52
        x, y = point(progress - index * .010)
        radius = 1.5 + 13 * weight ** 1.4
        draw.ellipse((x-radius,y-radius,x+radius,y+radius),
                     fill=(253, 158+int(53*weight), 38+int(51*weight), int(253 * weight * opacity)))
    for index in range(55):
        rng = Random(index)
        trail = rng.uniform(.03, .46)
        drift = rng.uniform(-1, 1) * (8 + 61 * trail)
        x, y = point(progress - trail)
        x += drift
        y += sin(index) * drift
        r = rng.uniform(.8, 2.6)
        flicker = .55 + .45 * sin(progress * 22 + index) ** 2
        draw.ellipse((x-r, y-r, x+r, y+r), fill=(255, 203, 104, int(190 * opacity * flicker)))
    x, y = point(progress)
    px, py = point(progress - .025)
    length = hypot(x-px, y-py) or 1
    dx, dy = (x-px)/length, (y-py)/length

    def vertex(forward, side):
        return x+dx*forward-dy*side, y+dy*forward+dx*side

    profile = ((28,0),(-19,-23),(-10,-7),(-31,0),(-10,7),(-19,23))
    draw.polygon([vertex(forward, side) for forward, side in profile],
                 fill=(255, 189, 58, int(250 * opacity)))
    draw.polygon([vertex(forward, side) for forward, side in ((24,0),(-12,-14),(-3,0),(-12,14))],
                 fill=(255, 244, 180, int(252 * opacity)))
    halo = ink.filter(ImageFilter.GaussianBlur(13))
    halo.alpha_composite(ink.filter(ImageFilter.GaussianBlur(4)))
    return Image.alpha_composite(halo, ink)
