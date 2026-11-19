"""Offline exact-SHA release artifact. No deploy credentials, APIs, merge or migrations."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

FUNCTION='kadrian-memory-sync'
FILES={**{f'supabase/functions/{FUNCTION}/{n}':f'supabase/functions/{FUNCTION}/{n}' for n in ('index.ts','service.ts','protocol.ts')},
       **{n:'mac/'+n for n in ('agent_memory.py','agent_memory_device.py','agent_memory_daemon.py','agent_memory_edge.py','agent_memory_sync.py','agent_memory_mac_setup.py','agent_memory_mac_keychain.py')},
       'deploy/memory-sync/requirements-mac.txt':'mac/requirements.txt',
       'deploy/memory-edge/config.fragment.toml':'supabase/config.toml'}


def verify(destination):
    dest=Path(destination)
    if dest.is_symlink():raise ValueError('Symlink release')
    body=json.loads((dest/'release.json').read_text())
    if body.get('format')!='agency-memory-edge-release/1' or body.get('function')!=FUNCTION or set(body['files'])!=set(FILES.values()):raise ValueError('Release identity/allowlist mismatch')
    actual={str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file()}
    if actual!=set(FILES.values())|{'release.json'} or any(p.is_symlink() for p in dest.rglob('*')):raise ValueError('Unexpected release files')
    for name,checksum in body['files'].items():
        if hashlib.sha256((dest/name).read_bytes()).hexdigest()!=checksum:raise ValueError('Release checksum mismatch')
    return body


def build(root,destination,sha):
    if len(sha)!=40 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('Exact reviewed commit required')
    root=Path(root);dest=Path(destination)
    subprocess.run(['git','-C',str(root),'cat-file','-e',sha+'^{commit}'],check=True,capture_output=True)
    if dest.exists() or dest.is_symlink():raise ValueError('Refusing to overwrite release')
    dest.mkdir(parents=True,mode=0o700);checksums={}
    for source,target in FILES.items():
        data=subprocess.check_output(['git','-C',str(root),'show',sha+':'+source])
        path=dest/target;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        checksums[target]=hashlib.sha256(data).hexdigest()
    body={'format':'agency-memory-edge-release/1','function':FUNCTION,'source_sha':sha,'files':checksums,
          'deployment':'manual_after_owner_approval','database_migrations':False,'render_services':False}
    (dest/'release.json').write_text(json.dumps(body,sort_keys=True,indent=2)+'\n')
    return verify(dest)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['build','verify']);p.add_argument('--root',default='.');p.add_argument('--destination',required=True);p.add_argument('--source-sha')
    a=p.parse_args();print(json.dumps(build(a.root,a.destination,a.source_sha) if a.action=='build' else verify(a.destination),sort_keys=True))
