"""Resume provider jobs on the existing leased worker, independently of the UI."""
from copy import deepcopy

from content_factory_live_video import LiveVideoError, LiveVideoPending, LiveVideoUncertain
from kadrian_providers import provider_client, heygen_payload, shotstack_payload, download_result


def resolve_spec(store, job):
    spec = deepcopy(job.get('provider_spec') or {})
    if job['kadrian_provider'] != 'shotstack':
        reference = spec.pop('reference_asset', None)
        if reference:
            from content_factory_live_video import _source_image
            _check_asset(job, reference)
            encoded = _source_image(store.read_asset(job['owner_id'], reference)).split(',', 1)[1]
            spec['image'] = {'type': 'base64', 'media_type': 'image/jpeg', 'data': encoded}
        return spec
    clips = []
    for clip in spec.get('clips') or []:
        asset = clip.get('asset') or {}
        _check_asset(job, asset)
        resolved = store.resolve_assets(job['owner_id'], [asset])[0]
        clips.append({'url': resolved['url'], 'duration': clip['duration']})
    spec['clips'] = clips
    if spec.get('music_asset'):
        _check_asset(job, spec['music_asset'])
        spec['music_url'] = store.resolve_assets(job['owner_id'], [spec.pop('music_asset')])[0]['url']
    return spec


def _check_asset(job, asset):
    parts = str(asset.get('storage_path') or '').split('/')
    if len(parts) < 4 or parts[:2] != [str(job['owner_id']), job['project_id']]:
        raise ValueError('Исходник не принадлежит выбранному проекту.')


def process_provider_job(worker, job, project):
    from content_factory_runtime import _configuration

    provider = job.get('kadrian_provider')
    client = provider_client(provider, _configuration())
    production = dict(job.get('production') or {})
    task_id = production.get('task_id')
    if not task_id:
        if production.get('starting') or production.get('creation_uncertain'):
            raise LiveVideoUncertain('Заказ мог быть принят сервисом. Проверьте его историю перед новым запуском.')
        spec = resolve_spec(worker.store, job)
        if provider == 'heygen':
            heygen_payload(spec)
        else:
            shotstack_payload(spec)
        from content_factory_billing import billing_enforced, reserve_usage
        from content_factory_core import iso
        if billing_enforced():
            cents = job.get('approved_quote_cents')
            if type(cents) is not int or cents <= 0:
                raise ValueError('Сначала согласуйте стоимость в валюте баланса Агентства.')
            reference = 'video:' + job['id']
            reserve_usage(worker.store, job['owner_id'], job['project_id'], cents,
                          reference=reference, kind='video', label='Кадриан · ' + provider,
                          now=iso(worker.clock()))
            worker._update(job, billing_reference=reference)
        production = {'provider': provider, 'starting': True}
        worker._update(job, production=production)
        task_id = client.create(spec, job['id'])
        production.update(task_id=task_id, starting=False)
        worker._update(job, production=production,
                       progress={'stage': 'provider_wait', 'message': 'Сервис создаёт согласованное видео.'})
    result = client.status(task_id, job.get('provider_spec') or {})
    status = result.get('status')
    if status in {'failed', 'cancelled'}:
        raise LiveVideoError('Сервис остановил создание видео. Откройте его историю для проверки причины.')
    if status not in {'completed', 'done'}:
        raise LiveVideoPending('Видео ещё создаётся в ' + ('HeyGen.' if provider == 'heygen' else 'Shotstack.'), retry_after=60)
    url = result.get('video_url') or result.get('url')
    if not url:
        raise LiveVideoError('Сервис завершил работу, но файл ещё недоступен.', retryable=True)
    data = download_result(url)
    asset = worker.store.put_asset(job['owner_id'], job['project_id'], job['id'], 'kadrian.mp4', data, 'video/mp4')
    from content_factory_billing import billing_enforced, settle_usage
    from content_factory_core import iso
    checkpoint = {'generation_complete': True}
    worker._update(job, assets=[asset], checkpoint=checkpoint)
    if billing_enforced():
        settle_usage(worker.store, job['owner_id'], job['project_id'],
                     reference='video:' + job['id'], now=iso(worker.clock()))
        checkpoint['billing_settled'] = True
    worker._finish(job, state='ready', assets=[asset], production=production,
                   content={'title': str(job.get('topic') or 'Кадриан')[:150],
                            'text': str(job.get('topic') or 'Готовое видео'), 'video_style': job.get('video_style', 'live')},
                   checkpoint=checkpoint, error='',
                   progress={'stage': 'ready', 'message': 'Видео готово к просмотру.'})
    return 'generated'
