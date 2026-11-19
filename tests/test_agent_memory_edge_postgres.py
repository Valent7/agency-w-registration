"""Feed an actual Deno-produced document through unchanged PR85 PostgreSQL RPC.

Only localhost/agent_memory_test accepted by the existing PostgreSQL fixture.
This tests JSONB/hash compatibility, not hosted PostgREST or production access.
"""
import os
import shutil
import unittest
import test_agent_memory_postgres as pg
import test_agent_memory_edge as edge
from agent_memory import canonical, digest


@unittest.skipUnless(pg.DSN and (os.environ.get('KADRIAN_TEST_DENO') or shutil.which('deno')),'Local test PostgreSQL and Deno required (mandatory in memory CI)')
class EdgePostgresCompatibilityTests(unittest.TestCase):
    def test_edge_append_numeric_hash_through_existing_atomic_rpc(self):
        pg.PostgresMemoryTests.setUpClass();self.addCleanup(pg.PostgresMemoryTests.tearDownClass)
        database=pg.PostgresMemoryTests();database.setUp()
        edge.EdgeClientTests.setUpClass();client=edge.EdgeClientTests();client.setUp();self.addCleanup(client.doCleanups)
        client.seed(numeric=True);self.assertEqual(client.sync.cycle()['status'],'current')
        snap=client.sync.load()['edge_snapshot']['snapshot'];raw=canonical(snap['document']);expected_hash=digest(snap['document'])
        self.assertEqual(expected_hash,snap['hash'])
        self.assertIn('1.0',raw);self.assertIn('1e-07',raw);self.assertIn('9007199254740993',raw)
        params=(database.owner,'synthetic',database.workspace,str(database.project),0,raw,canonical({'kind':'edge-compatibility-test'}),'edge-document',expected_hash)
        sql='SELECT revision,document,hash FROM agent_memory_commit(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)'
        with database.db.cursor() as cursor:
            cursor.execute('SET ROLE service_role')
            try:
                cursor.execute(sql,params);revision,document,doc_hash=cursor.fetchone()
                self.assertEqual(revision,1);self.assertEqual(document,snap['document']);self.assertEqual(digest(document),doc_hash)
                cursor.execute(sql,params);self.assertEqual(cursor.fetchone()[0],1)
                with self.assertRaises(database.psycopg.Error):cursor.execute(sql,(*params[:7],'different-key',expected_hash))
            finally:cursor.execute('RESET ROLE')
