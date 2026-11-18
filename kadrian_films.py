"""Durable film workspaces; no provider calls, schema changes or job resets."""
from copy import deepcopy
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from content_factory_store import StoreError
from content_factory_core import iso, utc_now


def films(project):
    catalog = deepcopy(project.get('kadrian_films') or {})
    if catalog:
        if catalog.get('version') != 1:
            raise StoreError('Неизвестная версия фильмов. Обновление остановлено.')
        return catalog
    legacy_id = str(uuid5(NAMESPACE_URL, 'kadrian-film:' + project['id']))
    return {'version': 1, 'legacy_id': legacy_id, 'items': {
        legacy_id: {'id': legacy_id, 'created_at': project.get('created_at', ''), 'title': 'Фильм 1 (прежняя история)',
                    'chat': deepcopy(project.get('kadrian_chat') or {}), 'attachments': [], 'settings': {}}
    }}


def _project(store, owner, project_id):
    project = store.get_project(owner, project_id)
    if not project or project.get('owner_id') != owner:
        raise StoreError('Проект недоступен этому владельцу.')
    return project


def change_film(store, owner, project_id, film_id, mutate):
    """Retry a CAS merge, never overwrite another film or unrelated project memory."""
    for attempt in range(5):
        project = _project(store, owner, project_id)
        catalog = films(project)
        if film_id not in catalog['items']:
            raise StoreError('Фильм не найден в этом проекте.')
        mutate(catalog['items'][film_id])
        try:
            return store.update_project_if_current(owner, project_id, {'kadrian_films': catalog}, project['updated_at'])
        except StoreError:
            if attempt == 4:
                raise


def create_film(store, owner, project_id, film_id=None):
    # Caller keeps this token if a write acknowledgement is lost.
    film_id = str(UUID(film_id)) if film_id else str(uuid4())
    for attempt in range(5):
        project = _project(store, owner, project_id)
        catalog = films(project)
        if film_id in catalog['items']:
            return catalog['items'][film_id]
        item = {'id': film_id, 'created_at': iso(utc_now()), 'title': f"Фильм {len(catalog['items']) + 1}",
                'chat': {}, 'attachments': [], 'settings': {}}
        catalog['items'][film_id] = item
        try:
            store.update_project_if_current(owner, project_id, {'kadrian_films': catalog}, project['updated_at'])
            return item
        except StoreError:
            if attempt == 4:
                raise


def production_status(job):
    """State wins over an old progress sentence, including terminal failures."""
    state = job.get('state')
    if job.get('error'):
        return str(job['error'])
    if state in {'ready', 'published', 'scheduled'}:
        return {'ready': 'Видео готово.', 'published': 'Видео опубликовано.',
                'scheduled': 'Видео готово, публикация запланирована.'}[state]
    if state == 'queued':
        progress = job.get('progress') or {}
        return (progress.get('message') or 'Ожидается пополнение кредитов.') if progress.get('stage') == 'credits' else 'Задание в очереди.'
    if state == 'generating':
        return (job.get('progress') or {}).get('message') or 'Идёт производство.'
    return {'failed': 'Производство завершилось ошибкой.', 'paused': 'Производство приостановлено.',
            'cancelled': 'Задание отменено.', 'studio': 'Материал сохранён.'}.get(state, 'Статус: ' + str(state))


