"""Derive offline reviewed deployment from exact release + public device registry.
No API calls, environment configuration, private keys, or live modifications.
"""
import argparse,hashlib,json,re,shutil
from pathlib import Path
from uuid import UUID
import sys
if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_memory import Scope
from tools.memory_edge_release import verify


def bind(source, destination, registry, release_id, project_ref):
    manifest=verify(source)
    if not re.fullmatch(r'[a-f0-9]{32}',release_id) or not re.fullmatch(r'[a-z0-9]{20}',project_ref):raise ValueError('Invalid release/project')
    data=json.loads(Path(registry).read_text())
    if len(data)!=1:raise ValueError('Exactly one device required')
    device,record=next(iter(data.items()));scope=Scope(**record['scope'])
    import base64
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}',device) or len(base64.b64decode(record['public_key'],validate=True))!=32:raise ValueError('Invalid public registration')
    if record.get('revoked') or scope.agent_id!='kadrian' or scope.project_id is None or scope.workspace_id!=str(UUID(scope.project_id)):raise ValueError('Single Kadrian project required')
    if set(record)-{'public_key','scope','revoked'}:raise ValueError('Only public registration accepted')
    destination=Path(destination)
    if destination.exists():raise ValueError('Never overwrite deployment')
    shutil.copytree(source,destination)
    body={'url':f'https://{project_ref}.supabase.co','release':release_id,'pilot':scope.as_dict(),'devices':data}
    target=destination/'supabase/functions/kadrian-memory-sync/binding.ts'
    target.write_text('// Reviewed public binding; no environment permissions.\nexport const binding = '+json.dumps(body,sort_keys=True)+';\n')
    # Derived release is distinct, with source identity and all current checksums.
    original=destination/'release.json';original.rename(destination/'source-release.json')
    files={str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.rglob('*') if p.is_file()}
    result={'format':'agency-memory-gated-deployment/1','source_sha':manifest['source_sha'],'release_id':release_id,
      'device_id':device,'scope':scope.as_dict(),'project_ref':project_ref,'files':files,'enabled':False,
      'requires_operator_database_registration':True}
    (destination/'deployment.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return result

def verify_bound(destination):
    dest=Path(destination)
    if dest.is_symlink() or any(p.is_symlink() for p in dest.rglob('*')):raise ValueError('Symlink deployment')
    body=json.loads((dest/'deployment.json').read_text())
    source=json.loads((dest/'source-release.json').read_text())
    from tools.memory_edge_release import FILES
    expected=(set(FILES.values())-{'release.json'})|{'source-release.json'}
    actual={str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file()}
    if body.get('format')!='agency-memory-gated-deployment/1' or set(body['files'])!=expected or actual!=expected|{'deployment.json'}:raise ValueError('Deployment allowlist mismatch')
    if source.get('source_sha')!=body.get('source_sha') or set(source['files'])!=set(FILES.values()):raise ValueError('Source manifest mismatch')
    binding_path='supabase/functions/kadrian-memory-sync/binding.ts'
    for name,checksum in body['files'].items():
        if hashlib.sha256((dest/name).read_bytes()).hexdigest()!=checksum:raise ValueError('Deployment checksum mismatch')
        if name not in (binding_path,'source-release.json') and source['files'][name]!=checksum:raise ValueError('Unexpected source modification')
    text=(dest/binding_path).read_text()
    if not text.startswith('// Reviewed public binding; no environment permissions.\nexport const binding = ') or not text.endswith(';\n'):raise ValueError('Unexpected binding program')
    bound=json.loads(text.split('export const binding = ',1)[1][:-2])
    if bound['release']!=body['release_id'] or bound['pilot']!=body['scope'] or set(bound['devices'])!={body['device_id']} or bound['url']!='https://'+body['project_ref']+'.supabase.co':raise ValueError('Deployment identity mismatch')
    if body.get('enabled') is not False:raise ValueError('Deployment must start disabled')
    return body


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--verify');p.add_argument('--source');p.add_argument('--destination')
    p.add_argument('--registry');p.add_argument('--release-id');p.add_argument('--project-ref')
    a=p.parse_args();print(json.dumps(verify_bound(a.verify) if a.verify else bind(a.source,a.destination,a.registry,a.release_id,a.project_ref),sort_keys=True))
