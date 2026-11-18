"""Feature-gated integration with existing Agency services (OFF by default).

No schema installation, key changes, LLM extraction, or paid calls occur here.
Every authenticated caller supplies identity from existing server auth, not the LLM.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
from uuid import uuid4

from agent_memory import Memory, Principal, Scope, digest, MemoryError
from agent_memory_store import Repository


def enabled(owner_id=None):
    if os.environ.get('AGENCY_MEMORY_V1', '').strip() != '1':
        return False
    owners = {v.strip() for v in os.environ.get('AGENCY_MEMORY_V1_OWNERS', '').split(',') if v.strip()}
    return bool(owners) if owner_id is None else str(owner_id) in owners


def project_memory(store, owner_id, project, *, kind='agent'):
    if project.get('owner_id') != owner_id or not store.get_project(owner_id, project['id']):
        raise MemoryError('Project does not belong to this owner')
    scope = Scope(owner_id, 'kadrian', project['id'], project['id'])
    principal = Principal(owner_id, 'owner:' + str(owner_id) if kind == 'owner' else 'agent:kadrian',
                          kind, 'kadrian', scope.workspace_id,
                          frozenset({'read', 'append', 'export', 'sync'}))
    return Memory(Repository(store, initialize=False), principal, scope)


def prepare_project(store, owner_id, project, task_id, request):
    if not enabled(owner_id):
        return project
    project_memory(store, owner_id, project)  # validate the caller's owner/project binding
    project = store.get_project(owner_id, project['id'])
    memory = project_memory(store, owner_id, project)
    # Drain durable turn outbox before reading the next task context.
    drain_turns(store, owner_id, project)
    own = memory.prepare_task(task_id, request, key='prepare:' + task_id)
    parents = []
    for agent in ('*', 'kadrian'):
        scope = Scope(owner_id, agent, 'owner')
        principal = Principal(owner_id, 'reader:kadrian', 'agent', agent, 'owner', frozenset({'read'}))
        ctx = Memory(memory.repository, principal, scope).context()
        parents.append({'scope': ctx['scope'], 'revision': ctx['revision'], 'hash': ctx['hash'],
                        'rules': ctx['rules']})
    result = deepcopy(project)
    result['_agent_memory'] = {'task_id': task_id, 'project': own['memory'], 'parents': parents}
    return result


def rule_fingerprint(context):
    return digest([context['project']['rules'], *[p['rules'] for p in context['parents']]])


def before_job(store, job, project):
    if not enabled(job['owner_id']) or not (job.get('kadrian_auto') or job.get('kadrian_provider')):
        return project
    current = prepare_project(store, job['owner_id'], project, 'job:' + job['id'],
                              {'brief': (job.get('provider_spec') or {}).get('brief', job.get('topic', ''))})
    # Refresh for policy comparison; task snapshot itself remains immutable.
    fresh = project_memory(store, job['owner_id'], project).context()
    current['_agent_memory']['project'] = fresh
    pinned = job.get('memory_snapshot')
    if pinned and rule_fingerprint(pinned) != rule_fingerprint(current['_agent_memory']):
        raise MemoryError('Правила памяти изменились после подготовки задания. Требуется пересмотр задания; повторная оплата не запущена.')
    return current


def job_event(job, changes=None):
    changes = changes or {}
    return {'id': uuid4().hex, 'job_id': job['id'],
            'state': changes.get('state', job['state']),
            'at': datetime.now(timezone.utc).isoformat(),
            'assets': [{'sha256': a.get('sha256'), 'kind': a.get('kind')}
                       for a in changes.get('assets', job.get('assets', []))],
            'checks': 'Provider completion is not a perceptual quality approval.'}


def record_job_state(store, job):
    if not enabled(job['owner_id']) or not (job.get('kadrian_auto') or job.get('kadrian_provider') or job.get('memory_snapshot')):
        return
    pending = job.get('memory_outbox') or []
    if not pending and job.get('memory_journaled'):
        return
    project = store.get_project(job['owner_id'], job['project_id'])
    memory = project_memory(store, job['owner_id'], project)
    if not pending:
        # One explicit legacy snapshot. New worker transitions carry durable outbox IDs.
        pending = [dict(job_event(job), id='legacy:' + job['updated_at'], at=job['updated_at'])]
    from agent_memory import Conflict
    for event in pending:
        for attempt in range(3):
            try:
                memory.record('execution', json.dumps(event, ensure_ascii=False),
                              source='worker-state', key='job:' + job['id'] + ':' + event['id'])
                break
            except Conflict:
                if attempt == 2:raise
    # Ack only the exact job version we delivered. A concurrent newer transition
    # keeps its own outbox; replay after a lost ack is idempotent.
    store.update_job(job['owner_id'], job['id'],
                     {'memory_outbox': [], 'memory_journaled': True},
                     lease_owner=job.get('lease_owner'), expected_updated_at=job['updated_at'])


def drain_turns(store, owner_id, project):
    if not enabled(owner_id):
        return
    memory = project_memory(store, owner_id, project)
    # Legacy turns are the durable outbox: replay is idempotent after process/window restart.
    for turn in (project.get('kadrian_chat') or {}).get('turns', []):
        memory.record('execution', json.dumps({'prompt': turn['prompt'], 'answer': turn['answer']}, ensure_ascii=False),
                      source='chat:' + turn['id'], key='chat:' + turn['id'])
        # Classification is explicitly tentative; exact owner text and source are retained.
        # No classifier output can verify a check or approve a policy.
        text = turn['prompt']
        if re.search(r'(?i)(замечание|исправ|не получ|неправиль|ошиб|не работает)', text):
            memory.record('feedback', text, source='owner-chat:' + turn['id'],
                          key='feedback:' + turn['id'])
        elif re.search(r'(?i)(решение:|решили|выбираю|сохрани|запомни)', text):
            memory.record('decision', text, source='owner-chat:' + turn['id'],
                          key='decision:' + turn['id'])


def migrate_legacy(store, owner_id, project):
    """Explicit, owner-triggered idempotent import; old columns/files are untouched."""
    if not enabled(owner_id):
        raise MemoryError('Memory v1 is disabled')
    memory = project_memory(store, owner_id, project, kind='owner')
    for field in ('kadrian_notes', 'facts'):
        if project.get(field):
            memory.record('legacy', str(project[field]), source='legacy-project:' + field,
                          key='legacy:' + field + ':' + digest(project[field]))
    drain_turns(store, owner_id, project)
    return memory.read()


def reconcile_project(store, owner_id, project):
    if not enabled(owner_id):
        return
    drain_turns(store, owner_id, project)
    for job in store.list_jobs(owner_id, project['id'], limit=1000):
        record_job_state(store, job)
