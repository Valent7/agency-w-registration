"""Offline release builder/guard. NEVER deploys, merges or calls Render/GitHub APIs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

INTEGRATION='codex/memory-sync-integration'
RELEASE='release/kadrian-memory-sync'
REPO='https://github.com/Valent7/agency-w-registration'
FILES=('agent_memory.py','agent_memory_store.py','content_factory_store.py',
       'agent_memory_runtime.py','agent_memory_device.py','agent_memory_device_api.py',
       'agent_memory_server.py')
REQUIREMENTS='deploy/memory-sync/requirements-server.txt'
PYTHON_VERSION='deploy/memory-sync/python-version'


def check_topology(services,pr_base,*,dedicated_service_id=None):
    if pr_base!=INTEGRATION:raise ValueError('Memory PR must not target main or a working branch')
    for s in services:
        same_repo=str(s.get('repo','')).removesuffix('.git')==REPO
        if same_repo and s.get('branch') in {INTEGRATION,RELEASE}:
            if s.get('id')!=dedicated_service_id or not dedicated_service_id or s['branch']!=RELEASE:
                raise ValueError('Memory branch is shared with a working service')
    return {'pr_base':INTEGRATION,'release_branch':RELEASE,'unrelated_deploy_targets':0}


def build(root,destination,source_sha):
    root=Path(root);dest=Path(destination)
    if len(source_sha)!=40 or any(c not in '0123456789abcdef' for c in source_sha):raise ValueError('Exact reviewed commit required')
    subprocess.run(['git','-C',str(root),'cat-file','-e',source_sha+'^{commit}'],check=True,capture_output=True)
    if dest.exists():raise ValueError('Refusing to overwrite release directory')
    dest.mkdir(parents=True,mode=0o700)
    manifest={}
    for src in (*FILES,REQUIREMENTS,PYTHON_VERSION):
        content=subprocess.check_output(['git','-C',str(root),'show',source_sha+':'+src])
        target='requirements.txt' if src==REQUIREMENTS else '.python-version' if src==PYTHON_VERSION else src
        (dest/target).write_bytes(content);manifest[target]=hashlib.sha256(content).hexdigest()
    body={'format':'agency-memory-release/1','source_sha':source_sha,'target_branch':RELEASE,
          'entrypoint':'uvicorn agent_memory_server:runtime_app --factory --host 0.0.0.0 --port $PORT --no-access-log',
          'files':manifest}
    (dest/'release.json').write_text(json.dumps(body,sort_keys=True,indent=2)+'\n')
    verify(dest)
    return body


def verify(destination):
    dest=Path(destination)
    if dest.is_symlink():raise ValueError('Symlink release directory')
    body=json.loads((dest/'release.json').read_text())
    if body.get('format')!='agency-memory-release/1' or body.get('target_branch')!=RELEASE:raise ValueError('Invalid release identity')
    expected=set(FILES)|{'requirements.txt','.python-version'}
    if set(body['files'])!=expected:raise ValueError('Release allowlist changed')
    if {p.name for p in dest.iterdir()}!=expected|{'release.json'}:raise ValueError('Unexpected release files')
    for name,checksum in body['files'].items():
        path=dest/name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
            raise ValueError('Release checksum mismatch')
    return body


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('build');p.add_argument('--root',required=True);p.add_argument('--destination',required=True);p.add_argument('--source-sha',required=True)
    p=sub.add_parser('verify');p.add_argument('--destination',required=True)
    p=sub.add_parser('check-topology');p.add_argument('--services-json',required=True);p.add_argument('--pr-base',required=True);p.add_argument('--dedicated-service-id')
    args=parser.parse_args()
    if args.action=='build':result=build(args.root,args.destination,args.source_sha)
    elif args.action=='verify':result=verify(args.destination)
    else:result=check_topology(json.loads(Path(args.services_json).read_text()),args.pr_base,dedicated_service_id=args.dedicated_service_id)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
