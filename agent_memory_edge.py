"""PR90 transport adapted to one isolated Edge Function; no server keys on Mac.

Encrypted base snapshot is a disposable transport checkpoint, not another memory
repository. Old history stays immutable in the existing PR85 Supabase tables.
"""
import base64
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit
from uuid import uuid4
from agent_memory import canonical, digest, MemoryError, Conflict
from agent_memory_daemon import Sync, HTTPS, exclusive, atomic


class EdgeHTTPS(HTTPS):
    paths={'/functions/v1/kadrian-memory-sync'}

    def __init__(self,endpoint):
        super().__init__(endpoint)
        if not re.fullmatch(r'[a-z0-9]+\.supabase\.co',urlsplit(endpoint).netloc):
            raise MemoryError('Fixed Supabase Edge host required')


class EdgeSync(Sync):
    def __init__(self,*args,budget=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.budget=budget or {'requests':100_000,'response_bytes':1_000_000_000}
        if set(self.budget)!={'requests','response_bytes'} or any(type(v)is not int or v<1 for v in self.budget.values()):
            raise MemoryError('Positive local budget required')

    def _usage(self,state):
        month=datetime.fromtimestamp(self.clock(),timezone.utc).strftime('%Y-%m')
        usage=state.get('edge_usage',{})
        if usage.get('month')!=month:
            usage={'month':month,'requests':0,'response_bytes':0}
            state['edge_usage']=usage
        return usage

    def _warnings(self,state):
        usage=self._usage(state)
        ratio=max(usage[k]/self.budget[k] for k in self.budget)
        warnings=[]
        if ratio>=.8:warnings.append('Локальный лимит обмена памятью: '+str(round(ratio*100))+'%. Это оценка этого Mac, не общий расход Supabase.')
        quota=state.get('edge_quota',{})
        if quota.get('warning'):warnings.append(quota['warning'])
        return warnings

    def _exchange(self,state,event):
        usage=self._usage(state)
        # Reserve persistently BEFORE sending; a lost response still consumes a call.
        if usage['requests']>=self.budget['requests'] or usage['response_bytes']+3_000_000>self.budget['response_bytes']:
            raise MemoryError('Local sync budget reached; queue retained')
        usage['requests']+=1
        usage['response_bytes']+=3_000_000  # conservative reservation survives a crash
        self.save(state)
        request={'format':'agency-edge-sync/1','device_id':self.device_id,'scope':self.scope.as_dict(),
            'operation':'sync','payload':{'known':state.get('edge_known'),'event':event,
                'base_document':canonical(state['edge_snapshot']['snapshot']['document']) if event and state.get('edge_snapshot') else None},
            'issued_at':int(self.clock()),'request_id':uuid4().hex}
        response=self.transport({'request':request,'signature':base64.b64encode(self.key.sign(canonical(request).encode())).decode()})
        size=len(canonical(response).encode())+1024  # includes conservative framing allowance
        if size>3_000_000:raise MemoryError('Edge response too large')
        usage['response_bytes']-=3_000_000-size
        if response.get('format')!='agency-edge-sync/1' or response.get('scope')!=self.scope.as_dict():raise MemoryError('Edge scope mismatch')
        known=response.get('known')
        if not isinstance(known,dict) or set(known)!={'project','global','agent'}:raise MemoryError('Version metadata missing')
        for item in known.values():
            if set(item)!={'revision','hash'} or type(item['revision'])is not int or item['revision']<0 or not re.fullmatch('[a-f0-9]{64}',item['hash']):raise MemoryError('Invalid metadata')
        snapshot=response.get('snapshot')
        if snapshot is not None:
            self.apply_snapshot(state,snapshot)
            if known['project']!={'revision':state['cloud_revision'],'hash':state['cloud_hash']}:raise Conflict('Snapshot metadata mismatch')
            parents={p['scope']['agent_id']:p for p in snapshot['parents']}
            if set(parents)!={'*',self.scope.agent_id}:raise MemoryError('Parent rules missing')
            for name,agent in [('global','*'),('agent',self.scope.agent_id)]:
                if known[name]['revision']!=parents[agent]['revision']:raise Conflict('Parent metadata mismatch')
            state['edge_snapshot']=snapshot
        elif known!=state.get('edge_known'):
            raise Conflict('Changed version without content')
        state['edge_known']=known;state['edge_quota']=response.get('quota',{})
        ack=response.get('ack')
        if ack is not None:
            if not event or ack.get('event_id')!=event['event_id'] or type(ack.get('revision'))is not int or ack['revision']>state['cloud_revision'] or type(ack.get('conflict'))is not bool:
                raise MemoryError('Ack mismatch')
            if ack['conflict']:
                atomic(self.directory/('conflict-'+event['event_id']+'.enc'),self.cipher.encrypt(canonical({'event':event,'ack':ack}).encode()))
            state['pending'].pop(0)
        self.save(state)
        return response

    def cycle(self,max_events=20):
        with exclusive(self.directory):
            state=self.load();self.capture(state);usage=self._usage(state)
            now=self.clock()
            try:
                if now<state.get('edge_retry_at',0):
                    state['status']='offline_or_blocked'
                elif usage['requests']>=self.budget['requests'] or usage['response_bytes']+3_000_000>self.budget['response_bytes']:
                    state['status']='budget_paused'
                elif state.get('synced_at') is not None and 0<=now-state['synced_at']<60 and (not state['pending'] or not max_events):
                    state['status']='pending' if state['pending'] else 'current'
                else:
                    # One event per signed request bounds CPU and CAS. Normal idle =
                    # one metadata-only request/minute. Burst capped to three events.
                    for _ in range(min(max_events,3)+2):
                        event=state['pending'][0] if state['pending'] and max_events else None
                        response=self._exchange(state,event)
                        if response.get('need_base'):
                            if not response.get('snapshot'):raise MemoryError('Missing base')
                            continue
                        if not event or not state['pending']:break
                        max_events-=1
                        if max_events<=0:break
                    state.update(synced_at=now,status='pending' if state['pending'] else 'current',edge_failures=0,edge_retry_at=0)
            except Exception:
                failures=min(state.get('edge_failures',0)+1,7)
                state.update(status='offline_or_blocked',edge_failures=failures,edge_retry_at=now+min(3600,60*2**(failures-1)))
            state['warnings']=self._warnings(state)
            self.save(state)
            status={k:state.get(k) for k in ('status','cloud_revision','synced_at','edge_retry_at','warnings')}
            status.update(pending=len(state['pending']),checked_at=now,usage=dict(usage),usage_source='this_mac_estimate')
            atomic(self.directory/'status.json',canonical(status).encode())
            return status
