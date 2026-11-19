"""Narrow signed device protocol. No database credentials or rule approval on Mac."""
import base64
import time
from copy import deepcopy
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from agent_memory import Scope, Principal, Memory, MemoryError, Conflict, canonical, digest, safe_content, validate_bundle

SYNC_FILES = ('context/PROJECT.md', 'context/DECISIONS.md', 'context/FEEDBACK.md')
MAX_BYTES = 512_000


def validate_snapshot(snapshot, scope):
    """Reuse v1 schema validation without pretending a latest snapshot is full history."""
    if snapshot.get('scope') != scope.as_dict():
        raise MemoryError('Snapshot scope mismatch')
    row = deepcopy(snapshot['snapshot'])
    if type(row.get('revision')) is not int or row['revision'] < 0:
        raise MemoryError('Invalid snapshot revision')
    if row['revision'] == 0 and (row['document']['entries'] or row['document']['tasks']):
        raise MemoryError('Invalid empty snapshot')
    # validate_bundle checks the whole schema and document hash. Only the sequence
    # number is normalized for validation of ONE latest snapshot, not a backup.
    row['revision'] = 1
    body = {'format':'agency-memory/1','scope':scope.as_dict(),'revisions':[row]}
    validate_bundle({**body,'checksum':digest(body)}, scope)
    parents=snapshot.get('parents',[])
    if not isinstance(parents,list) or len(parents)>2:raise MemoryError('Invalid parent rules')
    seen=set()
    for parent in parents:
        if set(parent)!={'scope','revision','rules','checksum'}:raise MemoryError('Invalid parent projection')
        ps=Scope(**parent['scope'])
        if ps.owner_id!=scope.owner_id or ps.project_id is not None or ps.workspace_id!='owner' or ps.agent_id not in {'*',scope.agent_id} or ps.agent_id in seen:
            raise MemoryError('Parent scope mismatch')
        seen.add(ps.agent_id)
        if type(parent['revision']) is not int or parent['revision']<0:raise MemoryError('Invalid parent revision')
        body={k:v for k,v in parent.items() if k!='checksum'}
        if digest(body)!=parent['checksum']:raise MemoryError('Parent checksum mismatch')
        safe_content(body,max_bytes=256_000)
        if not isinstance(parent['rules'],list):raise MemoryError('Invalid parent rules')
        for rule in parent['rules']:
            if set(rule)!={'id','kind','text','status','certainty','source'} or rule['kind']!='rule' or rule['status']!='approved' or not isinstance(rule['text'],str):
                raise MemoryError('Only approved parent rules allowed')
    return snapshot['snapshot']


