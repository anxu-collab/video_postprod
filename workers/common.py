"""Isolated component operations; never a second production scheduler."""
import concurrent.futures
import hashlib
import json
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException

ROOT=Path(__file__).resolve().parents[1]
OPS={'mpt':{'script.generate','speech.synthesize','speech.transcribe','material.inspect','music.select'},'narrato':{'video.analyze','scene.detect'}}

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()

def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.partial.json');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,path)

def input_path(ref):
    p=Path(ref['path']).resolve(strict=True)
    if not p.is_relative_to(ROOT) or str(p).startswith('\\\\'):raise ValueError('INPUT_PATH_OUTSIDE_PROJECT')
    if digest(p)!=ref['sha256']:raise ValueError('INPUT_HASH_CHANGED')
    return p

def validate_operation(req,kind):
    if req.get('protocol')!='postprod-worker/v1' or req.get('op') not in OPS[kind]:raise ValueError('OPERATION_NOT_ALLOWED')
    for name in ['operation_id','node_key','attempt_id','lease_token','deadline_utc','input_refs','params','provider_route_id','paid_intent_id']:
        if name not in req:raise ValueError('ENVELOPE_FIELD_MISSING:'+name)
    if not re.fullmatch('[A-Za-z0-9_.-]{1,128}',req['operation_id']):raise ValueError('OPERATION_ID_INVALID')
    if req['op']=='speech.synthesize' and req['params'].get('mode')!='broll':raise ValueError('LOCKED_TRANSCRIPT_CHANGE')

def output_ref(path,kind='manifest'):
    p=Path(path);return {'ref_id':p.stem,'path':str(p),'sha256':digest(p),'kind':kind}

def operation_app(kind,handler,capabilities):
    app=FastAPI(title='postprod '+kind+' components')
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
    lock=threading.Lock();journal=ROOT/'state/workers'/kind;journal.mkdir(parents=True,exist_ok=True)
    token=os.environ.get('POSTPROD_WORKER_TOKEN','')
    if not token:raise RuntimeError('POSTPROD_WORKER_TOKEN_REQUIRED')
    for p in journal.glob('*.json'):
        old=json.loads(p.read_text(encoding='utf-8'))
        if old.get('state')=='RUNNING':
            old.update(state='FAILED',errors=[{'code':'WORKER_INTERRUPTED','retryable':False,'message':'Reconcile Gateway intent before a new attempt'}]);atomic_json(p,old)
    def auth(value):
        import hmac
        if not hmac.compare_digest(value or '', 'Bearer '+token):raise HTTPException(401,'WORKER_TOKEN_REQUIRED')
    @app.get('/health/live')
    def live():return {'status':'alive','component':kind}
    @app.get('/v1/capabilities')
    def caps():return capabilities()
    @app.get('/health/ready')
    def ready():
        c=capabilities()
        if not c.get('ready'):raise HTTPException(503,c)
        return c
    def execute(req,file):
        state=json.loads(file.read_text(encoding='utf-8'));state['state']='RUNNING';atomic_json(file,state)
        work=ROOT/'jobs/worker-operations'/kind/req['operation_id'];work.mkdir(parents=True,exist_ok=True)
        try:
            from datetime import datetime,timezone
            if datetime.fromisoformat(req['deadline_utc'].replace('Z','+00:00'))<datetime.now(timezone.utc):raise ValueError('OPERATION_DEADLINE_EXPIRED')
            for ref in req['input_refs']:input_path(ref)
            result=handler(req,work)
            if not result.get('output_refs'):raise ValueError('EMPTY_OPERATION_OUTPUT')
            for ref in result['output_refs']:input_path(ref)
            state.update(result,state='SUCCEEDED',errors=[])
        except Exception as e:
            state.update(state='FAILED',output_refs=[],errors=[{'code':str(e).split(':')[0][:100],'retryable':False,'message':str(e)[:1000]}])
        atomic_json(file,state)
    @app.post('/v1/operations',status_code=202)
    def submit(req:dict,authorization:str|None=Header(default=None)):
        auth(authorization)
        try:validate_operation(req,kind)
        except ValueError as e:raise HTTPException(422,str(e))
        key=hashlib.sha256(json.dumps(req,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        file=journal/(req['operation_id']+'.json')
        with lock:
            if file.exists():
                old=json.loads(file.read_text(encoding='utf-8'))
                if old['request_hash']!=key:raise HTTPException(409,'OPERATION_ID_CONFLICT')
                return old
            state={'operation_id':req['operation_id'],'node_key':req['node_key'],'attempt_id':req['attempt_id'],'lease_token':req['lease_token'],'state':'PENDING','request_hash':key,'output_refs':[],'errors':[]}
            atomic_json(file,state);pool.submit(execute,req,file);return state
    @app.get('/v1/operations/{operation_id}')
    def get(operation_id:str,authorization:str|None=Header(default=None)):
        auth(authorization)
        if not re.fullmatch('[A-Za-z0-9_.-]{1,128}',operation_id):raise HTTPException(422,'BAD_ID')
        p=journal/(operation_id+'.json')
        if not p.exists():raise HTTPException(404,'NOT_FOUND')
        return json.loads(p.read_text(encoding='utf-8'))
    return app
