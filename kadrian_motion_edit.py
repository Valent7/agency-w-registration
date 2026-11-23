"""Speech-directed motion layout for the local vertical Reels renderer."""
from dataclasses import dataclass
from math import cos, pi, sin
import re

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageOps

from kadrian_comets import comet_frame

FPS = 30
SIZE = (540, 960)
CENTER = 270

# First-person speech stays on camera. Concrete lists, instructions and
# conclusions get distinct compositions when those ideas appear in the audio.
TOPICS = {
    'type': r'посчита|вместе|главн|важн|стоп|секрет',
    'list': r'письм|текст|план|отч[её]т|задач',
    'circle': r'агент|помощник|команд|эксперт|специалист',
    'flow': r'результат|получа|говорит|процесс|этап|шаг',
    'action': r'калькулятор|открой|подпиши|зарегистр|скача|переход|ссылк',
}


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Shot:
    start: int
    mode: str
    words: tuple[Word, ...] = ()


def timed_words(transcription):
    words = []
    for segment in transcription:
        current = []
        for token in segment['tokens']:
            text = token['text']
            if not text.strip() or text.startswith('[_'):
                continue
            if text[0].isspace() and current:
                words.append(Word(*current))
                current = []
            start, end = (token['offsets'][side] / 1000 for side in ('from', 'to'))
            if not current:
                current = [start, end, text.strip()]
            else:
                current[1] = max(current[1], end)
                current[2] += text.strip()
        if current:
            words.append(Word(*current))
    return tuple(words)