class DeviceService:
    def __init__(self, repository, devices, allowed, clock=time.time):
        self.repository, self.devices, self.allowed, self.clock = repository, devices, allowed, clock

    def handle(self, envelope):
        if not isinstance(envelope, dict) or set(envelope) != {'request','signature'}:
            raise MemoryError('Invalid envelope')
        request = envelope['request']
        safe_content(request, max_bytes=MAX_BYTES)
        if set(request) != {'format','device_id','scope','operation','payload','issued_at','request_id'}:
            raise MemoryError('Invalid request')
        if request['format'] != 'agency-device-sync/1':
            raise MemoryError('Invalid format')
        device = self.devices.get(request['device_id'])
        if not device or device.get('revoked', False):
            raise MemoryError('Device unavailable')
        scope = Scope(**device['scope'])  # trusted registry, NEVER caller identity
        if request['scope'] != scope.as_dict() or scope.project_id is None or scope.agent_id == '*':
            raise MemoryError('Scope unavailable')
        if not self.allowed(scope.owner_id, scope.project_id):
            raise MemoryError('Pilot disabled')
        if type(request['issued_at']) is not int or abs(self.clock()-request['issued_at']) > 300:
            raise MemoryError('Expired request')
        Ed25519PublicKey.from_public_bytes(base64.b64decode(device['public_key'],validate=True)).verify(
            base64.b64decode(envelope['signature'],validate=True), canonical(request).encode())
        principal = Principal(scope.owner_id, 'device:'+request['device_id'], 'sync',
                              scope.agent_id, scope.workspace_id, frozenset({'read','append','sync'}))
        memory = Memory(self.repository,principal,scope)
        if request['operation'] == 'pull':
            if request['payload'] != {}: raise MemoryError('Unexpected pull payload')
            parents=[]
            for agent in ('*',scope.agent_id):
                ps=Scope(scope.owner_id,agent,'owner')
                reader=Principal(scope.owner_id,'approved-rule-reader','agent',agent,'owner',frozenset({'read'}))
                context=Memory(self.repository,reader,ps).context()
                # Explicit allowlist: no parent/client knowledge, task or evidence export.
                rules=[{k:e[k] for k in ('id','kind','text','status','certainty','source')} for e in context['rules']]
                body={'scope':ps.as_dict(),'revision':context['revision'],'rules':rules}
                parents.append({**body,'checksum':digest(body)})
            result = {'scope':scope.as_dict(),'snapshot':memory.read(),'parents':parents}
            validate_snapshot(result,scope)
            return result
        if request['operation'] != 'push':
            raise MemoryError('Operation unavailable')
        event = request['payload']
        if set(event) != {'sequence','base_revision','files','file_modes','event_id'}:
            raise MemoryError('Invalid event')
        if type(event['sequence']) is not int or event['sequence'] < 1 or type(event['base_revision']) is not int or event['base_revision'] < 0:
            raise MemoryError('Invalid sequence')
        if not isinstance(event['files'],dict) or not event['files'] or set(event['files'])-set(SYNC_FILES):
            raise MemoryError('Only project context files allowed')
        if not all(isinstance(v,str) for v in event['files'].values()):
            raise MemoryError('Invalid file text')
        if not isinstance(event['file_modes'],dict) or set(event['file_modes']) != set(event['files']):
            raise MemoryError('Invalid file modes')
        for name, info in event['file_modes'].items():
            if set(info) != {'mode','base_hash','result_hash'} or info['mode'] not in {'snapshot','append'}:
                raise MemoryError('Invalid file delta')
            if info['mode']=='snapshot' and digest(event['files'][name])!=info['result_hash']:
                raise MemoryError('File checksum mismatch')
            if info['mode']=='append' and (not isinstance(info['base_hash'],str) or len(info['base_hash'])!=64):
                raise MemoryError('Append requires base checkpoint')
        expected_id = digest({'device':request['device_id'], **{k:v for k,v in event.items() if k!='event_id'}})
        if event['event_id'] != expected_id: raise MemoryError('Event checksum mismatch')
        safe_content(event,max_bytes=MAX_BYTES)
        # Append against latest CAS snapshot. Concurrent updates are preserved as
        # separate immutable entries, not overwritten and not auto-approved.
        key = 'device-event:'+request['device_id']+':'+str(event['sequence'])
        entry_id = digest({'scope':scope.as_dict(),'key':key})[:32]
        for _ in range(4):
            snap = memory.read()
            prior = snap['document']['entries'].get(entry_id)
            if prior:
                if prior.get('evidence',{}).get('event_id') != expected_id:
                    raise Conflict('Sequence reused with different event')
                return {'event_id':expected_id,'revision':snap['revision'],
                        'conflict':prior['evidence']['conflict']}
            if event['base_revision'] > snap['revision']:
                raise Conflict('Client is ahead of cloud')
            conflict = event['base_revision'] != snap['revision']
            try:
                row = memory.record('legacy',canonical({'files':event['files'],'file_modes':event['file_modes']}),source='device-context-snapshot',
                    key=key, expected=snap['revision'], certainty='unknown', evidence={
                        'event_id':expected_id,'device_id':request['device_id'],
                        'base_revision':event['base_revision'],'cloud_revision':snap['revision'],
                        'conflict':conflict,'file_modes':event['file_modes']})
                return {'event_id':expected_id,'revision':row['revision'],'conflict':conflict}
            except Conflict:
                continue
        raise Conflict('Cloud busy; retain local event')