class FilmStore:
    """UI store boundary: scope every route, library, upload and idempotency key."""
    def __init__(self, store, owner, project_id, film_id):
        self.raw = store
        self.owner, self.project_id, self.film_id = owner, project_id, film_id
        self.get_project(owner, project_id)

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def _scope(self, owner, project_id=None):
        if owner != self.owner or (project_id is not None and project_id != self.project_id):
            raise StoreError('Фильм недоступен.')

    def get_project(self, owner, project_id):
        self._scope(owner, project_id)
        project = _project(self.raw, owner, project_id)
        catalog = films(project)
        if self.film_id not in catalog['items']:
            raise StoreError('Фильм не найден в этом проекте.')
        item = catalog['items'][self.film_id]
        return {**project, 'kadrian_chat': deepcopy(item['chat']), 'film_id': self.film_id}

    def update_project_if_current(self, owner, project_id, changes, expected_updated_at):
        self._scope(owner, project_id)
        project = self.get_project(owner, project_id)
        if project['updated_at'] != expected_updated_at:
            raise StoreError('Проект изменён в другом окне.')
        changes = deepcopy(changes)
        if 'kadrian_chat' in changes:
            catalog = films(project)
            memory = changes.pop('kadrian_chat')
            catalog['items'][self.film_id]['chat'] = memory
            if self.film_id == catalog['legacy_id']:
                changes['kadrian_chat'] = memory
            changes['kadrian_films'] = catalog
        self.raw.update_project_if_current(owner, project_id, changes, expected_updated_at)
        return self.get_project(owner, project_id)

    def list_jobs(self, owner, project_id, limit=100):
        self._scope(owner, project_id)
        project = self.get_project(owner, project_id)
        return self.raw.list_film_jobs(owner, project_id, self.film_id,
                                       legacy=self.film_id == films(project)['legacy_id'], limit=limit)

    def get_job(self, owner, job_id):
        self._scope(owner)
        job = self.raw.get_job(owner, job_id)
        if job is None:
            return None
        project = self.get_project(owner, self.project_id)
        if job['project_id'] != self.project_id or (job.get('film_id') or films(project)['legacy_id']) != self.film_id:
            raise StoreError('Задание другого фильма недоступно.')
        return job

    def enqueue_job(self, owner, project_id, data):
        self._scope(owner, project_id)
        self.get_project(owner, project_id)
        self._check_references(data)
        job = self.raw.enqueue_job(owner, project_id, {**data, 'film_id': self.film_id,
            'dedupe_key': 'film:' + self.film_id + ':' + data['dedupe_key']})
        if job.get('film_id') != self.film_id:
            raise StoreError('Запрос относится к другому фильму.')
        return job

    def update_job(self, owner, job_id, changes, **kwargs):
        if not self.get_job(owner, job_id):
            raise StoreError('Задание не найдено.')
        if 'film_id' in changes and changes['film_id'] != self.film_id:
            raise StoreError('Нельзя переносить задание в другой фильм.')
        self._check_references(changes)
        return self.raw.update_job(owner, job_id, changes, **kwargs)

    def _check_references(self, value):
        if isinstance(value, dict):
            if value.get('storage_path'):
                self._check_asset(value)
            else:
                for entry in value.values():
                    self._check_references(entry)
        elif isinstance(value, list):
            for entry in value:
                self._check_references(entry)

    def _check_asset(self, asset):
        path = str(asset.get('storage_path') or '')
        if not path.startswith(f'{self.owner}/{self.project_id}/'):
            raise StoreError('Материал другого проекта недоступен.')
        if asset.get('film_id'):
            expected_prefix = str(asset.get('sha256') or '')[:16] + '-' + self.film_id + '-'
            if asset['film_id'] == self.film_id and path.split('/')[-1].startswith(expected_prefix):
                return
            raise StoreError('Материал другого фильма недоступен.')
        job_id = asset.get('job_id') or (path.split('/')[2] if len(path.split('/')) >= 4 and path.split('/')[2] != self.project_id else None)
        if job_id and self.get_job(self.owner, job_id):
            return
        project = self.get_project(self.owner, self.project_id)
        if not asset.get('film_id') and self.film_id == films(project)['legacy_id']:
            return
        raise StoreError('Материал другого фильма нельзя использовать без отдельного разрешения.')

    def put_asset(self, owner, project_id, job_id, filename, data, mime_type):
        self._scope(owner, project_id)
        self.get_project(owner, project_id)
        if job_id and not self.get_job(owner, job_id):
            raise StoreError('Задание не найдено.')
        # Distinct path even when two films upload the same filename/content.
        asset = self.raw.put_asset(owner, project_id, job_id, self.film_id + '-' + filename, data, mime_type)
        return {**asset, 'film_id': self.film_id, 'original_filename': filename}

    def read_asset(self, owner, asset):
        self._scope(owner)
        self._check_asset(asset)
        return self.raw.read_asset(owner, asset)

    def resolve_assets(self, owner, assets):
        self._scope(owner)
        for asset in assets:
            self._check_asset(asset)
        return self.raw.resolve_assets(owner, assets)

    def save_attachments(self, files):
        assets = [self.put_asset(self.owner, self.project_id, None, name, data, mime) for name, mime, data in files]
        def remember(item):
            item.setdefault('attachment_history', []).append(deepcopy(item.get('attachments', [])))
            item['attachments'] = assets
        change_film(self.raw, self.owner, self.project_id, self.film_id, remember)
        return assets

    def restore_attachments(self):
        project = self.get_project(self.owner, self.project_id)
        return [(asset.get('original_filename') or asset['filename'].removeprefix(self.film_id + '-'), asset['mime_type'],
                 self.read_asset(self.owner, asset))
                for asset in films(project)['items'][self.film_id].get('attachments', [])]
