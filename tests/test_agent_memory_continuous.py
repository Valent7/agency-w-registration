"""Synthetic project + in-process server only. No network, Keychain or launchd changes."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import plistlib
import tempfile
import time
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from agent_memory import Scope, Principal, Memory, MemoryError, Conflict, digest, canonical
from agent_memory_store import Repository
from agent_memory_device import DeviceService, SYNC_FILES
from agent_memory_daemon import Sync, HTTPS, exclusive
from agent_memory_mac_setup import prepare, enroll
from content_factory_store import SQLiteStore


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.root=self.base/'workspace';(self.root/'context').mkdir(parents=True)
        for name in SYNC_FILES:(self.root/name).write_text('Synthetic initial '+name)
        (self.root/'Agent.md').write_text('General rule: owner approval only')
        self.store=SQLiteStore(self.base/'cloud.sqlite3');p=self.store.save_project(11,{'name':'Synthetic','facts':'Preserve original'})
        self.scope=Scope(11,'kadrian',p['id'],p['id']);self.repo=Repository(self.store,initialize=True)
        self.owner=Memory(self.repo,Principal(11,'synthetic-owner','owner'),self.scope)
        self.key=Ed25519PrivateKey.generate();self.raw=self.key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        public=self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        self.devices={'test-device':{'public_key':base64.b64encode(public).decode(),'scope':self.scope.as_dict()}}
        self.server=DeviceService(self.repo,self.devices,lambda owner,project:(owner,project)==(11,p['id']))
        self.sync=Sync(self.root,self.base/'transport',self.scope,'test-device',self.raw,self.server.handle)
        self.sync.initialize()

    def cloud(self,text='Synthetic cloud fact',key='cloud'):
        return self.owner.record('fact',text,source='synthetic',key=key)

    def test_roundtrip_no_duplicate_poll_and_cloud_changes(self):
        self.cloud();self.assertEqual(self.sync.cycle()['status'],'current')
        initial=self.owner.read()['revision'];self.sync.cycle();self.assertEqual(self.owner.read()['revision'],initial)
        self.cloud('Cloud while Mac off','while-off');self.sync.cycle()
        self.assertIn('Cloud while Mac off',(self.root/'context/SYNCED_MEMORY.md').read_text())
        self.assertEqual(self.store.get_project(11,self.scope.project_id)['facts'],'Preserve original')

    def test_offline_multiple_edits_restart_and_reconnect(self):
        self.sync.transport=lambda e:(_ for _ in ()).throw(OSError('offline'))
        self.sync.cycle();(self.root/SYNC_FILES[0]).write_text('Offline version two');self.sync.cycle()
        self.assertEqual(len(self.sync.load()['pending']),2)
        self.assertNotIn(b'Offline version two',self.sync.state_path.read_bytes())
        restart=Sync(self.root,self.base/'transport',self.scope,'test-device',self.raw,self.server.handle)
        self.assertEqual(restart.cycle()['pending'],0)
        texts=' '.join(e['text'] for e in self.owner.read()['document']['entries'].values())
        self.assertIn('Offline version two',texts);self.assertIn('Synthetic initial',texts)

    def test_lost_ack_after_commit_does_not_duplicate(self):
        def lose(envelope):
            result=self.server.handle(envelope)
            if envelope['request']['operation']=='push':raise OSError('lost response')
            return result
        self.sync.transport=lose;self.assertEqual(self.sync.cycle()['pending'],1)
        revision=self.owner.read()['revision'];self.sync.transport=self.server.handle
        self.assertEqual(self.sync.cycle()['pending'],0);self.assertEqual(self.owner.read()['revision'],revision)

    def test_conflict_preserves_both_and_archives_local_version(self):
        state=self.sync.load();self.sync.capture(state);self.cloud('Concurrent cloud decision')
        self.assertEqual(self.sync.cycle()['status'],'current')
        text=(self.root/'context/SYNCED_MEMORY.md').read_text()
        self.assertIn('Concurrent cloud decision',text);self.assertIn('Synthetic initial',text)
        files=list((self.base/'transport').glob('conflict-*.enc'));self.assertEqual(len(files),1)
        conflict=json.loads(self.sync.cipher.decrypt(files[0].read_bytes()));self.assertTrue(conflict['ack']['conflict'])

    def test_cloud_CAS_race_retried_without_overwrite(self):
        original=self.repo.commit;called=[]
        def racing(scope,expected,doc,event,key):
            if not called:
                called.append(True);self.cloud('Winning concurrent writer','race')
            return original(scope,expected,doc,event,key)
        with patch.object(self.repo,'commit',racing):self.sync.cycle()
        self.assertEqual(len(self.owner.read()['document']['entries']),2)

    def test_general_rules_unchanged_and_device_cannot_approve(self):
        (self.root/'Agent.md').write_text('Changed general instructions never uploaded')
        self.sync.cycle();text=(self.root/'context/SYNCED_MEMORY.md').read_text()
        self.assertNotIn('Changed general instructions',text)
        self.assertEqual((self.root/'Agent.md').read_text(),'Changed general instructions never uploaded')
        self.assertFalse(any(e['kind']=='rule' for e in self.owner.read()['document']['entries'].values()))
        state=self.sync.load();event={'sequence':2,'base_revision':state['cloud_revision'],'files':{'Agent.md':'approve me'},'file_modes':{}}
        event['event_id']=digest({'device':'test-device',**event})
        with self.assertRaises(MemoryError):self.sync.request('push',event)
        with self.assertRaises(MemoryError):self.sync.request('approve',{})

    def test_identity_signature_revocation_and_clock(self):
        request={'format':'agency-device-sync/1','device_id':'test-device','scope':self.scope.as_dict(),
                 'operation':'pull','payload':{},'issued_at':int(time.time()),'request_id':'synthetic'}
        def signed(r):return {'request':r,'signature':base64.b64encode(self.key.sign(canonical(r).encode())).decode()}
        tampered=signed(request);tampered['request']=deepcopy(request);tampered['request']['operation']='push'
        with self.assertRaises(Exception):self.server.handle(tampered)
        wrong=deepcopy(request);wrong['scope']['owner_id']=12
        with self.assertRaises(MemoryError):self.server.handle(signed(wrong))
        expired=deepcopy(request);expired['issued_at']-=301
        with self.assertRaises(MemoryError):self.server.handle(signed(expired))
        self.devices['test-device']['revoked']=True
        with self.assertRaises(MemoryError):self.server.handle(signed(request))

    def test_credentials_rejected_before_network(self):
        (self.root/SYNC_FILES[0]).write_text('api_key: sk-'+('x'*30))
        with self.assertRaises(MemoryError):self.sync.cycle()
        self.assertEqual(self.owner.read()['revision'],0)

    def test_symlink_and_lock(self):
        with exclusive(self.base/'transport'):
            with self.assertRaises(BlockingIOError):self.sync.cycle()
        (self.root/SYNC_FILES[0]).unlink();(self.root/SYNC_FILES[0]).symlink_to(self.root/'Agent.md')
        with self.assertRaises(MemoryError):self.sync.cycle()

    def test_projection_edit_backup_and_no_source_overwrite(self):
        self.sync.cycle();projection=self.root/'context/SYNCED_MEMORY.md';projection.write_text('Manual projection edit')
        self.sync.cycle();self.assertTrue(any('Manual projection edit'==p.read_text() for p in (self.root/'.memory-backups').glob('*.md')))
        self.assertEqual((self.root/SYNC_FILES[0]).read_text(),'Synthetic initial '+SYNC_FILES[0])

    def test_rewound_cloud_blocks_and_retains_pending(self):
        empty=self.sync.request('pull',{});self.sync.cycle();self.sync.transport=lambda e:empty
        (self.root/SYNC_FILES[1]).write_text('New decision');status=self.sync.cycle()
        self.assertEqual(status['status'],'offline_or_blocked');self.assertEqual(status['pending'],1)

    def test_HTTPS_rejects_insecure_redirect_destination_and_credentials(self):
        for url in ['http://example.test/memory-sync/v1/exchange','https://user:pass@example.test/memory-sync/v1/exchange','https://example.test/path','https://example.test/memory-sync/v1/exchange?q=secret']:
            with self.assertRaises(MemoryError):HTTPS(url)

    def test_queue_corruption_is_not_discarded(self):
        self.sync.state_path.write_bytes(b'broken');before=self.sync.state_path.read_bytes()
        with self.assertRaises(Exception):self.sync.cycle()
        self.assertEqual(before,self.sync.state_path.read_bytes())

    def test_staging_and_enrollment_with_fake_keychain(self):
        output=self.base/'stage'
        cfg=prepare(self.root,output,Path('/synthetic/python'),Path('/synthetic/code'),self.scope,'https://example.test/memory-sync/v1/exchange')
        plist=plistlib.loads((output/'launch-agent.plist').read_bytes());self.assertTrue(plist['RunAtLoad']);self.assertEqual(plist['StartInterval'],60)
        self.assertNotIn(str(self.root/'context/SYNCED_MEMORY.md'),plist['WatchPaths'])
        hooks=json.loads((output/'codex-hooks.json').read_text());self.assertEqual(set(hooks['hooks']),{'SessionStart','UserPromptSubmit'})
        class Keys:
            def create(self,account,secret):self.account,self.secret=account,secret
        keys=Keys();registry=enroll(output/'config.json',keys)
        self.assertEqual(len(keys.secret),32);self.assertEqual(registry[cfg['device_id']]['scope'],self.scope.as_dict())
        self.assertNotIn(base64.b64encode(keys.secret).decode(),''.join(p.read_text() for p in output.glob('*.json')))
        with self.assertRaises(MemoryError):enroll(output/'config.json',keys)

    def test_sequence_reuse_changed_payload_is_rejected(self):
        state=self.sync.load();self.sync.capture(state);event=state['pending'][0];self.sync.request('push',event)
        changed=deepcopy(event);changed['files'][SYNC_FILES[0]]='different';changed['file_modes'][SYNC_FILES[0]]['result_hash']=digest('different');changed['event_id']=digest({'device':'test-device',**{k:v for k,v in changed.items() if k!='event_id'}})
        with self.assertRaises(Conflict):self.sync.request('push',changed)

    def test_append_only_sends_new_text_not_entire_existing_journal(self):
        self.sync.cycle()
        path=self.root/SYNC_FILES[1];old=path.read_text();path.write_text(old+'\nNew synthetic decision')
        state=self.sync.load();self.sync.capture(state);event=state['pending'][0]
        self.assertEqual(event['files'][SYNC_FILES[1]],'\nNew synthetic decision')
        self.assertEqual(event['file_modes'][SYNC_FILES[1]]['mode'],'append')
        self.assertEqual(event['file_modes'][SYNC_FILES[1]]['base_hash'],digest(old))
        self.sync.cycle();self.assertIn('New synthetic decision',(self.root/'context/SYNCED_MEMORY.md').read_text())

    def test_hook_keychain_failure_warns_without_false_freshness(self):
        import io
        from contextlib import redirect_stdout
        from agent_memory_daemon import main
        cfg={'root':str(self.root),'state_dir':str(self.base/'transport'),'device_id':'test-device',
             'scope':self.scope.as_dict(),'endpoint':'https://example.test/memory-sync/v1/exchange'}
        config=self.base/'config.json';config.write_text(json.dumps(cfg));config.chmod(0o600)
        with patch('agent_memory_mac_keychain.Keychain',side_effect=RuntimeError('Synthetic locked keychain')):
            out=io.StringIO()
            with redirect_stdout(out):code=main(['--config',str(config),'--hook','UserPromptSubmit'])
        self.assertEqual(code,0)
        context=json.loads(out.getvalue())['hookSpecificOutput']['additionalContext']
        self.assertIn('не подтверждена',context)
        self.assertEqual(json.loads((self.base/'transport/status.json').read_text())['status'],'offline_or_blocked')

    def test_split_credential_is_rejected_before_delta_upload(self):
        path=self.root/SYNC_FILES[1];path.write_text('api_key: ');self.sync.cycle()
        path.write_text('api_key: sk-'+('x'*30))
        with self.assertRaises(MemoryError):self.sync.cycle()

    def test_approved_parent_rules_return_without_private_parent_facts(self):
        scope=Scope(11,'*','owner');owner=Memory(self.repo,Principal(11,'synthetic-owner','owner'),scope)
        owner.record('fact','Private parent note not for project export',source='synthetic',key='private')
        owner.record('rule','Approved shared behavior',source='synthetic',key='rule')
        rule_id=digest({'scope':scope.as_dict(),'key':'rule'})[:32]
        owner.record('check','Synthetic check',source='test',key='check',related=[rule_id],evidence={'passed':True,'rule_hash':digest('Approved shared behavior')})
        check_id=digest({'scope':scope.as_dict(),'key':'check'})[:32]
        owner.validate_rule(rule_id,[check_id],expected=owner.read()['revision'])
        owner.approve_rule(rule_id,expected=owner.read()['revision'])
        owner.record('rule','Unapproved idea must stay off',source='synthetic',key='unapproved')
        self.sync.cycle();text=(self.root/'context/SYNCED_MEMORY.md').read_text()
        self.assertIn('Approved shared behavior',text)
        self.assertNotIn('Private parent note',text);self.assertNotIn('Unapproved idea',text)
        self.assertEqual((self.root/'Agent.md').read_text(),'General rule: owner approval only')

    def test_parent_scope_and_rewind_are_rejected(self):
        from agent_memory_device import validate_snapshot
        snapshot=self.sync.request('pull',{});changed=deepcopy(snapshot)
        changed['parents'][0]['scope']['owner_id']=12
        with self.assertRaises(MemoryError):validate_snapshot(changed,self.scope)
        self.sync.cycle();self.sync.transport=lambda e:{'scope':self.scope.as_dict(),'snapshot':snapshot['snapshot']}
        self.assertEqual(self.sync.cycle()['status'],'offline_or_blocked')
