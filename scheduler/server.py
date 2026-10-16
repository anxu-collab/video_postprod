import sys,hmac,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from fastapi import FastAPI,Header,HTTPException
from scheduler.db import Store
from scheduler.security import worker_token,operator_token
from providers.gateway import Gateway
from scheduler.service import create_app
store=Store(ROOT/'state/jobs.sqlite');gateway=Gateway(store);app=create_app(store,gateway,operator_token())
def auth(value):
    if not hmac.compare_digest(value or '', 'Bearer '+worker_token()):raise HTTPException(401,'WORKER_TOKEN_REQUIRED')
@app.post('/internal/provider-token')
def issue(p:dict,authorization:str|None=Header(default=None)):
    auth(authorization)
    if not store.get_job(p['job_id'],p.get('revision',1)) and p['job_id']!='deployment-qualification':raise HTTPException(404,'JOB_NOT_FOUND')
    try:model=gateway.route_model(p['job_id'],p['node_key'],p.get('revision',1))
    except ValueError as e:raise HTTPException(409,str(e))
    return {'operation_token':gateway.issue(p['job_id'],p['node_key'],p.get('revision',1),model=model),'base_url':'http://127.0.0.1:8760/provider/v1','model':model}
@app.post('/provider/v1/chat/completions')
def chat(p:dict,authorization:str|None=Header(default=None)):
    try:return gateway.request((authorization or '').removeprefix('Bearer '),p)
    except Exception as e:raise HTTPException(409,{'code':type(e).__name__,'message':str(e).split(':')[0][:80]})
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8760,log_level='warning')
