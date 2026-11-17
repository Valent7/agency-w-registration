"""Bounded HeyGen v3 and Shotstack clients for the studio's durable worker."""
from __future__ import annotations

import math
import re
import requests

from content_factory_live_video import LiveVideoError, LiveVideoUncertain, RunwayVideoClient


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise ValueError('Сервис не вернул корректный номер задачи.')
    return value


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Проверьте текст задания для видеосервиса.')
    return value.strip()


class HeyGenClient:
    def __init__(self, key, session=None):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('Добавьте KADRIAN_HEYGEN_API_KEY в настройки сервера студии.')
        self.session = session or requests.Session()
        self.headers = {'X-Api-Key': key.strip()}

    def _request(self, method, path, *, payload=None, request_id=None):
        headers = dict(self.headers)
        if request_id:
            headers['Idempotency-Key'] = _identifier(request_id)
        try:
            response = self.session.request(method, 'https://api.heygen.com' + path,
                                            headers=headers, json=payload, timeout=(10, 120), allow_redirects=False)
        except requests.RequestException:
            if method == 'POST':
                raise LiveVideoUncertain('HeyGen мог принять заказ. Повторный запуск остановлен.') from None
            raise LiveVideoError('HeyGen временно недоступен.', retryable=True) from None
        try:
            if not 200 <= response.status_code < 300:
                if method == 'POST' and (response.status_code >= 500 or response.status_code == 409):
                    raise LiveVideoUncertain('HeyGen не подтвердил заказ. Проверьте историю перед повтором.')
                raise LiveVideoError('HeyGen отклонил запрос. Проверьте API-доступ, баланс и параметры.',
                                     retryable=method == 'GET' and response.status_code >= 500)
            try:
                body = response.json()
                data = body.get('data', body)
                if isinstance(data, list):
                    return {'items': data, 'next_token': body.get('next_token')}
                if not isinstance(data, dict):
                    raise ValueError
                return data
            except (TypeError, ValueError, AttributeError):
                if method == 'POST':
                    raise LiveVideoUncertain('HeyGen не вернул понятное подтверждение заказа.') from None
                raise LiveVideoError('HeyGen вернул непонятный ответ.', retryable=True) from None
        finally:
            response.close()

    def preflight(self):
        self._request('GET', '/v3/users/me')
        return {'connected': True}

    def avatars(self, token=None):
        from urllib.parse import urlencode
        query = {'ownership': 'public', 'limit': 50}
        if token:
            query['token'] = token
        return self._request('GET', '/v3/avatars/looks?' + urlencode(query))

    def avatar(self, avatar_id):
        return self._request('GET', '/v3/avatars/looks/' + _identifier(avatar_id))

    def voices(self, token=None):
        from urllib.parse import urlencode
        query = {'type': 'public', 'language': 'ru', 'limit': 50}
        if token:
            query['token'] = token
        return self._request('GET', '/v3/models/audio/voices?' + urlencode(query))

    def create(self, spec, request_id):
        payload = heygen_payload(spec)
        path = '/v3/models/videos' if spec['kind'] == 'scene' else '/v3/videos'
        result = self._request('POST', path, payload=payload, request_id=request_id)
        try:
            return _identifier(result.get('video_id', result.get('id')))
        except ValueError:
            raise LiveVideoUncertain('HeyGen принял запрос без номера задачи. Проверьте историю заказов.') from None

    def status(self, task_id, spec):
        path = '/v3/models/videos/' if spec['kind'] == 'scene' else '/v3/videos/'
        return self._request('GET', path + _identifier(task_id))


def heygen_payload(spec):
    kind = spec.get('kind')
    aspect = spec.get('aspect_ratio', '9:16')
    if aspect not in {'9:16', '16:9'}:
        raise ValueError('Выберите вертикальный или горизонтальный формат.')
    if kind == 'scene':
        duration = spec.get('duration')
        if type(duration) is not int or not 5 <= duration <= 30:
            raise ValueError('Сцена HeyGen должна длиться от 5 до 30 секунд.')
        payload = {'model': 'heygen-video-1', 'mode': 'text_to_video',
                   'prompt': _text(spec.get('prompt'), 32000), 'duration': duration,
                   'resolution': '1080p', 'aspect_ratio': aspect, 'prompt_enhancement': 'quality'}
        if spec.get('image'):
            payload.update(mode='image_to_video', image=spec['image'])
        return payload
    speakers = spec.get('speakers')
    dialogue = spec.get('dialogue')
    expected = 1 if kind == 'presenter' else 2 if kind == 'interview' else 0
    if not expected or not isinstance(speakers, list) or len(speakers) != expected:
        raise ValueError('Выберите одного ведущего или двух участников интервью.')
    if not isinstance(dialogue, list) or not 1 <= len(dialogue) <= 50:
        raise ValueError('Нужны реплики для каждого участника.')
    scenes = []
    used = set()
    for line in dialogue:
        index = line.get('speaker')
        if type(index) is not int or not 0 <= index < expected:
            raise ValueError('Проверьте, кому принадлежит каждая реплика.')
        used.add(index)
        person = speakers[index]
        engine = person.get('engine', 'heygen-avatar-6')
        if engine not in {'heygen-avatar-6', 'heygen-avatar-6-turbo', 'avatar_v', 'avatar_iv'}:
            raise ValueError('Неизвестная модель ведущего.')
        scenes.append({'type': 'avatar_video', 'input': {
            'type': 'avatar', 'avatar_id': _identifier(person.get('avatar_id')),
            'voice_id': _identifier(person.get('voice_id')),
            'script': _text(line.get('text'), 5000), 'engine': {'type': engine},
        }})
    if used != set(range(expected)):
        raise ValueError('Добавьте реплики каждого участника интервью.')
    return {'type': 'studio', 'title': _text(spec.get('title', 'Кадриан'), 150),
            'aspect_ratio': aspect, 'resolution': '1080p', 'scenes': scenes}


