"""Synthetic local activation latch. No production paths, network or Keychain."""
import hashlib,json,os,plistlib,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_memory_run import permitted,stop,main
from agent_memory_mac_setup import prepare_gated_existing

class LocalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.cfg=self.root/'config.json';self.cfg.write_text(json.dumps({'root':str(self.root),'state_dir':str(self.state),'device_id':'synthetic','scope':{'owner_id':42}}));self.cfg.chmod(0o600)
        (self.state/'transport.enc').write_bytes(b'unchanged encrypted fixture')
    def approve(self):
        p=self.state/'run-permit.json';p.write_text(json.dumps({'format':'agency-memory-run-permit/1','approval':'synthetic-owner-approval',
          'config_sha256':hashlib.sha256(self.cfg.read_bytes()).hexdigest(),'device_id':'synthetic','scope':{'owner_id':42}}));p.chmod(0o600)
    def test_env_cannot_open_missing_permit_and_keychain_not_opened(self):
        with patch.dict(os.environ,{'KADRIAN_MEMSYNC_CONFIG':'{"enabled":true}','KADRIAN_SYNC_ENABLED':'1'}),patch('agent_memory_mac_keychain.Keychain') as key:
            self.assertFalse(permitted(self.cfg));self.assertEqual(main(['--config',str(self.cfg)]),2);key.assert_not_called()
    def test_stop_survives_new_process_keeps_queue_and_retained_permit(self):
        self.approve();self.assertTrue(permitted(self.cfg));stop(self.cfg);self.assertFalse(permitted(self.cfg))
        r=subprocess.run([sys.executable,'agent_memory_run.py','--config',str(self.cfg)],capture_output=True,text=True)
        self.assertEqual(r.returncode,2);self.assertEqual(json.loads(r.stdout)['status'],'locally_stopped')
        self.assertEqual((self.state/'transport.enc').read_bytes(),b'unchanged encrypted fixture');self.assertTrue((self.state/'run-permit.json').exists())
    def test_tamper_config_wrong_scope_insecure_or_symlink_permit_fails(self):
        self.approve();self.cfg.write_text(self.cfg.read_text()+' ');self.assertFalse(permitted(self.cfg));self.approve()
        p=self.state/'run-permit.json';p.chmod(0o644);self.assertFalse(permitted(self.cfg));p.unlink();p.symlink_to(self.cfg);self.assertFalse(permitted(self.cfg))
    def test_crash_recovery_needs_explicit_reapproval_no_queue_reenroll(self):
        self.approve();stop(self.cfg);self.approve();self.assertFalse(permitted(self.cfg))
        (self.state/'STOPPED').unlink() # explicit synthetic owner reapproval step
        self.assertTrue(permitted(self.cfg));self.assertEqual((self.state/'transport.enc').read_bytes(),b'unchanged encrypted fixture')
    def test_hook_honestly_reports_stopped_without_launch_or_network(self):
        with patch('agent_memory_mac_keychain.Keychain') as key:
            self.assertEqual(main(['--config',str(self.cfg),'--hook','SessionStart']),0);key.assert_not_called()
    def test_prepared_login_and_hooks_reuse_config_identity_queue_never_start(self):
        cfg=json.loads(self.cfg.read_text());cfg['scope']={'owner_id':42};self.cfg.write_text(json.dumps(cfg))
        original=self.cfg.read_bytes()
        prepare_gated_existing(self.cfg,self.root/'stage','/synthetic/python','/synthetic/code')
        plist=plistlib.loads((self.root/'stage/launch-agent.plist').read_bytes());self.assertTrue(plist['RunAtLoad'])
        self.assertIn('/synthetic/code/agent_memory_run.py',plist['ProgramArguments'])
        self.assertEqual(plist['ProgramArguments'][-1],str(self.cfg))
        hooks=json.loads((self.root/'stage/codex-hooks.json').read_text());self.assertEqual(set(hooks['hooks']),{'SessionStart','UserPromptSubmit'})
        self.assertEqual(self.cfg.read_bytes(),original);self.assertFalse((self.state/'run-permit.json').exists())
    def test_entrypoint_never_loads_environment_permissions_or_legacy_adapter(self):
        s=Path('supabase/functions/kadrian-memory-sync/index.ts').read_text()
        self.assertNotIn('KADRIAN_MEMSYNC_CONFIG',s);self.assertNotIn('SupabaseMemoryStore',s)
        self.assertIn('GatedMemoryStore',s);self.assertIn('= null',Path('supabase/functions/kadrian-memory-sync/binding.ts').read_text())

class OperatorTests(unittest.TestCase):
    def test_activation_cas_approval_and_stop_never_auto_executes(self):
        from tools.memory_gate_operator import statement
        args=(42,'kadrian','11111111-1111-4111-8111-111111111111','synthetic','a'*32)
        with self.assertRaises(ValueError):statement('enable',*args)
        sql=statement('enable',*args,generation=3,approval="owner's approval")
        self.assertIn('generation=3',sql);self.assertIn("owner''s approval",sql);self.assertIn("15 minutes",sql)
        self.assertIn('NOT enabled',sql);self.assertNotIn('interval',statement('enable',*args,generation=3,approval='owner',permanent=True))
        self.assertIn('NOT confirmed',statement('stop',*args))
        self.assertNotIn('enabled=true',statement('register',*args))

class BindingTests(unittest.TestCase):
    def test_offline_binding_exact_source_public_only_and_no_overwrite(self):
        import base64
        from tools.memory_edge_release import FILES,build
        from tools.memory_gate_binding import bind,verify_bound
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'repo';root.mkdir()
            def git(*args):return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
            git('init','-q');git('config','user.name','Synthetic');git('config','user.email','test@example.invalid')
            for name in FILES:
                f=root/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_text('// synthetic')
            git('add','.');git('commit','-qm','synthetic');sha=git('rev-parse','HEAD');source=Path(tmp)/'source';build(root,source,sha)
            scope={'owner_id':42,'agent_id':'kadrian','workspace_id':'11111111-1111-4111-8111-111111111111','project_id':'11111111-1111-4111-8111-111111111111'}
            reg=Path(tmp)/'public.json';reg.write_text(json.dumps({'synthetic':{'scope':scope,'public_key':base64.b64encode(bytes(32)).decode()}}))
            out=Path(tmp)/'bound';result=bind(source,out,reg,'a'*32,'x'*20)
            self.assertFalse(result['enabled']);self.assertEqual(result['source_sha'],sha)
            self.assertEqual(verify_bound(out)['release_id'],'a'*32)
            victim=out/'mac/agent_memory_run.py';victim.write_text('tampered')
            with self.assertRaises(ValueError):verify_bound(out)
            self.assertNotIn('Deno.env', (out/'supabase/functions/kadrian-memory-sync/binding.ts').read_text())
            with self.assertRaises(ValueError):bind(source,out,reg,'a'*32,'x'*20)
            data=json.loads(reg.read_text());data['synthetic']['private_key']='forbidden';reg.write_text(json.dumps(data))
            with self.assertRaises(ValueError):bind(source,Path(tmp)/'bad',reg,'a'*32,'x'*20)
