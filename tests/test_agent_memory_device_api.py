"""HTTP boundary checks against a synthetic ASGI app, no external network."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from starlette.applications import Starlette
from starlette.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from agent_memory import Scope, canonical, digest, MemoryError
from agent_memory_device_api import device_routes
from agent_memory_device import MAX_BYTES
from agent_memory_daemon import Sync, HTTPS
from content_factory_store import SQLiteStore


class DeviceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        self.store=SQLiteStore(self.base/'cloud.sqlite3');p=self.store.save_project(11,{'name':'Synthetic'})
        self.scope=Scope(11,'kadrian',p['id'],p['id']);key=Ed25519PrivateKey.generate()
        raw=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        pub=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        self.env={'AGENCY_MEMORY_V1':'1','AGENCY_MEMORY_V1_OWNERS':'11','AGENCY_MEMORY_V1_PROJECT_ID':p['id'],
                  'AGENCY_MEMORY_DEVICE_SYNC':'1','AGENCY_MEMORY_SYNC_DEVICES':json.dumps({'test':{'public_key':base64.b64encode(pub).decode(),'scope':self.scope.as_dict()}})}
        # Test-only schema. The route never initializes schema on production startup.
        from agent_memory_store import Repository
        Repository(self.store,initialize=True)
        self.client=TestClient(Starlette(routes=device_routes(self.store,self.env)));self.addCleanup(self.client.close)
        self.sync=Sync(self.base,self.base/'transport',self.scope,'test',raw,self.exchange)

    def exchange(self,envelope):
        with patch.dict(os.environ,self.env,clear=True):
            response=self.client.post('/memory-sync/v1/exchange',json=envelope)
        if response.status_code!=200:raise MemoryError('Blocked')
        self.last=response;return response.json()

    def test_authenticated_pull_no_cache(self):
        self.assertEqual(self.sync.request('pull',{})['snapshot']['revision'],0)
        self.assertEqual(self.last.headers['cache-control'],'no-store')

    def test_bad_signature_unknown_device_disabled_pilot(self):
        with self.assertRaises(MemoryError):self.sync.request('pull',{'bad':'payload'})
        self.env['AGENCY_MEMORY_V1_PROJECT_ID']='different-project'
        with self.assertRaises(MemoryError):self.sync.request('pull',{})
        self.env['AGENCY_MEMORY_DEVICE_SYNC']='0'
        self.assertEqual(self.client.post('/memory-sync/v1/exchange',json={}).status_code,404)

    def test_no_body_or_oversize_or_method_leak(self):
        response=self.client.post('/memory-sync/v1/exchange',content='not json')
        self.assertEqual(response.status_code,403);self.assertEqual(response.json(),{'error':'sync_unavailable'})
        self.assertEqual(self.client.post('/memory-sync/v1/exchange',content='x'*(MAX_BYTES+1)).status_code,413)
        self.assertEqual(self.client.get('/memory-sync/v1/exchange').status_code,405)

    def test_redirect_is_not_followed_by_Mac(self):
        transport=HTTPS('https://example.test/memory-sync/v1/exchange')
        response=Mock();response.status_code=302;response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        transport.session.post=Mock(return_value=response)
        with self.assertRaises(MemoryError):transport({'request':'synthetic'})
        self.assertFalse(transport.session.post.call_args.kwargs['allow_redirects'])
        self.assertFalse(transport.session.trust_env)
