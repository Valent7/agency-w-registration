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

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--destination',required=True)
    p.add_argument('--registry',required=True);p.add_argument('--release-id',required=True);p.add_argument('--project-ref',required=True)
    a=p.parse_args();print(json.dumps(bind(a.source,a.destination,a.registry,a.release_id,a.project_ref),sort_keys=True))
