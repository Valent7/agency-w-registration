"""Deployment/process isolation proof on synthetic data. No cloud/service mutations."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from starlette.testclient import TestClient
from agent_memory import Scope,Memory,Principal
from agent_memory_server import create_app,MemoryOnlySupabaseStore,configured_scope
from agent_memory_store import Repository
from agent_memory_daemon import Sync
from agent_memory_device import SYNC_FILES
from content_factory_store import SQLiteStore,StoreError
from tools.memory_sync_release import check_topology,build,verify,INTEGRATION,RELEASE,REPO,FILES,REQUIREMENTS,PYTHON_VERSION

ROOT=Path(__file__).resolve().parents[1]


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve()
        self.root=self.base/'mac';(self.root/'context').mkdir(parents=True)
        for name in SYNC_FILES:(self.root/name).write_text('Synthetic local '+name)
        self.store=SQLiteStore(self.base/'existing-memory.sqlite3')
        project=self.store.save_project(11,{'name':'Synthetic allowed','facts':'Keep original'})
        self.other=self.store.save_project(12,{'name':'Other owner'})
        self.scope=Scope(11,'kadrian',project['id'],project['id']);Repository(self.store,initialize=True)
        key=Ed25519PrivateKey.generate();raw=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        self.env={'AGENCY_MEMORY_V1':'1','AGENCY_MEMORY_DEVICE_SYNC':'1','AGENCY_MEMORY_V1_OWNERS':'11',
                  'AGENCY_MEMORY_V1_PROJECT_ID':project['id'],'AGENCY_MEMORY_SYNC_AGENT_ID':'kadrian',
                  'AGENCY_MEMORY_SYNC_DEVICES':json.dumps({'synthetic-device':{'scope':self.scope.as_dict(),'public_key':base64.b64encode(public).decode()}})}
        self.client=TestClient(create_app(self.store,self.env));self.addCleanup(self.client.close)
        self.sync=Sync(self.root,self.base/'transport',self.scope,'synthetic-device',raw,self.exchange);self.sync.initialize()

    def exchange(self,envelope):
        response=self.client.post('/memory-sync/v1/exchange',json=envelope)
        if response.status_code!=200:raise RuntimeError('Synthetic network unavailable')
        return response.json()

    def test_standalone_roundtrip_and_restart_without_other_jobs(self):
        owner=Memory(Repository(self.store),Principal(11,'owner','owner'),self.scope)
        owner.record('fact','Synthetic cloud update',source='test',key='cloud')
        self.assertEqual(self.sync.cycle()['status'],'current')
        self.assertIn('Synthetic cloud update',(self.root/'context/SYNCED_MEMORY.md').read_text())
        rev=owner.read()['revision'];self.sync.cycle();self.assertEqual(owner.read()['revision'],rev)
        self.assertEqual(self.store.list_jobs(11,self.scope.project_id),[])
        self.assertEqual(self.store.get_project(12,self.other['id'])['name'],'Other owner')
        self.assertEqual(self.client.get('/ready').status_code,200)

    def test_no_social_provider_routes_or_lifespan_tasks(self):
        self.assertEqual({route.path for route in self.client.app.routes},{'/health','/ready','/memory-sync/v1/exchange'})
        for path in ['/instagram/webhook','/vk/webhook','/connect/instagram','/oauth/instagram/callback']:
            self.assertEqual(self.client.get(path).status_code,404)
        with TestClient(create_app(self.store,self.env)) as client:self.assertEqual(client.get('/health').status_code,200)
        self.assertEqual(self.store.list_jobs(11,self.scope.project_id),[])

    def test_import_and_startup_deny_worker_provider_imports_and_network(self):
        code='''
import importlib.abc,sys,requests
class Deny(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith(('instagram','vk_','neona_','streamlit','openai','content_factory_runtime','content_factory_worker','content_factory_oauth','kadrian_studio')):
   raise AssertionError('Forbidden import: '+fullname)
sys.meta_path.insert(0,Deny())
requests.Session.request=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Startup network'))
from agent_memory_server import create_app
app=create_app(env={})
assert {r.path for r in app.routes}=={'/health','/ready','/memory-sync/v1/exchange'}
print('isolated')
'''
        r=subprocess.run([sys.executable,'-c',code],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr);self.assertIn('isolated',r.stdout)

    def test_disabled_service_not_ready_and_no_schema_install(self):
        with patch('agent_memory_server.Repository') as repo:
            client=TestClient(create_app(env={}))
            self.assertEqual(client.get('/health').status_code,200)
            self.assertEqual(client.get('/ready').status_code,503)
            self.assertEqual(client.post('/memory-sync/v1/exchange',json={}).status_code,404)
            repo.assert_not_called();client.close()

    def test_service_rejects_registry_with_other_owner_agent_project(self):
        for field,value in [('owner_id',12),('agent_id','neona'),('project_id',self.other['id']),('workspace_id',self.other['id'])]:
            env=dict(self.env);registry=json.loads(env['AGENCY_MEMORY_SYNC_DEVICES']);registry['synthetic-device']['scope'][field]=value
            env['AGENCY_MEMORY_SYNC_DEVICES']=json.dumps(registry)
            with self.assertRaises(Exception):configured_scope(env)

    def test_narrow_adapter_rejects_jobs_oauth_media_and_parent_writes_before_network(self):
        session=Mock();store=MemoryOnlySupabaseStore('https://synthetic.supabase.co','synthetic-key',self.scope,session=session)
        for method,path,kw in [('POST','/rest/v1/content_factory_jobs',{}),('GET','/rest/v1/content_factory_connections',{'params':{'owner_id':'eq.11'}}),
            ('GET','/storage/v1/object/private/file',{'params':{'owner_id':'eq.11'}}),
            ('POST','/rest/v1/rpc/agent_memory_commit',{'json':{'p_owner':11,'p_agent':'*','p_workspace':'owner','p_project':None}})]:
            with self.assertRaises(StoreError):store._request(method,path,**kw)
        with self.assertRaises(StoreError):store.get_project(12,self.other['id'])
        session.request.assert_not_called()

    def test_project_lookup_sends_only_identity_select_no_private_fields(self):
        response=Mock();response.ok=True;response.content=b'[]';response.json.return_value=[]
        session=Mock();session.request.return_value=response
        store=MemoryOnlySupabaseStore('https://synthetic.supabase.co','synthetic-key',self.scope,session=session)
        self.assertIsNone(store.get_project(11,self.scope.project_id))
        kw=session.request.call_args.kwargs;self.assertEqual(kw['params']['select'],'id,owner_id');self.assertFalse(kw['allow_redirects'])
        self.assertFalse(session.trust_env)

    def test_topology_rejects_main_and_shared_service_branch(self):
        current=[{'id':'web','repo':REPO+'.git','branch':'main','autoDeploy':'yes'},
                 {'id':'neona','repo':REPO,'branch':'main','autoDeploy':'yes'}]
        self.assertEqual(check_topology(current,INTEGRATION)['unrelated_deploy_targets'],0)
        with self.assertRaises(ValueError):check_topology(current,'main')
        with self.assertRaises(ValueError):check_topology(current+[{'id':'social','repo':REPO,'branch':RELEASE}],INTEGRATION)
        with self.assertRaises(ValueError):check_topology(current+[{'id':'social','repo':REPO,'branch':INTEGRATION}],INTEGRATION)
        self.assertEqual(check_topology(current+[{'id':'dedicated','repo':REPO,'branch':RELEASE}],INTEGRATION,dedicated_service_id='dedicated')['unrelated_deploy_targets'],0)

    def test_release_builder_exact_commit_no_secrets_extras_or_working_code(self):
        repo=self.base/'source';repo.mkdir()
        subprocess.run(['git','init','-q',str(repo)],check=True)
        for name in (*FILES,REQUIREMENTS,PYTHON_VERSION):
            p=repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# Synthetic '+name+'\n')
        (repo/'client-secret.txt').write_text('Do not export synthetic private file')
        subprocess.run(['git','-C',str(repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Synthetic','-c','user.email=synthetic@example.test','commit','-qm','synthetic'],check=True)
        sha=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        (repo/FILES[0]).write_text('Uncommitted change must not export')
        target=self.base/'release';manifest=build(repo,target,sha)
        self.assertEqual(manifest['source_sha'],sha);self.assertNotIn('client-secret.txt',manifest['files'])
        self.assertNotIn('Uncommitted',(target/FILES[0]).read_text());verify(target)
        (target/'unexpected.txt').write_text('extra')
        with self.assertRaises(ValueError):verify(target)

    def test_existing_oauth_source_matches_installed_base(self):
        self.assertEqual(hashlib.sha256((ROOT/'content_factory_oauth.py').read_bytes()).hexdigest(),'60f00f112e901675917ae6c4be9b08736b2cbb40c624bb9dc5d4f3180dc5b1b8')

    def test_merging_only_integration_preserves_main_and_all_working_targets(self):
        repo=self.base/'merge-proof';subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
        run=lambda *args:subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
        run('config','user.name','Synthetic');run('config','user.email','synthetic@example.test')
        (repo/'app.txt').write_text('working baseline');run('add','.');run('commit','-qm','baseline')
        baseline=run('rev-parse','main');run('branch',INTEGRATION);run('switch','-qc','feature')
        (repo/'memory.txt').write_text('synthetic independent memory code');run('add','.');run('commit','-qm','memory')
        run('switch','-q',INTEGRATION);run('merge','--no-ff','-m','reviewed memory merge','feature')
        self.assertEqual(run('rev-parse','main'),baseline)
        current=[{'id':name,'repo':REPO,'branch':'main','autoDeploy':'yes'} for name in ('web','neona','instagram','video')]
        self.assertEqual(check_topology(current,INTEGRATION)['unrelated_deploy_targets'],0)
        self.assertNotEqual(run('rev-parse',INTEGRATION),baseline)