def shotstack_payload(spec):
    clips = spec.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= 50:
        raise ValueError('Выберите от одного до 50 фрагментов для сборки.')
    timeline = []
    start = 0.0
    for clip in clips:
        seconds = clip.get('duration')
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0.1 <= seconds <= 600:
            raise ValueError('Укажите длительность каждого фрагмента от 0,1 до 600 секунд.')
        # These URLs come from owner-scoped signed media, never from model output.
        source = clip.get('url')
        if not isinstance(source, str) or not source.startswith('https://'):
            raise ValueError('Фрагмент должен иметь временную HTTPS-ссылку хранилища.')
        timeline.append({'asset': {'type': 'video', 'src': source}, 'start': start, 'length': seconds})
        start += seconds
    if start > 1800:
        raise ValueError('Для одной сборки выберите до 30 минут материала.')
    tracks = [{'clips': timeline}]
    if spec.get('music_url'):
        if not isinstance(spec['music_url'], str) or not spec['music_url'].startswith('https://'):
            raise ValueError('Музыка должна иметь HTTPS-ссылку хранилища.')
        volume = spec.get('music_volume', 0.12)
        duration = spec.get('music_duration', start)
        if (isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 1
                or isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration <= 1800):
            raise ValueError('Проверьте громкость и длительность музыки.')
        tracks.append({'clips': [{'asset': {'type': 'audio', 'src': spec['music_url'], 'volume': volume},
                                   'start': 0, 'length': min(start, duration)}]})
    aspect = spec.get('aspect_ratio', '9:16')
    if aspect not in {'9:16', '16:9'}:
        raise ValueError('Выберите вертикальный или горизонтальный формат.')
    return {'timeline': {'tracks': tracks}, 'output': {'format': 'mp4', 'resolution': '1080', 'aspectRatio': aspect}}


class ShotstackClient:
    def __init__(self, key, session=None):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('Добавьте SHOTSTACK_API_KEY в настройки сервера студии.')
        self.session = session or requests.Session()
        self.headers = {'x-api-key': key.strip()}

    def _request(self, method, path, payload=None):
        try:
            response = self.session.request(method, 'https://api.shotstack.io/edit/v1' + path,
                                            headers=self.headers, json=payload, timeout=(10, 120), allow_redirects=False)
        except requests.RequestException:
            if method == 'POST':
                raise LiveVideoUncertain('Shotstack мог принять сборку. Повторный запуск остановлен.') from None
            raise LiveVideoError('Сервис сборки временно недоступен.', retryable=True) from None
        try:
            if not 200 <= response.status_code < 300:
                if method == 'POST' and response.status_code >= 500:
                    raise LiveVideoUncertain('Shotstack не подтвердил создание сборки.')
                raise LiveVideoError('Shotstack отклонил запрос. Проверьте API-доступ, баланс и исходники.',
                                     retryable=method == 'GET' and response.status_code >= 500)
            try:
                body = response.json()['response']
                if not isinstance(body, dict):
                    raise ValueError
                return body
            except (KeyError, ValueError, TypeError):
                if method == 'POST':
                    raise LiveVideoUncertain('Shotstack не вернул номер сборки.') from None
                raise LiveVideoError('Shotstack вернул непонятный ответ.', retryable=True) from None
        finally:
            response.close()

    def create(self, spec, request_id):
        result = self._request('POST', '/render', shotstack_payload(spec))
        try:
            return _identifier(result.get('id'))
        except ValueError:
            raise LiveVideoUncertain('Shotstack не вернул номер сборки.') from None

    def status(self, task_id, spec):
        return self._request('GET', '/render/' + _identifier(task_id))


def provider_client(provider, config):
    if provider == 'heygen':
        return HeyGenClient(config.get('KADRIAN_HEYGEN_API_KEY'))
    if provider == 'shotstack':
        return ShotstackClient(config.get('SHOTSTACK_API_KEY'))
    raise ValueError('Неизвестный производственный сервис Кадриана.')


def download_result(url):
    # Reuse the existing credential-free, SSRF-checked media download path.
    return RunwayVideoClient('download-only').download(url, max_bytes=100 * 1024 * 1024)
