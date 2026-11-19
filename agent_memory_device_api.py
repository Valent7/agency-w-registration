"""Optional routes on existing ASGI service; disabled unless explicitly installed."""
import json
import os
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool
from agent_memory_device import DeviceService, MAX_BYTES
from agent_memory import Conflict
from agent_memory_runtime import enabled
from agent_memory_store import Repository


def device_routes(store, env=None, *, allowed=None):
    env = os.environ if env is None else env
    allowed = enabled if allowed is None else allowed
    async def exchange(request):
        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
        if env.get('AGENCY_MEMORY_DEVICE_SYNC') != '1':
            return JSONResponse({'error':'disabled'},404,headers=headers)
        raw=bytearray()
        try:
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>MAX_BYTES: return JSONResponse({'error':'too_large'},413,headers=headers)
            devices=json.loads(env.get('AGENCY_MEMORY_SYNC_DEVICES','{}'))
            service=DeviceService(Repository(store,initialize=False),devices,allowed)
            result=await run_in_threadpool(service.handle,json.loads(raw))
            return JSONResponse(result,headers=headers)
        except Conflict:
            return JSONResponse({'error':'conflict'},409,headers=headers)
        except Exception:
            # Never include request, key, file text or provider exception in logs.
            return JSONResponse({'error':'sync_unavailable'},403,headers=headers)
    return [Route('/memory-sync/v1/exchange',exchange,methods=['POST'])]