def direction(words, total):
    motion = {
        mode: tuple(w for w in words if re.match(pattern, w.text.lower()))
        for mode, pattern in TOPICS.items()
    }
    shots = [Shot(0, 'intro'), Shot(min(24, total // 9), 'close')]
    candidates = []
    for mode, matches in motion.items():
        if not matches:
            continue
        word = next((w for w in matches if w.start > 2), matches[0])
        if mode == 'list':
            match = tuple(w for w in matches if word.start <= w.start < word.start + 5)
            if len(match) < 2:
                continue
        elif mode == 'type':
            match = (word,)
        elif mode == 'flow':
            match = tuple(w for w in matches if word.start <= w.start < word.start + 5)
            milestone = next((w for w in match if w.text.lower().startswith('результат')), None)
            if milestone and milestone != word:
                word = milestone
                match = (match[0], milestone)
        elif mode == 'action':
            word = next((w for w in matches if w.text.lower().startswith('калькулятор')), word)
            match = (word,)
        else:
            match = (word,)
        start = round(word.start * FPS)
        if total - start >= 32:
            candidates.append(Shot(start, mode, match))

    for shot in sorted(candidates, key=lambda part: part.start):
        if shot.start - shots[-1].start >= 34:
            shots.append(shot)
    if len(shots) == 2 and words:
        pick = max((w for w in words if .28 * total <= w.start * FPS <= .65 * total),
                   key=lambda w: len(w.text), default=None)
        if pick:
            shots += [Shot(round(pick.start * FPS), 'type', (pick,))]

    timeline = []
    for index, shot in enumerate(shots):
        timeline.append(shot)
        stop = shots[index+1].start if index+1 < len(shots) else total
        hold = {'type': 58, 'list': 146, 'flow': 75, 'circle': 155, 'action': 146}.get(shot.mode)
        if hold and stop - shot.start > hold + 35:
            timeline.append(Shot(shot.start + hold, 'close'))
    return timeline


def caption_groups(words):
    groups, current = [], []
    for word in words:
        if current and (len(current) >= 5 or sum(len(w.text) for w in current) + len(word.text) > 43
                        or word.start - current[-1].end > .46
                        or current[-1].text.endswith(('.', '?', '!', ',')) and len(current) >= 2):
            groups.append(tuple(current))
            current = []
        current.append(word)
    if current:
        groups.append(tuple(current))
    return groups


def _ease(value):
    return 1 - (1 - max(0, min(1, value))) ** 3


def _icon(draw, kind, x, y, ink):
    if kind == 'mail':
        draw.rounded_rectangle((x-17, y-12, x+17, y+12), 4, outline=ink, width=3)
        draw.line((x-16,y-10,x,y+2,x+16,y-10), fill=ink, width=3)
    elif kind == 'file':
        draw.rounded_rectangle((x-13,y-17,x+13,y+17), 4, outline=ink, width=3)
        for offset in (-6, 1, 8):
            draw.line((x-6,y+offset,x+6,y+offset), fill=ink, width=2)
    elif kind == 'calendar':
        draw.rounded_rectangle((x-16,y-14,x+16,y+16), 4, outline=ink, width=3)
        draw.line((x-16,y-4,x+16,y-4), fill=ink, width=3)
        for dx in (-8, 8):
            draw.line((x+dx,y-19,x+dx,y-9), fill=ink, width=3)
    elif kind == 'calculator':
        draw.rounded_rectangle((x-17,y-23,x+17,y+23), 5, outline=ink, width=3)
        draw.rounded_rectangle((x-10,y-16,x+10,y-7), 2, outline=ink, width=2)
        for dx in (-7, 0, 7):
            for dy in (1, 9, 17):
                draw.ellipse((x+dx-1,y+dy-1,x+dx+1,y+dy+1), fill=ink)
    elif kind == 'spark':
        draw.polygon((x,y-17,x+4,y-4,x+17,y,x+4,y+4,x,y+17,x-4,y+4,x-17,y,x-4,y-4), fill=ink)
    else:
        draw.ellipse((x-14,y-14,x+14,y+14), outline=ink, width=3)
        draw.line((x-7,y,x-1,y+6,x+8,y-6), fill=ink, width=3)


def _card_icon(word):
    word = word.lower()
    for pattern, icon in ((r'письм', 'mail'), (r'текст|отч', 'file'), (r'план', 'calendar')):
        if re.match(pattern, word):
            return icon
    return 'check'


def render_frames(root, shots, total, words, palette, still, font, title_cues=(), *, room=None):
    dark, middle, accent, cream = map(ImageColor.getrgb, palette)
    font_cache = {}

    def face(size):
        if size not in font_cache:
            font_cache[size] = font(size)
        return font_cache[size]

    def text(draw, value, x, y, size, fill, width=400, stroke=0):
        rows = []
        for word in value.split():
            previous = rows[-1] if rows else ''
            candidate = (previous + ' ' + word).strip()
            if previous and face(size).getlength(candidate) <= width:
                rows[-1] = candidate
            else:
                rows.append(word)
        if not rows:
            return
        while max(face(size).getlength(row) for row in rows) > width and size > 14:
            size -= 2
        draw.multiline_text((x,y), '\n'.join(rows), font=face(size), fill=fill, anchor='mm',
                            align='center', spacing=5, stroke_width=stroke, stroke_fill=dark)

    gradient = Image.linear_gradient('L').resize(SIZE).rotate(180)
    base = ImageOps.colorize(gradient, dark, middle).convert('RGBA')
    flare = Image.new('RGBA', SIZE)
    ImageDraw.Draw(flare).ellipse((125,210,421,700), fill=(*middle,115))
    base.alpha_composite(flare.filter(ImageFilter.GaussianBlur(74)))
    shade = Image.new('RGBA', SIZE)
    shade.putalpha(Image.linear_gradient('L').resize(SIZE).point(lambda x: max(0, x-124)))
    portrait = ImageOps.fit(still, SIZE, centering=(.5,.43)).convert('RGBA')
    interiors = {}
    if room:
        for mode, center, strength in (('list', (.17,.20), .24), ('flow', (.85,.68), .29),
                                      ('circle', (.8,.33), .17), ('action', (.35,.8), .22),
                                      ('type', (.65,.14), .15)):
            w, h = room.size
            x, y = round(w*.27*center[0]), round(h*.27*center[1])
            detail = room.crop((x,y,x+round(w*.73),y+round(h*.73)))
            detail = ImageOps.fit(detail, SIZE).filter(ImageFilter.GaussianBlur(3))
            interior = Image.blend(base, detail.convert('RGBA'), strength)
            light = Image.new('RGBA', SIZE)
            lx = 60 if mode in {'list','type'} else 445
            ImageDraw.Draw(light).ellipse((lx-147,100,lx+147,490), fill=(255,159,66,75))
            interior.alpha_composite(light.filter(ImageFilter.GaussianBlur(85)))
            interiors[mode] = interior
    gold = (252, 189, 69) if palette[2].upper() == '#F1C77D' else accent
    routes = {
        'circle': (((-40,716),(238,788),(443,645)), ((574,432),(487,199),(411,236))),
        'list': (((575,691),(510,617),(483,477)),),
        'type': (((-45,703),(119,672),(285,566)),),
        'flow': (((-44,683),(97,757),(296,709)),),
        'action': (((578,683),(459,717),(304,620)),),
    }
    groups = caption_groups(words)
    group_index = shot_index = 0
    folder = root / 'motion'
    folder.mkdir()
    for frame in range(total):
        time = frame / FPS
        if shot_index + 1 < len(shots) and frame == shots[shot_index+1].start:
            shot_index += 1
        shot = shots[shot_index]
        age = (frame - shot.start) / FPS
        mode = shot.mode
        art = shade.copy() if mode == 'close' else portrait.copy() if mode == 'intro' else interiors.get(mode, base).copy()
        if mode == 'intro':
            art.alpha_composite(shade)
        draw = ImageDraw.Draw(art)
        illumination = Image.new('RGBA', SIZE)
        glows = ImageDraw.Draw(illumination)
        def bezel(bounds, radius, *, opaque=True):
            x0, y0, x1, y1 = bounds
            glows.rounded_rectangle(bounds, radius, outline=(*gold,235), width=13)
            draw.rounded_rectangle(bounds, radius, fill=dark if opaque else None,
                                   outline=(120,67,23), width=7)
            draw.rounded_rectangle((x0+2,y0+2,x1-2,y1-2), max(1,radius-2), outline=gold, width=3)
            draw.arc((x0+2,y0+2,x0+radius*2,y0+radius*2),180,270,fill=(255,239,180),width=3)
            draw.line((x0+radius,y0+2,x1-radius,y0+2),fill=(255,239,180),width=2)

        if room and mode == 'close':
            bounds = (24,42,516,900) if shot_index in (1,7) else (20,35,520,910)
            bezel(bounds, 30, opaque=False)
        if mode in {'list', 'circle', 'flow', 'action'}:
            for x in (54, 486):
                for y in range(168, 741, 29):
                    draw.ellipse((x,y,x+1,y+1), fill=(*accent,67))

        if mode == 'circle':
            glows.ellipse((138,310,402,574), outline=(*gold,244),width=26)
            draw.ellipse((136,308,404,576), outline=gold, width=7)
            draw.ellipse((142,314,398,570), outline=(255,237,169), width=4)
            for radius, color in ((146, (*accent,74)), (136, (*accent,160)), (127, (*accent,255))):
                draw.ellipse((CENTER-radius,442-radius,CENTER+radius,442+radius), outline=color,
                             width=2 if radius > 127 else 5)
            draw.ellipse((146,318,394,566), fill=(0,0,0,0))
            glows.arc((112,284,428,600), age*97, age*97+93, fill=(*gold,235), width=14)
            draw.arc((112,284,428,600), age*97, age*97+93, fill=(255,234,157), width=5)
            word = shot.words[0].text.strip('.,!?')
            bezel((137,192,403,275), 25)
            text(draw, 'АГЕНТ' if word.lower() in {'агенту','агента','агентом'} else word.upper(),
                 CENTER, 235, 60, cream)
            for offset, icon in enumerate(('spark', 'file', 'check')):
                phase = age * .72 + offset * 2*pi/3
                x, y = CENTER + cos(phase)*177, 442 + sin(phase)*165
                glows.ellipse((x-23,y-23,x+23,y+23),outline=(*gold,210),width=8)
                draw.ellipse((x-22,y-22,x+22,y+22),fill=dark,outline=gold,width=3)
                _icon(draw, icon, x, y, accent)
        elif mode == 'list':
            for index, word in enumerate(shot.words[:3]):
                progress = _ease((time-word.start+.06)/.27)
                if not progress:
                    continue
                x = 88 + 45 * (1-progress)
                y = 365 + 111*index
                bezel((x,y-36,540-x,y+36), 18)
                _icon(draw, _card_icon(word.text), x+43, y, accent)
                text(draw, word.text.strip('.,!?').upper(), CENTER+24, y, 34, cream, 207)
        elif mode == 'flow':
            selected = shot.words[:2]
            for index, word in enumerate(selected):
                reveal = _ease((age - index*.31)/.32)
                if not reveal:
                    continue
                y = 333 + 233*index
                glows.ellipse((232,y-38,308,y+38),outline=(*gold,220),width=10)
                draw.ellipse((232,y-38,308,y+38),fill=dark,outline=gold,width=3)
                _icon(draw, 'spark' if index == 0 else 'check', CENTER, y, accent)
                label = 'ЗАПРОС' if word.text.lower().startswith('говорит') else word.text.strip('.,!?').upper()
                bezel((115,y+43,425,y+107),19)
                text(draw, label, CENTER, y+75, 46, cream, 281)
        elif mode == 'type':
            word = shot.words[0].text.strip('.,!?').upper()
            growth = _ease(age/.35)
            bezel((33,412,507,523),24)
            text(draw, word, CENTER, 467+15*(1-growth), round(42+28*growth), cream, 428)
        elif mode == 'action':
            label = next((line.strip('→↓ ') for line in reversed(title_cues)
                          if re.search(TOPICS['action'], line.lower())), '')
            label = label or shot.words[0].text.strip('.,!?')
            y = 310 + 28*(1-_ease(age/.33))
            glows.rounded_rectangle((218,y-52,322,y+52),23,outline=(*gold,230),width=13)
            draw.rounded_rectangle((218,y-52,322,y+52),23,outline=gold,width=4)
            _icon(draw, 'calculator' if 'калькулятор' in label.lower() else 'check', CENTER, y, accent)
            bezel((39,402,501,584),28)
            text(draw, label.upper(), CENTER, 493, 60, cream, 405)

        if mode != 'intro':
            art.alpha_composite(illumination.filter(ImageFilter.GaussianBlur(11)))
            if mode == 'circle':
                draw.ellipse((146,318,394,566), fill=(0,0,0,0))
        for index, route in enumerate(routes.get(mode, ())):
            progress = (age - .07 - index*.83) / 1.58
            if 0 < progress < 1:
                art.alpha_composite(comet_frame(progress, route))
        if shot_index and age < .23:
            light = Image.new('RGBA', SIZE)
            edge = ImageDraw.Draw(light)
            x = -170 + 880 * _ease(age/.23)
            edge.polygon((x-115,0,x-37,0,x+109,960,x+31,960), fill=(*accent,95))
            art.alpha_composite(light.filter(ImageFilter.GaussianBlur(16)))

        while group_index+1 < len(groups) and groups[group_index+1][0].start <= time:
            group_index += 1
        if groups:
            line = groups[group_index]
            if line[0].start <= time <= max(line[0].start+.37, line[-1].end+.24):
                message = ' '.join(w.text for w in line)
                active = next((w for w in line if w.start <= time < w.end+.025), None)
                if mode == 'close' and active and active == max(line, key=lambda w: len(w.text)) and len(active.text) > 4:
                    emphasis = active.text.strip('.,!?').upper()
                    text(draw, emphasis, CENTER, 658+8*(1-_ease((time-active.start)/.16)),
                         53, cream, 416, stroke=2)
                text(draw, message, CENTER, 778, 28, cream, 412, stroke=2)
        art.save(folder / f'{frame:05d}.png', compress_level=1)
