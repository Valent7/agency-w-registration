"""Optional real PostgreSQL tests. Refuse non-local/non-test DSNs by construction."""
import os
from pathlib import Path
from urllib.parse import urlparse
import unittest
from uuid import uuid4

DSN = os.environ.get('AGENCY_MEMORY_TEST_DSN')


@unittest.skipUnless(DSN, 'Local PostgreSQL test DSN not configured')
class PostgresMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url=urlparse(DSN)
        if url.hostname not in {'localhost','127.0.0.1','::1'} or url.path != '/agent_memory_test':
            raise RuntimeError('Only localhost/agent_memory_test is allowed')
        import psycopg
        cls.psycopg=psycopg
        cls.db=psycopg.connect(DSN,autocommit=True)
        with cls.db.cursor() as c:
            c.execute("DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon; END IF; IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated; END IF; IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role BYPASSRLS; END IF; END $$;")
            c.execute('CREATE TABLE IF NOT EXISTS public.content_factory_projects(id uuid NOT NULL,owner_id bigint NOT NULL,UNIQUE(id,owner_id))')
            c.execute(Path('agent_memory_setup.sql').read_text())
            c.execute('GRANT SELECT ON public.content_factory_projects TO service_role')

    @classmethod
    def tearDownClass(cls):cls.db.close()

    def setUp(self):
        self.project=uuid4();self.workspace=str(uuid4());self.owner=111
        with self.db.cursor() as c:c.execute('INSERT INTO content_factory_projects VALUES(%s,%s)',(self.project,self.owner))

    def commit(self,c,expected=0,document=None,key='test'):
        from psycopg.types.json import Jsonb
        from agent_memory import digest
        doc=document or {'schema':1,'entries':{},'tasks':{}}
        c.execute('SELECT revision,document FROM agent_memory_commit(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (self.owner,'synthetic',self.workspace,self.project,expected,Jsonb(doc),Jsonb({'kind':'test'}),key,digest(doc)))
        return c.fetchone()

    def test_atomic_cas_idempotency_and_append_only_history(self):
        with self.db.cursor() as c:
            self.assertEqual(self.commit(c)[0],1)
            self.assertEqual(self.commit(c)[0],1)
            with self.assertRaises(self.psycopg.Error):self.commit(c,key='other')
            self.assertEqual(self.commit(c,expected=1,key='next')[0],2)
            with self.assertRaises(self.psycopg.Error):c.execute('DELETE FROM agent_memory_revisions WHERE workspace_id=%s',(self.workspace,))
            with self.assertRaises(self.psycopg.Error):c.execute('UPDATE agent_memory_revisions SET hash=%s WHERE workspace_id=%s',('a'*64,self.workspace))

    def test_anon_authenticated_cannot_read_or_call_rpc(self):
        for role in ('anon','authenticated'):
            with self.db.cursor() as c:
                c.execute('SET ROLE '+role)
                try:
                    with self.assertRaises(self.psycopg.Error):c.execute('SELECT * FROM agent_memory_revisions')
                    with self.assertRaises(self.psycopg.Error):self.commit(c)
                finally:c.execute('RESET ROLE')

    def test_service_role_must_match_project_owner_and_bound_workspace(self):
        with self.db.cursor() as c:
            c.execute('SET ROLE service_role')
            try:
                self.assertEqual(self.commit(c)[0],1)
                self.owner=222
                with self.assertRaises(self.psycopg.Error):self.commit(c,key='foreign-owner')
                self.owner=111;self.project=uuid4()
                with self.assertRaises(self.psycopg.Error):self.commit(c,key='foreign-project')
            finally:c.execute('RESET ROLE')

    def test_rpc_is_invoker_rls_enabled_and_no_delete_grant(self):
        with self.db.cursor() as c:
            c.execute("SELECT prosecdef FROM pg_proc WHERE proname='agent_memory_commit'")
            self.assertFalse(c.fetchone()[0])
            c.execute("SELECT bool_and(relrowsecurity) FROM pg_class WHERE relname IN ('agent_memory_workspaces','agent_memory_revisions')")
            self.assertTrue(c.fetchone()[0])
            c.execute("SELECT has_table_privilege('service_role','agent_memory_revisions','DELETE')")
            self.assertFalse(c.fetchone()[0])
