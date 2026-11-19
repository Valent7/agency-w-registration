"""Actual Python <-> Deno signatures, snapshots and durable Mac client, synthetic only."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import plistlib
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from agent_memory import Scope, canonical, digest, MemoryError
from agent_memory_device import SYNC_FILES, validate_snapshot
from agent_memory_edge import EdgeSync, EdgeHTTPS
from agent_memory_mac_setup import prepare


class EdgeClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deno=os.environ.get('KADRIAN_TEST_DENO') or shutil.which('deno')
        if not cls.deno:raise unittest.SkipTest('Deno required for cross-runtime tests (mandatory in Edge CI)')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.root=self.base/'workspace';(self.root/'context').mkdir(parents=True)
        for name in SYNC_FILES:(self.root/name).write_text('Synthetic initial '+name)
        (self.root/'Agent.md').write_text('Owner-approved instructions unchanged')
        self.scope=Scope(42,'kadrian','11111111-1111-4111-8111-111111111111','11111111-1111-4111-8111-111111111111')
        self.key=Ed25519PrivateKey.generate();self.raw=self.key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        public=self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        self.fixture={'scope':self.scope.as_dict(),'device_id':'synthetic','public_key':base64.b64encode(public).decode()}
        self.now=1000;self.calls=0
        self.start()
        self.sync=self.client();self.sync.initialize()

    def start(self,rows=None):
        fixture=self.base/'fixture.json';fixture.write_text(canonical({**self.fixture,'rows':rows or []}))
        self.process=subprocess.Popen([self.deno,'run','--allow-read='+str(fixture),str(Path(__file__).parent/'edge/bridge.ts'),str(fixture)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
        self.addCleanup(self.stop)

    def stop(self):
        if self.process.poll() is None:self.process.terminate();self.process.wait(timeout=5)
        for pipe in (self.process.stdin,self.process.stdout,self.process.stderr):pipe.close()

    def bridge(self,msg):
        self.process.stdin.write(canonical(msg)+'\n');self.process.stdin.flush()
        if not select.select([self.process.stdout],[],[],10)[0]:raise RuntimeError('Synthetic bridge timed out')
        line=self.process.stdout.readline()
        if not line:raise RuntimeError('Synthetic bridge failed: '+self.process.stderr.read()[:1000])
        return json.loads(line)

    def transport(self,msg):
        self.calls+=1;result=self.bridge(msg)
        if result['status']!=200:raise MemoryError('Synthetic Edge error')
        return json.loads(result['raw'])

    def client(self,transport=None,budget=None):
        return EdgeSync(self.root,self.base/'transport',self.scope,'synthetic',self.raw,transport or self.transport,clock=lambda:self.now,budget=budget)

    def seed(self,text='Cloud while Mac off',revision=4,numeric=False):
        entry={'id':'cloud','kind':'fact','text':text,'source':'synthetic','certainty':'verified','status':'recorded','related':[],
            'evidence':{'n':1.0,'tiny':1e-7,'large':9007199254740993,'unicode':'😀\uE000'} if numeric else {}}
        doc={'schema':1,'entries':{'cloud':entry},'tasks':{}}
        row={'revision':revision,'document':doc,'hash':digest(doc),'event':{},'idempotency_key':'seed','created_at':'2026-11-19T00:00:00Z'}
        self.bridge({'control':'seed','scope':self.scope.as_dict(),'row':canonical(row)})
        return doc

    def test_roundtrip_python_signature_and_legacy_numeric_hash(self):
        original=self.seed(numeric=True)
        status=self.sync.cycle();self.assertEqual(status['status'],'current');self.assertEqual(status['pending'],0)
        snapshot=self.sync.load()['edge_snapshot'];validate_snapshot(snapshot,self.scope)
        self.assertEqual(snapshot['snapshot']['document']['entries']['cloud'],original['entries']['cloud'])
        self.assertEqual(snapshot['snapshot']['document']['entries']['cloud']['evidence']['large'],9007199254740993)
        self.assertEqual(snapshot['snapshot']['hash'],digest(snapshot['snapshot']['document']))

    def test_idle_one_small_request_no_full_read_or_write_and_hook_coalescing(self):
        self.sync.cycle();before=self.bridge({'control':'stats'});calls=self.calls
        self.sync.cycle(max_events=0);self.assertEqual(self.calls,calls)
        self.now+=61;self.sync.cycle();after=self.bridge({'control':'stats'})
        self.assertEqual(self.calls,calls+1);self.assertEqual(after['reads'],before['reads']);self.assertEqual(after['writes'],before['writes'])
        status=json.loads((self.base/'transport/status.json').read_text());self.assertLess(status['usage']['response_bytes'],20000)

    def test_offline_edits_restart_reconnect_preserves_all_and_no_duplicates(self):
        self.sync.transport=lambda e:(_ for _ in ()).throw(OSError('offline'))
        self.sync.cycle();(self.root/SYNC_FILES[0]).write_text('Offline second version');self.sync.cycle()
        self.assertEqual(len(self.sync.load()['pending']),2)
        self.assertNotIn(b'Offline second version',self.sync.state_path.read_bytes())
        self.now+=61;restart=self.client();status=restart.cycle();self.assertEqual(status['status'],'current');self.assertEqual(status['pending'],0)
        document=restart.load()['edge_snapshot']['snapshot']['document'];self.assertEqual(len(document['entries']),2)
        revision=status['cloud_revision'];self.now+=61;self.assertEqual(restart.cycle()['cloud_revision'],revision)
        self.assertIn('Offline second version',(self.root/'context/SYNCED_MEMORY.md').read_text())

    def test_backoff_persists_across_mac_client_restart(self):
        self.sync.transport=lambda e:(_ for _ in ()).throw(OSError('offline'));self.sync.cycle()
        calls=self.calls;self.assertEqual(self.client().cycle()['status'],'offline_or_blocked');self.assertEqual(self.calls,calls)
        self.now+=61;self.assertEqual(self.client().cycle()['status'],'current')

    def test_lost_ack_reconciles_no_second_revision(self):
        # Get base without sending captured events; then lose commit response.
        self.sync.cycle(max_events=0);self.bridge({'control':'fault','lost':True});self.now+=61
        self.assertEqual(self.sync.cycle()['status'],'offline_or_blocked');before=self.bridge({'control':'stats'})
        self.now+=61;self.assertEqual(self.client().cycle()['pending'],0);after=self.bridge({'control':'stats'})
        self.assertEqual(before['writes'],after['writes']);self.assertEqual(after['writes'],1)

    def test_concurrent_cloud_and_local_changes_keep_versions_and_encrypted_conflict(self):
        state=self.sync.load();self.sync.capture(state);self.seed()
        self.assertEqual(self.sync.cycle()['status'],'current');self.assertIn('Cloud while Mac off',(self.root/'context/SYNCED_MEMORY.md').read_text())
        self.assertEqual(len(list((self.base/'transport').glob('conflict-*.enc'))),1)
        self.assertEqual((self.root/'Agent.md').read_text(),'Owner-approved instructions unchanged')

    def test_cloud_function_process_restart_and_mac_restart(self):
        self.sync.cycle();rows=self.bridge({'control':'stats'})['rows'];self.stop();self.start(rows)
        self.seed('New after server restart',revision=5);self.now+=61
        self.assertEqual(self.client().cycle()['status'],'current');self.assertIn('New after server restart',(self.root/'context/SYNCED_MEMORY.md').read_text())

    def test_budget_pauses_retains_queue_and_warns_without_claiming_global_usage(self):
        client=self.client(budget={'requests':1,'response_bytes':1_000_000_000})
        client.cycle();self.now+=61;status=client.cycle()
        self.assertEqual(status['status'],'budget_paused');self.assertEqual(status['pending'],1)
        self.assertTrue(any('не общий расход' in w for w in status['warnings']))

    def test_quota_reservation_survives_failed_request(self):
        self.sync.transport=lambda e:(_ for _ in ()).throw(OSError('offline'))
        self.sync.cycle();usage=self.sync.load()['edge_usage'];self.assertEqual(usage['requests'],1);self.assertEqual(usage['response_bytes'],3_000_000)

    def test_tampered_scope_or_unexpected_changed_metadata_retains_queue(self):
        self.sync.cycle(max_events=0);self.now+=61
        original=self.transport
        def altered(e):
            r=original(e);r['known']['project']['hash']='0'*64;r['snapshot']=None;return r
        self.sync.transport=altered;self.assertEqual(self.sync.cycle()['status'],'offline_or_blocked');self.assertEqual(len(self.sync.load()['pending']),1)

    def test_manually_changed_projection_is_backed_up_and_rebuilt_without_full_poll(self):
        self.sync.cycle();projection=self.root/'context/SYNCED_MEMORY.md';projection.write_text('Manual projection version')
        calls=self.calls;self.sync.cycle(max_events=0);self.assertEqual(self.calls,calls)
        self.assertNotEqual(projection.read_text(),'Manual projection version')
        self.assertTrue(any(p.read_text()=='Manual projection version' for p in (self.root/'.memory-backups').glob('*.md')))

    def test_monthly_local_budget_reset_keeps_durable_pending(self):
        self.sync.transport=lambda e:(_ for _ in ()).throw(OSError('offline'))
        self.sync.cycle();state=self.sync.load();self.assertEqual(state['edge_usage']['requests'],1)
        self.now=32*86400;usage=self.sync._usage(state);self.assertEqual(usage['requests'],0);self.assertEqual(len(state['pending']),1)

    def test_projection_lock_released_after_forced_process_exit(self):
        from agent_memory import Conflict
        self.sync.cycle();state=self.sync.load();snapshot=state['edge_snapshot']
        lock=self.root/'.memory-projection.lock'
        code="import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)"
        process=subprocess.Popen([sys.executable,'-c',code,str(lock)],stdout=subprocess.PIPE,text=True)
        try:
            self.assertTrue(select.select([process.stdout],[],[],5)[0]);self.assertEqual(process.stdout.readline().strip(),'locked')
            with self.assertRaises(Conflict):self.sync.apply_snapshot(state,snapshot)
            process.kill();process.wait(timeout=5)
            self.sync.apply_snapshot(state,snapshot)  # no stale-lock deletion required
            self.assertTrue(lock.exists())
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=5)
            process.stdout.close()

    def test_launchd_assets_codex_hooks_scoped_and_not_installed(self):
        cfg=prepare(self.root,self.base/'stage',Path('/synthetic/python'),Path('/synthetic/code'),self.scope,'https://synthetic.supabase.co/functions/v1/kadrian-memory-sync')
        self.assertEqual(cfg['backend'],'supabase-edge');self.assertNotIn('service_role',canonical(cfg))
        plist=plistlib.loads((self.base/'stage/launch-agent.plist').read_bytes());self.assertTrue(plist['RunAtLoad']);self.assertEqual(plist['StartInterval'],60)
        self.assertEqual(plist['WatchPaths'],[str(self.root/name) for name in SYNC_FILES])
        hooks=json.loads((self.base/'stage/codex-hooks.json').read_text());self.assertEqual(set(hooks['hooks']),{'SessionStart','UserPromptSubmit'})

    def test_edge_endpoint_rejects_redirect_credentials_wronghost_and_query(self):
        for url in ['http://synthetic.supabase.co/functions/v1/kadrian-memory-sync','https://evil.test/functions/v1/kadrian-memory-sync','https://u:p@synthetic.supabase.co/functions/v1/kadrian-memory-sync','https://synthetic.supabase.co/functions/v1/kadrian-memory-sync?token=x','https://synthetic.supabase.co:443/functions/v1/kadrian-memory-sync']:
            with self.assertRaises(MemoryError):EdgeHTTPS(url)


if __name__=='__main__':unittest.main()
