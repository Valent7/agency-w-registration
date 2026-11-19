"""Independent memory-only ASGI process. No agency runtime/lifespan/provider import."""
import json
import os
import requests
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool
from agent_memory import Scope, MemoryError
from agent_memory_device_api import device_routes
from agent_memory_store import Repository
from content_factory_store import SupabaseStore, StoreError


def configured_scope(env):
    owners=env.get('AGENCY_MEMORY_V1_OWNERS','').split(',')
    if len(owners)!=1 or not owners[0].strip().isdigit():raise MemoryError('One owner required')
    project=env.get('AGENCY_MEMORY_V1_PROJECT_ID','')
    scope=Scope(int(owners[0]),env.get('AGENCY_MEMORY_SYNC_AGENT_ID','kadrian'),project,project)
    if scope.agent_id=='*':raise MemoryError('Project agent required')
    registry=json.loads(env.get('AGENCY_MEMORY_SYNC_DEVICES','{}'))
    if not isinstance(registry,dict) or not registry:raise MemoryError('Device registry required')
    for device in registry.values():
        if Scope(**device['scope'])!=scope:raise MemoryError('Device outside service scope')
    return scope


class MemoryOnlySupabaseStore(SupabaseStore):
    """Application capability boundary; DB service credential still remains privileged.

    Reuses PR85 tables/RPC and validates every outbound path/scope. No job, media,
    OAuth, project mutation or parent-rule mutation can pass this adapter.
    """
    def __init__(self,url,key,scope,session=None):
        session=session or requests.Session();session.trust_env=False
        super().__init__(url,key,session=session);self.scope=scope

    def get_project(self,owner_id,project_id):
        if (owner_id,project_id)!=(self.scope.owner_id,self.scope.project_id):raise StoreError('Project outside scope')
        rows=self._rows('content_factory_projects',{'owner_id':'eq.'+str(owner_id),
            'id':'eq.'+project_id,'select':'id,owner_id','limit':1})
        return rows[0] if rows else None

    def _request(self,method,path,**kwargs):
        scope=self.scope;params=kwargs.get('params',{})
        if method=='GET':
            if params.get('owner_id')!='eq.'+str(scope.owner_id):raise StoreError('Owner outside scope')
            if path=='/rest/v1/content_factory_projects':
                if params.get('id')!='eq.'+scope.project_id or params.get('select')!='id,owner_id' or set(params)-{'owner_id','id','select','limit'}:
                    raise StoreError('Project fields unavailable')
            elif path in {'/rest/v1/agent_memory_workspaces','/rest/v1/agent_memory_revisions'}:
                agent=params.get('agent_id');workspace=params.get('workspace_id')
                if not ((agent=='eq.'+scope.agent_id and workspace=='eq.'+scope.workspace_id) or
                        (agent in {'eq.*','eq.'+scope.agent_id} and workspace=='eq.owner')):
                    raise StoreError('Memory outside scope')
                if set(params)-{'owner_id','agent_id','workspace_id','order','limit','offset','revision','idempotency_key','select'}:
                    raise StoreError('Filter unavailable')
            else:raise StoreError('Read path unavailable')
        elif method=='POST' and path=='/rest/v1/rpc/agent_memory_commit':
            data=kwargs.get('json',{})
            if (data.get('p_owner'),data.get('p_agent'),data.get('p_workspace'),data.get('p_project'))!=(scope.owner_id,scope.agent_id,scope.workspace_id,scope.project_id):
                raise StoreError('Write outside project memory')
        else:raise StoreError('Operation unavailable')
        kwargs['allow_redirects']=False
        return super()._request(method,path,**kwargs)


def create_app(store=None,env=None):
    env=dict(os.environ if env is None else env)
    enabled=env.get('AGENCY_MEMORY_V1')=='1' and env.get('AGENCY_MEMORY_DEVICE_SYNC')=='1'
    scope=configured_scope(env) if enabled else None
    if enabled and store is None:raise MemoryError('Memory store required')
    def alive(_):return JSONResponse({'service':'kadrian-memory-sync','status':'alive'},headers={'Cache-Control':'no-store'})
    async def ready(_):
        if not enabled:return JSONResponse({'ready':False},503)
        try:await run_in_threadpool(Repository(store,initialize=False).load,scope)
        except Exception:return JSONResponse({'ready':False},503)
        return JSONResponse({'ready':True},headers={'Cache-Control':'no-store'})
    allowed=lambda owner,project:enabled and (owner,project)==(scope.owner_id,scope.project_id)
    app=Starlette(routes=[Route('/health',alive),Route('/ready',ready),*device_routes(store,env,allowed=allowed)])
    return app


def runtime_app():
    """Explicit uvicorn --factory target. Importing this module performs NO I/O."""
    env=dict(os.environ)
    if env.get('AGENCY_MEMORY_V1')!='1' or env.get('AGENCY_MEMORY_DEVICE_SYNC')!='1':return create_app(env=env)
    scope=configured_scope(env)
    store=MemoryOnlySupabaseStore(env.get('SUPABASE_URL',''),env.get('SUPABASE_SECRET_KEY',''),scope)
    return create_app(store,env)
