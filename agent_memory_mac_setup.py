"""Generate reviewable install assets; never starts launchd or changes cloud."""
import argparse
import base64
import json
from pathlib import Path
import plistlib
import shlex
import sys
from uuid import uuid4
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from agent_memory import Scope, canonical, MemoryError
from agent_memory_daemon import atomic, private_dir, Sync, HTTPS
from agent_memory_device import SYNC_FILES

LABEL='com.agency-w.kadrian-memory-sync'


def prepare(root,output,python,code,scope,endpoint,device_id=None):
    """Creates files only at operator-selected staging destination, no installation."""
    from agent_memory_edge import EdgeHTTPS
    backend='supabase-edge' if '/functions/v1/' in endpoint else 'standalone'
    (EdgeHTTPS if backend=='supabase-edge' else HTTPS)(endpoint)  # validation only, no request
    root=Path(root).absolute();code=Path(code).absolute();python=Path(python).absolute()
    output=private_dir(Path(output).absolute())
    for name in ('config.json','launch-agent.plist','codex-hooks.json'):
        if (output/name).exists():raise MemoryError('Staging destination already populated')
    cfg={'root':str(root),'state_dir':str(output/'transport'),'scope':scope.as_dict(),
         'device_id':device_id or uuid4().hex,'endpoint':endpoint,'backend':backend}
    config=output/'config.json'
    atomic(config,canonical(cfg).encode())
    command=[str(python),str(code/'agent_memory_daemon.py'),'--config',str(config)]
    plist={'Label':LABEL,'ProgramArguments':command,'RunAtLoad':True,'StartInterval':60,
           'WatchPaths':[str(root/name) for name in SYNC_FILES],
           'ThrottleInterval':30,'ProcessType':'Background','LimitLoadToSessionType':'Aqua',
           'Umask':0o077,'StandardOutPath':'/dev/null','StandardErrorPath':'/dev/null'}
    atomic(output/'launch-agent.plist',plistlib.dumps(plist))
    hooks={'description':'Project-scoped Kadrian memory freshness check; no conversation text uploaded.',
           'hooks':{event:[{'hooks':[{'type':'command','command':shlex.join(command+['--hook',event]),
               'timeout':70,'statusMessage':'Проверяю актуальность памяти Кадриана'}]}]
               for event in ('SessionStart','UserPromptSubmit')}}
    atomic(output/'codex-hooks.json',json.dumps(hooks,ensure_ascii=False,indent=2).encode())
    return cfg


def enroll(config_path,keychain):
    """Explicit installation step. New DEVICE signing key only, never API/db keys."""
    config_path=Path(config_path)
    if config_path.is_symlink() or config_path.stat().st_mode & 0o077:raise MemoryError('Private config required')
    cfg=json.loads(config_path.read_text());directory=private_dir(cfg['state_dir'])
    with __import__('agent_memory_daemon').exclusive(directory):
        if (directory/'transport.enc').exists():raise MemoryError('Device already enrolled')
        key=Ed25519PrivateKey.generate()
        raw=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        keychain.create(cfg['device_id'],raw)
        from agent_memory_edge import EdgeHTTPS
        sync=Sync(cfg['root'],directory,Scope(**cfg['scope']),cfg['device_id'],raw,
                  (EdgeHTTPS if cfg.get('backend')=='supabase-edge' else HTTPS)(cfg['endpoint']))
        sync.initialize()
        public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        registry={cfg['device_id']:{'public_key':base64.b64encode(public).decode(),'scope':cfg['scope'],'revoked':False}}
        atomic(config_path.parent/'device-public-registration.json',canonical(registry).encode())
        return registry


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('--root',required=True);p.add_argument('--output',required=True)
    p.add_argument('--python',required=True);p.add_argument('--code',required=True);p.add_argument('--owner',required=True,type=int)
    p.add_argument('--agent',default='kadrian');p.add_argument('--project',required=True);p.add_argument('--endpoint',required=True)
    p=sub.add_parser('enroll');p.add_argument('--config',required=True)
    args=parser.parse_args()
    if args.action=='prepare':
        prepare(args.root,args.output,args.python,args.code,Scope(args.owner,args.agent,args.project,args.project),args.endpoint)
        print('Install assets prepared; no services activated.')
    else:
        from agent_memory_mac_keychain import Keychain
        enroll(args.config,Keychain());print('Device created in Keychain; public registration staged. No cloud/launchd changes.')


if __name__=='__main__':main()


def prepare_gated_existing(config_path, output, python, code):
    """Review-only assets using EXISTING identity/config/queue; no enrollment/start."""
    config=Path(config_path).resolve(strict=True)
    cfg=json.loads(config.read_text())
    if config.stat().st_mode & 0o077:raise MemoryError('Private config required')
    output=private_dir(Path(output).absolute())
    if any(output.iterdir()):raise MemoryError('Empty staging directory required')
    command=[str(Path(python).absolute()),str(Path(code).absolute()/'agent_memory_run.py'),'--config',str(config)]
    plist={'Label':LABEL,'ProgramArguments':command,'RunAtLoad':True,'StartInterval':60,
           'WatchPaths':[str(Path(cfg['root'])/n) for n in SYNC_FILES],
           'ThrottleInterval':30,'ProcessType':'Background','LimitLoadToSessionType':'Aqua','Umask':0o077,
           'StandardOutPath':'/dev/null','StandardErrorPath':'/dev/null'}
    atomic(output/'launch-agent.plist',plistlib.dumps(plist))
    hooks={'hooks':{event:[{'hooks':[{'type':'command','command':shlex.join(command+['--hook',event]),
           'timeout':70}]}] for event in ('SessionStart','UserPromptSubmit')}}
    atomic(output/'codex-hooks.json',json.dumps(hooks,ensure_ascii=False,indent=2).encode())
    atomic(output/'identity.json',canonical({'scope':cfg['scope'],'device_id':cfg['device_id'],
           'config':str(config),'state_dir':cfg['state_dir'],'installed':False}).encode())
    return cfg
