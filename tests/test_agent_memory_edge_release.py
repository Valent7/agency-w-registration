"""Release supply boundaries; temporary synthetic git repository only."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.memory_edge_release import FILES, build, verify


class EdgeReleaseTests(unittest.TestCase):
    def test_exact_sha_allowlist_tamper_extra_files_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'source';root.mkdir();out=Path(tmp)/'release'
            def git(*args):return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
            git('init','-q');git('config','user.name','Synthetic');git('config','user.email','synthetic@example.invalid')
            for name in FILES:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('Synthetic source: '+name)
            (root/'neona-worker.py').write_text('Never in release')
            git('add','.');git('commit','-qm','Synthetic release fixture');sha=git('rev-parse','HEAD')
            manifest=build(root,out,sha)
            self.assertEqual(set(manifest['files']),set(FILES.values()))
            self.assertFalse(manifest['database_migrations']);self.assertFalse(manifest['render_services'])
            self.assertFalse((out/'neona-worker.py').exists());self.assertEqual(verify(out)['source_sha'],sha)
            with self.assertRaises(ValueError):build(root,out,sha)
            victim=out/'mac/agent_memory_edge.py';victim.write_text('Tampered')
            with self.assertRaises(ValueError):verify(out)

    def test_release_has_no_customer_data_keys_or_production_entrypoints(self):
        self.assertEqual(len(FILES),12)
        for forbidden in ('Agent.md','AGENTS.md','context/PROJECT.md','render.yaml','content_factory_oauth.py','content_factory_worker.py','neola-knowledge-search'):
            self.assertFalse(any(forbidden in name for name in FILES))
        root=Path(__file__).resolve().parents[1]
        workflow=(root/'.github/workflows/memory-edge.yml').read_text()
        self.assertNotIn('secrets.',workflow);self.assertNotIn('supabase functions deploy',workflow);self.assertNotIn('render deploy',workflow)
        self.assertIn('codex/memory-sync-integration',workflow)

    def test_launch_assets_validate_on_macos_without_loading_agent(self):
        import sys
        if sys.platform!='darwin':return
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'synthetic.plist';p.write_bytes(plistlib.dumps({'Label':'com.agency-w.synthetic-never-loaded','RunAtLoad':True,'StartInterval':60,'ProgramArguments':['/usr/bin/true']}))
            r=subprocess.run(['/usr/bin/plutil','-lint',str(p)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
