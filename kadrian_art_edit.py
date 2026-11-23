"""Local 9:16 editorial montage using an existing synchronized presenter clip."""
from io import BytesIO
from pathlib import Path
import json
import re
import subprocess
from tempfile import TemporaryDirectory
import wave
from array import array
from math import exp, pi, sin

from PIL import Image, ImageFont, ImageOps
from difflib import SequenceMatcher

from kadrian_motion_edit import Word, direction, render_frames, timed_words
from kadrian_live_draft import _local_program
from kadrian_reels import _run, _stream
from kadrian_room_matte import prepare_room_matte

PALETTES = {
    'Синий и золото · Агентство W': ('#081427', '#173653', '#F1C77D', '#FFF6E7'),
    'Тёмно-зелёный и лайм': ('#101B16', '#2C4334', '#D5F06F', '#FFF7E9'),
    'Бордовый и медь': ('#231523', '#4C2E41', '#F1A981', '#FFF7EC'),
}


def _transcribe_words(root, script):
    engine = _local_program('KADRIAN_WHISPER_CLI', 'KADRIAN_WHISPER_MODEL')
    if not engine:
        raise ValueError('Для синхронизации графики нужен локальный модуль расшифровки.')
    executable, model = engine
    _run('-protocol_whitelist','file,pipe','-i','voice','-ar','16000','-ac','1','speech.wav',cwd=root)
    subprocess.run([executable, '-m', model, '-f', 'speech.wav', '-l', 'ru',
                    '-ojf', '-ml', '52', '-sow', '-of', 'speech', '-np'],
                   cwd=root, check=True, capture_output=True, timeout=180)
    words = timed_words(json.loads((root/'speech.json').read_text())['transcription'])
    if not words:
        raise ValueError('Не удалось выделить слова в записи речи.')
    if not script:
        return words
    approved = script.split()
    normalize = lambda text: re.sub(r'\W', '', text.lower())
    spans = SequenceMatcher(None, [normalize(w.text) for w in words], [normalize(w) for w in approved])
    revised = list(words)
    for kind, first, end, other, last in spans.get_opcodes():
        if kind == 'replace' and end-first == last-other:
            for index, candidate in zip(range(first, end), approved[other:last]):
                word = words[index]
                if SequenceMatcher(None, normalize(word.text), normalize(candidate)).ratio() >= .72:
                    revised[index] = Word(word.start, word.end, candidate)
    return tuple(revised)


