"""Execute the documented local-only recipes on synthetic files and memory."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from agent_memory import Memory, Principal, Scope
from agent_memory_store import Repository
from agent_memory_sync import import_local, local_bundle, FILES
from content_factory_store import SQLiteStore


class SyncGuideTests(unittest.TestCase):
    def test_guide_recipes_complete_both_directions_without_rewriting_originals(self):
        guide=Path(__file__).resolve().parents[1]/'docs/KADRIAN_MEMORY_SYNC_RU.md'
        blocks=re.findall(r'```python\n(.*?)```',guide.read_text(),flags=re.S)
        self.assertEqual(len(blocks),2)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'local';(root/'context').mkdir(parents=True)
            for filename in FILES:(root/filename).write_text('Synthetic '+filename,encoding='utf-8')
            (root/'AGENTS.md').write_text('Keep bootstrap unchanged',encoding='utf-8')
            original={name:(root/name).read_bytes() for name in (*FILES,'AGENTS.md')}
            store=SQLiteStore(Path(tmp)/'cloud-test.sqlite3')
            project=store.save_project(11,{'name':'Synthetic sync project'})
            scope=Scope(11,'kadrian',project['id'],project['id'])
            memory=Memory(Repository(store,initialize=True),Principal(11,'owner:11','owner'),scope)
            memory.record('fact','Cloud observation',source='synthetic',key='fact')
            downloaded=Path(tmp)/'downloaded.agent-memory.json'
            downloaded.write_text(json.dumps(memory.export()),encoding='utf-8')
            ns={'KADRIAN_ROOT':str(root),'OWNER_ID':11,'PROJECT_ID':project['id'],
                'DOWNLOADED_CLOUD_FILE':str(downloaded)}
            exec(compile(blocks[0],str(guide)+':local-package','exec'),ns)
            package=json.loads(ns['out'].read_text(encoding='utf-8'))
            self.assertEqual(package['format'],'agency-local/1')
            self.assertEqual(package['base_revision'],1)
            self.assertEqual(set(package['files']),set(FILES))
            self.assertEqual(ns['out'].stat().st_mode & 0o777,0o600)
            import_local(memory,package)
            self.assertEqual(memory.read()['revision'],2)
            import_local(memory,package)
            self.assertEqual(memory.read()['revision'],2)
            # Manual step in the guide: download a fresh cloud export after import.
            ns['cloud']=memory.export()
            exec(compile(blocks[1],str(guide)+':local-projection','exec'),ns)
            projection=(root/'context/SYNCED_MEMORY.md').read_text(encoding='utf-8')
            self.assertIn('Версия облака: 2',projection)
            self.assertIn('Cloud observation',projection)
            self.assertEqual({name:(root/name).read_bytes() for name in original},original)
            # Projection is not re-exported, avoiding recursive history growth.
            self.assertEqual(set(local_bundle(root,scope,2)['files']),set(FILES))
            # The documentation's packages are scope-bound, not trusted owner identities.
            ns['OWNER_ID']=22
            with self.assertRaises(ValueError):
                exec(compile(blocks[0],str(guide)+':wrong-owner','exec'),ns)
