"""Actual local PostgreSQL gate transactions, scopes, privileges, stop races.
No production DSN accepted. Uses existing test fixture safety check.
"""
from pathlib import Path
import unittest
from uuid import uuid4
import test_agent_memory_postgres as pg
from tools.memory_gate_operator import statement
from agent_memory import canonical,digest

@unittest.skipUnless(pg.DSN,'Local PostgreSQL test DSN required')
class GatePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg.PostgresMemoryTests.setUpClass();cls.db=pg.PostgresMemoryTests.db;cls.psycopg=pg.PostgresMemoryTests.psycopg
        cls.db.execute(Path('deploy/memory-edge/safety-gate.sql').read_text())
    @classmethod
    def tearDownClass(cls):pg.PostgresMemoryTests.tearDownClass()
    def setUp(self):
        self.owner=42;self.project=str(uuid4());self.release=uuid4().hex;self.device='synthetic';self.generation=1
        self.scope={'owner_id':42,'agent_id':'kadrian','workspace_id':self.project,'project_id':self.project}
        self.db.execute('INSERT INTO public.content_factory_projects VALUES(%s,%s)',(self.project,42))
        self.op('register')
    def op(self,action,**kw):
        self.db.execute(statement(action,42,'kadrian',self.project,self.device,self.release,**kw))
    def call(self,action='begin',connection=None,scope=None,generation=None,device=None,release=None,**extra):
        args={'p_action':action,'p_release':release or self.release,'p_device':device or self.device,'p_owner':42,'p_agent':'kadrian','p_workspace':self.project,'p_project':self.project,
          'p_generation':generation if generation is not None else self.generation,'p_scope':canonical(scope or self.scope),**extra}
        jsonargs={'p_scope','p_document','p_event'}
        sql='SELECT public.kadrian_memory_exchange('+','.join(k+'=>%s'+('::jsonb' if k in jsonargs else '') for k in args)+')'
        return (connection or self.db).execute(sql,list(args.values())).fetchone()[0]
    def activate(self):self.op('enable',generation=1,approval='synthetic owner');self.generation=2
    def test_disabled_defaults_expiry_missing_and_foreign_bindings(self):
        with self.assertRaises(self.psycopg.Error):self.call()
        self.activate();self.assertEqual(self.call()['generation'],2)
        for kwargs in ({'device':'foreign'},{'release':'0'*32},{'generation':1,'action':'read'},{'scope':{'owner_id':42},'action':'read'},{'scope':{**self.scope,'owner_id':43},'action':'read'}, {'scope':{**self.scope,'agent_id':'neona'},'action':'read'}):
            with self.assertRaises(self.psycopg.Error):self.call(**kwargs)
        self.db.execute("UPDATE kadrian_sync_control.gate SET expires_at=clock_timestamp()-interval '1 second' WHERE workspace_id=%s",(self.project,))
        with self.assertRaises(self.psycopg.Error):self.call()
    def test_runtime_cannot_open_gate_and_public_roles_cannot_use_rpc(self):
        self.activate()
        for role in ('anon','authenticated','service_role'):
            self.db.execute('SET ROLE '+role)
            try:
                with self.assertRaises(self.psycopg.Error):self.db.execute('UPDATE kadrian_sync_control.gate SET enabled=true')
                if role!='service_role':
                    with self.assertRaises(self.psycopg.Error):self.call()
                else:self.assertEqual(self.call()['generation'],2)
            finally:self.db.execute('RESET ROLE')
    def test_stop_commit_then_old_generation_denied_restart_is_closed_until_approval(self):
        self.activate();self.call('metadata');self.op('stop')
        for action in ('begin','read','metadata','commit'):
            with self.assertRaises(self.psycopg.Error):self.call(action)
        with self.assertRaises(self.psycopg.Error):self.op('enable',generation=2,approval='old stale approval')
        # Failed transaction explicitly rolled back before new approval.
        self.db.execute('ROLLBACK')
        self.op('enable',generation=3,approval='new synthetic owner');self.generation=4
        self.assertEqual(self.call()['generation'],4)
        with self.assertRaises(self.psycopg.Error):self.call('metadata',generation=2)
    def test_share_lock_serializes_stop_and_no_false_success_on_timeout(self):
        self.activate()
        a=self.psycopg.connect(pg.DSN);b=self.psycopg.connect(pg.DSN,autocommit=True)
        try:
            self.call('metadata',connection=a)
            b.execute("SET lock_timeout='100ms'")
            with self.assertRaises(self.psycopg.Error):
                b.execute('UPDATE kadrian_sync_control.gate SET enabled=false,generation=generation+1 WHERE workspace_id=%s',(self.project,))
            # Stop was NOT acknowledged; original operation can finish first.
            a.commit();self.op('stop')
            with self.assertRaises(self.psycopg.Error):self.call('metadata',connection=b)
        finally:a.close();b.close()
    def test_original_memory_idempotency_and_rules_remain_unchanged(self):
        self.activate();doc={'schema':1,'entries':{},'tasks':{}}
        kw={'p_expected':0,'p_document':canonical(doc),'p_event':'{"kind":"synthetic"}','p_key':'same-event','p_hash':digest(doc)}
        self.assertEqual(self.call('commit',**kw)[0]['revision'],1)
        self.assertEqual(self.call('commit',**kw)[0]['revision'],1)
        self.op('stop')
        with self.assertRaises(self.psycopg.Error):self.call('commit',**{**kw,'p_expected':1,'p_key':'new-event'})
        self.assertEqual(self.db.execute('SELECT count(*) FROM public.agent_memory_revisions WHERE workspace_id=%s',(self.project,)).fetchone()[0],1)
        self.assertFalse(self.db.execute("SELECT prosecdef FROM pg_proc WHERE proname='agent_memory_commit'").fetchone()[0])

    def test_actual_python_deno_postgres_exchange_replay_stop_recovery(self):
        import base64,json,os,select,shutil,subprocess,tempfile
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        from agent_memory_edge import EdgeSync
        from agent_memory import Scope,MemoryError
        deno=os.environ.get('KADRIAN_TEST_DENO') or shutil.which('deno')
        if not deno:self.skipTest('Deno required')
        self.activate()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'context').mkdir()
            from agent_memory_device import SYNC_FILES
            for name in SYNC_FILES:(root/name).write_text('Synthetic context '+name)
            (root/'Agent.md').write_text('Owner rule stays')
            key=Ed25519PrivateKey.generate();raw=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
            fixture=root/'fixture.json';fixture.write_text(canonical({'scope':self.scope,'release':self.release,'public_key':base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()}))
            process=subprocess.Popen([deno,'run','--allow-read='+str(fixture),'tests/edge/gate_pg_bridge.ts',str(fixture)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
            lost_ack=[False]
            def transport(envelope):
                process.stdin.write(canonical({'envelope':envelope,'now':now[0]})+'\n');process.stdin.flush()
                while True:
                    if not select.select([process.stdout],[],[],10)[0]:raise RuntimeError('Bridge timeout')
                    line=process.stdout.readline()
                    if not line:raise RuntimeError(process.stderr.read()[:1000])
                    message=json.loads(line)
                    if message.get('done'):
                        if message['status']!=200:raise MemoryError('Gate closed')
                        value=json.loads(message['raw'])
                        if lost_ack[0] and value.get('ack'):
                            lost_ack[0]=False;raise OSError('Synthetic lost ack after actual commit')
                        return value
                    args=json.loads(message['rpc']);params=[];parts=[]
                    for name,value in args.items():
                        isjson=name in {'p_scope','p_document','p_event'}
                        parts.append(name+'=>%s'+('::jsonb' if isjson else ''));params.append(canonical(value) if isjson else value)
                    try:
                        self.db.execute('SET ROLE service_role')
                        value=self.db.execute('SELECT public.kadrian_memory_exchange('+','.join(parts)+')',params).fetchone()[0]
                        reply={'status':200,'raw':canonical(value)}
                    except self.psycopg.Error:reply={'status':403,'raw':'{}'}
                    finally:self.db.execute('RESET ROLE')
                    process.stdin.write(json.dumps(reply)+'\n');process.stdin.flush()
            try:
                now=[1000]
                def client():return EdgeSync(root,root/'queue',Scope(**self.scope),'synthetic',raw,transport,clock=lambda:now[0])
                sync=client();sync.initialize();self.assertEqual(sync.cycle()['cloud_revision'],1)
                # Stop server; retain pending locally, process restart must not clear stop.
                self.op('stop');(root/'context/FEEDBACK.md').write_text('Synthetic offline correction');now[0]+=61
                self.assertEqual(client().cycle()['status'],'offline_or_blocked');self.assertEqual(len(client().load()['pending']),1)
                now[0]+=61;self.assertEqual(client().cycle()['status'],'offline_or_blocked')
                self.op('enable',generation=3,approval='explicit synthetic restart');now[0]+=121
                lost_ack[0]=True
                self.assertEqual(client().cycle()['status'],'offline_or_blocked')
                self.assertEqual(len(client().load()['pending']),1)
                now[0]+=601 # durable exponential backoff also survives lost ack
                status=client().cycle();self.assertEqual(status['pending'],0);self.assertEqual(status['cloud_revision'],2)
                now[0]+=61;self.assertEqual(client().cycle()['cloud_revision'],2)
                self.assertEqual((root/'Agent.md').read_text(),'Owner rule stays')
            finally:
                process.terminate();process.wait(timeout=5)
                for pipe in (process.stdin,process.stdout,process.stderr):pipe.close()