def _font(size, bold=True):
    directory = Path('/System/Library/Fonts/Supplemental')
    path = directory / ('Arial Bold.ttf' if bold else 'Arial.ttf')
    return ImageFont.truetype(str(path) if path.is_file() else ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'), size)


def _music(root, seconds):
    rate = 24000
    samples = array('f', [0]) * round(rate * seconds)
    beat = .70
    chords = ((50, 57, 65, 69), (46, 53, 62, 65), (53, 60, 64, 69), (48, 55, 62, 67))
    melodies = ((74, 72, 69), (72, 69, 65), (69, 72, 76), (74, 67, 69))

    def add_note(start, note, level):
        offset = round(start * rate)
        angle = 2 * pi * 440 * 2 ** ((note - 69) / 12) / rate
        for index in range(min(round(beat * 2.4 * rate), len(samples) - offset)):
            t = index / rate
            envelope = min(1, t / .012) * exp(-3.6 * t) * min(1, (beat * 2.4 - t) / .12)
            phase = index * angle
            tone = sin(phase) + .24 * exp(-2 * t) * sin(2 * phase) + .08 * exp(-4 * t) * sin(3 * phase)
            samples[offset + index] += level * envelope * tone

    for bar in range(round(seconds / (4 * beat)) + 1):
        start = bar * 4 * beat
        for step, note in enumerate(chords[bar % len(chords)]):
            add_note(start + step * beat, note, 1700 if step == 0 else 1000)
        for step, note in zip((.5, 1.5, 3), melodies[bar % len(melodies)]):
            add_note(start + step * beat, note, 1450)
    pcm = array('h', (
        int(value * min(1, index / (.12 * rate), (len(samples) - index) / (1.3 * rate)))
        for index, value in enumerate(samples)
    ))
    with wave.open(str(root/'bed.wav'),'wb') as out:
        out.setparams((1,2,rate,0,'NONE','not compressed'))
        out.writeframes(pcm.tobytes())


def render_art_edit(clip, voice, still, palette_name, title_cues=(), *, with_music=True, approved_script="", room=None):
    """Return a finished MP4 from local input bytes. The clip must already match the voice."""
    palette = PALETTES[palette_name]
    with TemporaryDirectory(prefix='kadrian-art-') as folder:
        root=Path(folder)
        (root/'clip.mp4').write_bytes(clip)
        (root/'voice').write_bytes(voice)
        vid, length = _stream(root/'clip.mp4','video',{'mp4','mov'})
        aid, duration = _stream(root/'voice','audio',{'mp3','wav','mp4','mov'})
        if abs(length-duration) > .20 or duration < 8:
            raise ValueError('Для художественного монтажа нужен синхронный клип той же длины, что и запись речи, от 8 секунд.')
        words = _transcribe_words(root, approved_script)
        total = round(min(length, duration) * 30)
        shots = direction(words, total)
        with Image.open(BytesIO(still)) as source:
            render_frames(root, shots, total, words, palette, ImageOps.exif_transpose(source),
                          _font, title_cues)
        args = ['-filter_complex_threads','2','-protocol_whitelist','file,pipe','-i','clip.mp4',
                '-protocol_whitelist','file,pipe','-i','voice',
                '-framerate','30','-i','motion/%05d.png']
        camera = [(i, shot) for i, shot in enumerate(shots) if shot.mode in ('close', 'circle')]
        inputs = ''.join(f'[cam{i}]' for i, _ in camera)
        split = f',split={len(camera)}' if len(camera) > 1 else ''
        graph = []
        picture = (f'[0:{vid}]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,'
                   'crop=1080:1920,setsar=1')
        next_input = 3
        if room:
            width, height = prepare_room_matte(root, room, vid, total)
            args += ['-f','rawvideo','-pixel_format','gray','-video_size',f'{width}x{height}',
                     '-framerate','30','-i','presenter.gray','-loop','1','-framerate','30','-i','room.jpg']
            room_inputs = ''.join(f'[room{i}]' for i, _ in camera)
            graph += [picture+'[live]',
                      "[3:v]scale=1080:1920:flags=bilinear,format=gray,lut=y='clip((val-23)*255/222,0,255)'[alpha]",
                      f'[live][alpha]alphamerge{split}{inputs}',
                      f'[4:v]setsar=1{split}{room_inputs}']
            next_input = 5
        else:
            graph.append(picture+split+inputs)
        for i, shot in enumerate(shots):
            end = shots[i+1].start if i+1 < len(shots) else total
            count = end - shot.start
            if shot.mode in ('close','circle'):
                label = f'presenter{i}' if room else f'v{i}'
                trim = f'[cam{i}]trim=start_frame={shot.start}:end_frame={end},setpts=PTS-STARTPTS,'
                if shot.mode == 'close':
                    graph.append(trim+f'crop=486:864:296:0,scale=1080:1920,setsar=1[{label}]')
                else:
                    graph.append(trim+
                                 f'crop=540:540:270:0,scale=496:496,pad=1080:1920:292:636:color=0x00000000,setsar=1[{label}]')
                if room:
                    graph += [f'[room{i}]trim=end_frame={count},setpts=PTS-STARTPTS[plate{i}]',
                              f'[plate{i}][{label}]overlay=shortest=1,format=yuv420p[v{i}]']
            else:
                graph.append(f'color=c=black:s=1080x1920:r=30,trim=end_frame={count}[v{i}]')
        graph += [''.join(f'[v{i}]' for i in range(len(shots))) + f'concat=n={len(shots)}:v=1:a=0[base]',
                  '[2:v]scale=1080:1920:flags=lanczos[graphics]',
                  '[base][graphics]overlay=shortest=1,format=yuv420p[out]']
        if with_music:
            _music(root, duration)
            args += ['-i','bed.wav']
            graph += [f'[1:{aid}]aresample=48000,aformat=channel_layouts=stereo,asplit[voice][key]',
                      f'[{next_input}:a]aresample=48000,aformat=channel_layouts=stereo,volume=.22[bed]',
                      '[bed][key]sidechaincompress=threshold=.025:ratio=6:attack=15:release=230[duck]',
                      '[voice][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-18:TP=-1.8:LRA=9,'
                      'aresample=48000[a]']
            audio = ['-map','[a]','-c:a','aac','-b:a','192k']
        else:
            audio = ['-map',f'1:{aid}','-c:a','aac','-b:a','192k']
        _run(*args,'-filter_complex',';'.join(graph),'-map','[out]',*audio,'-c:v','libx264',
             '-threads','4','-preset','fast','-crf','19','-movflags','+faststart','reels.mp4',cwd=root)
        _run('-xerror','-i','reels.mp4','-f','null','-',cwd=root)
        return (root/'reels.mp4').read_bytes()
