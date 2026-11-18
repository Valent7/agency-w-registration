"""Memory persistence adapters. Uses existing SQLite/Supabase connections.

SQLite schema initialization is opt-in. Supabase schema is NEVER installed by
application startup; operator applies reviewed SQL only after deployment approval.
"""
from copy import deepcopy
from hashlib import sha256
import json

from agent_memory import AccessDenied, Conflict, MemoryError, canonical, digest, empty, now
from content_factory_store import SQLiteStore, SupabaseStore, StoreError


class Repository:
    def __init__(self, store, *, initialize=False):
        self.store = store
        if not isinstance(store, (SQLiteStore, SupabaseStore)):
            raise MemoryError('Unsupported memory store')
        if initialize:
            if not isinstance(store, SQLiteStore):
                raise MemoryError('Cloud memory schema must be installed separately')
            with store._db() as db:
                db.executescript('''
                CREATE TABLE IF NOT EXISTS agent_memory_workspaces(
                    owner_id INTEGER NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    project_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id,agent_id,workspace_id),
                    FOREIGN KEY(project_id,owner_id) REFERENCES content_factory_projects(id,owner_id));
                CREATE TABLE IF NOT EXISTS agent_memory_revisions(
                    owner_id INTEGER NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, document TEXT NOT NULL, event TEXT NOT NULL,
                    hash TEXT NOT NULL, idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id,agent_id,workspace_id,revision),
                    UNIQUE(owner_id,agent_id,workspace_id,idempotency_key),
                    FOREIGN KEY(owner_id,agent_id,workspace_id)
                    REFERENCES agent_memory_workspaces(owner_id,agent_id,workspace_id));
                CREATE TRIGGER IF NOT EXISTS agent_memory_no_update BEFORE UPDATE ON agent_memory_revisions
                    BEGIN SELECT RAISE(ABORT,'Immutable memory history'); END;
                CREATE TRIGGER IF NOT EXISTS agent_memory_no_delete BEFORE DELETE ON agent_memory_revisions
                    BEGIN SELECT RAISE(ABORT,'Immutable memory history'); END;
                ''')

    def _project(self, scope):
        if scope.project_id and not self.store.get_project(scope.owner_id, scope.project_id):
            raise AccessDenied('Project is not accessible to this owner')

    def _params(self, scope):
        return {'owner_id': 'eq.' + str(scope.owner_id), 'agent_id': 'eq.' + scope.agent_id,
                'workspace_id': 'eq.' + scope.workspace_id}

    def load(self, scope):
        self._project(scope)
        if isinstance(self.store, SQLiteStore):
            with self.store._db() as db:
                binding = db.execute('SELECT project_id FROM agent_memory_workspaces WHERE owner_id=? AND agent_id=? AND workspace_id=?',
                                     (scope.owner_id, scope.agent_id, scope.workspace_id)).fetchone()
                if binding and binding['project_id'] != scope.project_id:
                    raise AccessDenied('Workspace project mismatch')
                row = db.execute('SELECT * FROM agent_memory_revisions WHERE owner_id=? AND agent_id=? AND workspace_id=? ORDER BY revision DESC LIMIT 1',
                                 (scope.owner_id, scope.agent_id, scope.workspace_id)).fetchone()
                return self._unpack(dict(row)) if row else None
        binding = self.store._rows('agent_memory_workspaces', {**self._params(scope), 'limit': 1})
        if binding and binding[0].get('project_id') != scope.project_id:
            raise AccessDenied('Workspace project mismatch')
        rows = self.store._rows('agent_memory_revisions', {**self._params(scope), 'order': 'revision.desc', 'limit': 1})
        return self._unpack(rows[0]) if rows else None

    @staticmethod
    def _unpack(row):
        result = {k: row[k] for k in ('revision', 'document', 'event', 'hash', 'idempotency_key', 'created_at')}
        for k in ('document', 'event'):
            if isinstance(result[k], str):
                result[k] = json.loads(result[k])
        if digest(result['document']) != result['hash']:
            raise MemoryError('Memory revision checksum mismatch')
        return deepcopy(result)

    def commit(self, scope, expected, document, event, key):
        self._project(scope)
        if type(expected) is not int or expected < 0 or not isinstance(key, str) or not 1 <= len(key) <= 250:
            raise MemoryError('Invalid memory revision or idempotency key')
        if isinstance(self.store, SQLiteStore):
            with self.store._db() as db:
                db.execute('BEGIN IMMEDIATE')
                args = (scope.owner_id, scope.agent_id, scope.workspace_id)
                db.execute('INSERT OR IGNORE INTO agent_memory_workspaces(owner_id,agent_id,workspace_id,project_id) VALUES(?,?,?,?)', (*args, scope.project_id))
                workspace = db.execute('SELECT * FROM agent_memory_workspaces WHERE owner_id=? AND agent_id=? AND workspace_id=?', args).fetchone()
                if workspace['project_id'] != scope.project_id:
                    raise AccessDenied('Workspace project mismatch')
                prior = db.execute('SELECT * FROM agent_memory_revisions WHERE owner_id=? AND agent_id=? AND workspace_id=? AND idempotency_key=?', (*args, key)).fetchone()
                if prior:
                    # A lost network response may be retried, but never with new content.
                    if prior['hash'] != digest(document):
                        raise Conflict('Idempotency key reused with different document')
                    return self._unpack(dict(prior))
                if workspace['revision'] != expected:
                    raise Conflict('Memory changed; reload before writing')
                row = {'revision': expected + 1, 'document': document, 'event': event,
                       'hash': digest(document), 'idempotency_key': key, 'created_at': now()}
                db.execute('INSERT INTO agent_memory_revisions VALUES(?,?,?,?,?,?,?,?,?)',
                           (*args, row['revision'], canonical(document), canonical(event), row['hash'], key, row['created_at']))
                db.execute('UPDATE agent_memory_workspaces SET revision=? WHERE owner_id=? AND agent_id=? AND workspace_id=?', (row['revision'], *args))
                return deepcopy(row)
        try:
            rows = self.store._request('POST', '/rest/v1/rpc/agent_memory_commit', json={
                'p_owner': scope.owner_id, 'p_agent': scope.agent_id, 'p_workspace': scope.workspace_id,
                'p_project': scope.project_id, 'p_expected': expected, 'p_document': document,
                'p_event': event, 'p_key': key, 'p_hash': digest(document)})
        except StoreError:
            # Never blindly repeat a write. Read-after-error reconciles only the same operation.
            prior = self.store._rows('agent_memory_revisions', {**self._params(scope), 'idempotency_key': 'eq.' + key, 'limit': 1})
            if prior and prior[0]['hash'] == digest(document):
                return self._unpack(prior[0])
            raise Conflict('Memory write not confirmed; reload before retry') from None
        if not rows:
            raise Conflict('Memory revision was not committed')
        return self._unpack(rows[0])

    def history(self, scope):
        self.load(scope)  # verifies project binding as well as ownership
        if isinstance(self.store, SQLiteStore):
            with self.store._db() as db:
                return [self._unpack(dict(row)) for row in db.execute('SELECT * FROM agent_memory_revisions WHERE owner_id=? AND agent_id=? AND workspace_id=? ORDER BY revision',
                                                                     (scope.owner_id, scope.agent_id, scope.workspace_id))]
        # PostgREST default row limit must not silently truncate a backup.
        rows = []; offset = 0
        while True:
            page = self.store._rows('agent_memory_revisions', {**self._params(scope), 'order': 'revision.asc', 'limit': 500, 'offset': offset})
            rows.extend(self._unpack(row) for row in page)
            if len(page) < 500:
                break
            offset += len(page)
        return rows
