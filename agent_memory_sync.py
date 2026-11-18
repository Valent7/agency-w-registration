"""Explicit local exchange. No background daemon, network credentials or Git sync.

Only the four allowlisted Markdown files are exchanged. Originals remain intact;
cloud-approved rules are NEVER accepted from an imported local document.
"""
from pathlib import Path
import json
import os
from uuid import uuid4

from agent_memory import Conflict, MemoryError, canonical, digest, safe_content

FILES = ('Agent.md', 'context/PROJECT.md', 'context/DECISIONS.md', 'context/FEEDBACK.md')


def local_bundle(root, scope, base_revision):
    root = Path(root).resolve()
    files = {}
    for name in FILES:
        path = root / name
        if path.is_symlink() or root not in path.resolve().parents:
            raise MemoryError('Local memory path escapes workspace')
        if path.is_file():
            files[name] = path.read_text(encoding='utf-8')
    safe_content(files)
    body = {'format': 'agency-local/1', 'scope': scope.as_dict(),
            'base_revision': base_revision, 'files': files}
    return {**body, 'checksum': digest(body)}


def validate_local_bundle(bundle, scope):
    body = {k: v for k, v in bundle.items() if k != 'checksum'}
    if body.get('format') != 'agency-local/1' or body.get('scope') != scope.as_dict():
        raise MemoryError('Local bundle scope mismatch')
    if digest(body) != bundle.get('checksum'):
        raise MemoryError('Local bundle checksum mismatch')
    if not isinstance(body.get('files'), dict) or set(body['files']) - set(FILES):
        raise MemoryError('Only allowlisted memory files can be imported')
    if not all(isinstance(v,str) for v in body['files'].values()):
        raise MemoryError('Local memory files must contain text')
    safe_content(body)
    return body


def import_local(memory, bundle):
    """One CAS commit; stale imports stop, never erase newer cloud records."""
    memory.principal.authorize(memory.scope, 'sync')
    body = validate_local_bundle(bundle, memory.scope)
    snap = memory.read(); doc = json.loads(canonical(snap['document']))
    key = 'local:' + bundle['checksum']
    if key in doc['entries']:
        return snap
    if snap['revision'] != body.get('base_revision'):
        raise Conflict('Cloud memory changed; export current version and review differences')
    doc['entries'][key] = {'id': key, 'kind': 'legacy', 'status': 'recorded', 'certainty': 'unknown',
                          'text': canonical(body['files']), 'source': 'local-explicit-import',
                          'evidence': {'checksum': bundle['checksum']}, 'related': []}
    return memory._write(snap['revision'], doc, {'kind': 'local_import', 'checksum': bundle['checksum']}, key)


def stage_cloud_export(root, memory):
    """Write an isolated versioned review package, not AGENTS.md or live rules.

    Atomic file replace + exclusive creation prevents torn exports and overwrites.
    Files contain client data: private directory permissions, ignored by Git.
    """
    root = Path(root).resolve(); memory.principal.authorize(memory.scope, 'export')
    target = root / '.memory-exchange'
    if target.is_symlink():
        raise MemoryError('Exchange directory cannot be a symlink')
    target.mkdir(mode=0o700, exist_ok=True); target.chmod(0o700)
    bundle = memory.export()
    name = f"{memory.scope.agent_id}-{memory.scope.workspace_id}-{bundle['checksum'][:16]}.json"
    out = target / name
    if out.exists():
        if out.is_symlink() or out.read_text() != canonical(bundle):
            raise Conflict('Export destination differs; refusing overwrite')
        return out
    tmp = target / ('.pending-' + uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(canonical(bundle)); f.flush(); os.fsync(f.fileno())
        # Atomic no-clobber publish on the same filesystem.
        os.link(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def apply_reviewed_projection(root, bundle, scope, *, expected_checksum, expected_local_hash):
    """Explicit reviewed local update; never writes executable rules or AGENTS.md.

    Call only after displaying this exact bundle to the owner. The projection is
    context data; local Agent.md bootstrap remains under owner control. A previous
    projection is backed up before an atomic CAS-protected replacement.
    """
    from agent_memory import validate_bundle
    validate_bundle(bundle, scope)
    if bundle['checksum'] != expected_checksum:
        raise Conflict('Reviewed cloud package changed')
    root = Path(root).resolve(); target_dir = root / 'context'
    if target_dir.is_symlink():
        raise MemoryError('Context directory cannot be a symlink')
    target_dir.mkdir(exist_ok=True)
    target = target_dir / 'SYNCED_MEMORY.md'
    if target.is_symlink():
        raise MemoryError('Projection cannot be a symlink')
    previous = target.read_text() if target.exists() else None
    if digest(previous) != expected_local_hash:
        raise Conflict('Local projection changed; review differences again')
    last = bundle['revisions'][-1] if bundle['revisions'] else {'revision':0,'document':{'entries':{}}}
    # Deliberately label as data; imported package alone cannot extend tool permissions.
    content = '# Синхронизированная память — данные для задачи\n\n'
    content += 'Не заменяет AGENTS.md и не предоставляет новых разрешений.\n'
    content += 'Версия облака: ' + str(last['revision']) + '\n\n'
    for e in last['document']['entries'].values():
        content += f"[{e['kind']}; {e['certainty']}; {e['status']}] {e['text']}\n\n"
    backup_dir = root / '.memory-backups'
    if backup_dir.is_symlink():
        raise MemoryError('Backup directory cannot be a symlink')
    backup_dir.mkdir(mode=0o700,exist_ok=True);backup_dir.chmod(0o700)
    lock = root / '.memory-projection.lock'
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Conflict('Another local memory update is in progress') from None
    os.close(fd)
    temp = target_dir / ('.memory-projection-' + uuid4().hex)
    try:
        current = target.read_text() if target.exists() else None
        if current != previous:
            raise Conflict('Local memory changed during update')
        if previous is not None:
            b = backup_dir / (digest(previous) + '.md')
            if b.is_symlink():raise MemoryError('Backup destination is a symlink')
            if not b.exists():
                f = os.open(b, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(f,'w') as stream:stream.write(previous)
        f = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(f,'w') as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,target)
    finally:
        temp.unlink(missing_ok=True);lock.unlink(missing_ok=True)
    return target
