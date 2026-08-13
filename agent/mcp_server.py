from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
import os
import json
import threading
import hmac
import hashlib
import requests
from typing import Optional
import sys

BASE_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, PARENT_DIR)

CONFIG_PATH = os.path.join(PARENT_DIR, 'config', 'api_keys.json')

def _load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

config = _load_config()
API_SECRET = os.environ.get('JEEVES_API_SECRET') or config.get('jeeves_api_secret')
CALLBACK_SECRET = os.environ.get('JEEVES_CALLBACK_SECRET') or config.get('jeeves_callback_secret')

from composio_agent import run_agentic_task
from agent.task_queue import get_queue

app = FastAPI(title='Jeeves MCP Server')


class InvokeRequest(BaseModel):
    prompt: str
    system_prompt: Optional[str] = None
    mode: str = 'sync'  # 'sync' or 'async'
    callback_url: Optional[str] = None


def _check_auth(x_secret: Optional[str], authorization: Optional[str]):
    if API_SECRET is None:
        return False
    if x_secret == API_SECRET:
        return True
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == 'bearer' and parts[1] == API_SECRET:
            return True
    return False


def _sign_payload(payload: bytes) -> str:
    if not CALLBACK_SECRET:
        return ''
    sig = hmac.new(CALLBACK_SECRET.encode('utf-8'), payload, hashlib.sha256).hexdigest()
    return sig


@app.post('/invoke')
def invoke(req: InvokeRequest, request: Request, x_jeeves_secret: Optional[str] = Header(None)):
    auth = request.headers.get('authorization')
    if not _check_auth(x_jeeves_secret, auth):
        raise HTTPException(status_code=401, detail='Unauthorized')

    if req.mode == 'sync':
        # Synchronous execution via existing agent runtime
        result = run_agentic_task(req.prompt, req.system_prompt)
        return {'result': result}

    # Async: enqueue and return task id; notify callback when done
    q = get_queue()

    callback = req.callback_url

    def _on_complete(task_id, result):
        if not callback:
            return
        payload = json.dumps({'task_id': task_id, 'result': result}).encode('utf-8')
        sig = _sign_payload(payload)
        headers = {'Content-Type': 'application/json'}
        if sig:
            headers['X-Jeeves-Signature'] = sig
        try:
            requests.post(callback, data=payload, headers=headers, timeout=10)
        except Exception:
            pass

    task_id = q.submit(goal=req.prompt, on_complete=_on_complete)
    return {'task_id': task_id}


@app.get('/task/{task_id}')
def task_status(task_id: str, request: Request, x_jeeves_secret: Optional[str] = Header(None)):
    auth = request.headers.get('authorization')
    if not _check_auth(x_jeeves_secret, auth):
        raise HTTPException(status_code=401, detail='Unauthorized')
    q = get_queue()
    status = q.get_status(task_id)
    if not status:
        raise HTTPException(status_code=404, detail='Task not found')
    return status


if __name__ == '__main__':
    import uvicorn
    port = int(config.get('jeeves_public_port', 8000))
    uvicorn.run('agent.mcp_server:app', host='0.0.0.0', port=port, reload=False)
