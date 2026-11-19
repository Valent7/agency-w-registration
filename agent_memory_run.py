"""Owner-controlled durable local latch. No environment switch or auto-enable.

Use as launchd/Codex entrypoint only after explicit approval. No keychain or
transport is opened until both a permit and absence of STOPPED are verified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
from agent_memory_daemon import atomic


def permitted(config):
    path=Path(config)
    if any(p.is_symlink() for p in (path,*path.parents)) or path.stat().st_mode & 0o077:return False
    cfg=json.loads(path.read_text());state=Path(cfg['state_dir'])
    if any(p.is_symlink() for p in (state,*state.parents)) or state.stat().st_mode & 0o077:return False
    if (state/'STOPPED').exists() or (state/'STOPPED').is_symlink():return False
    permit=state/'run-permit.json'
    if not permit.exists() or permit.is_symlink() or permit.stat().st_mode & 0o077:return False
    p=json.loads(permit.read_text())
    return p.get('format')=='agency-memory-run-permit/1' and bool(p.get('approval')) and p.get('config_sha256')==hashlib.sha256(path.read_bytes()).hexdigest() and p.get('device_id')==cfg['device_id'] and p.get('scope')==cfg['scope']


def stop(config):
    cfg=json.loads(Path(config).read_text());state=Path(cfg['state_dir'])
    if any(p.is_symlink() for p in (state,*state.parents)):raise ValueError('Unsafe state path')
    # Keep permit and all queue files. STOPPED dominates a retained permit.
    atomic(state/'STOPPED',b'Owner stop; explicit reapproval required.\n')


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--config',required=True)
    p.add_argument('--stop',action='store_true');p.add_argument('--hook',choices=['SessionStart','UserPromptSubmit']);p.add_argument('--preflight',action='store_true')
    a=p.parse_args(argv)
    if a.stop:stop(a.config);return 0
    try:allowed=permitted(a.config)
    except Exception:allowed=False
    if not allowed:
        if a.hook:print(json.dumps({'hookSpecificOutput':{'hookEventName':a.hook,'additionalContext':'Синхронизация остановлена. Актуальность облачной памяти не подтверждена. Очередь сохранена.'}},ensure_ascii=False))
        else:print(json.dumps({'status':'locally_stopped','checked_at':time.time()}))
        return 0 if a.hook else 2
    from agent_memory_daemon import main as daemon
    args=['--config',a.config]
    if a.hook:args+=['--hook',a.hook]
    if a.preflight:args+=['--preflight']
    return daemon(args, permit_check=lambda: permitted(a.config))


if __name__=='__main__':raise SystemExit(main())
