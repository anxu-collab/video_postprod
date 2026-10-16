import datetime,json,time,urllib.request,urllib.error,uuid
from scheduler.contracts import canonical_hash
from scheduler.security import worker_token
from workers.common import ROOT,atomic_json,input_path,digest
def request(url,payload=None):
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,headers={'Authorization':'Bearer '+worker_token(),'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
def operation(store,kind,op,refs,params,versions=None,publication_guard=None,lease_token=1):
    adapter='music_adapter.py' if op=='music.select' else ('components.py' if kind=='mpt' else 'analysis.py')
    key=canonical_hash({'op':op,'refs':[r['sha256'] for r in refs],'params':{k:v for k,v in params.items() if k!='provider'},'provider':{k:v for k,v in params.get('provider',{}).items() if k!='operation_token'},'adapter_hash':digest(ROOT/'workers'/kind/adapter),'versions':versions or json.loads((ROOT/'config/versions.json').read_text(encoding='utf-8'))})
    result=store.cache_get(key)
    if result:return result,True
    record=ROOT/'state/dispatch'/kind/(key+'.json');base='http://127.0.0.1:'+str({'mpt':8761,'narrato':8762}[kind])
    if record.exists():
        saved=json.loads(record.read_text(encoding='utf-8'));operation_id=saved['operation_id']
        try:result=request(base+'/v1/operations/'+operation_id)
        except urllib.error.HTTPError as e:
            if e.code!=404:raise
            # Authenticated worker journal proves this operation was not accepted.
            # Any previously accepted paid subcall is still fenced by the Gateway.
            envelope=saved.get('envelope') or {'protocol':'postprod-worker/v1','operation_id':operation_id,'node_key':key,'attempt_id':uuid.uuid4().hex,'lease_token':1,'deadline_utc':(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=20)).isoformat(),'op':op,'input_refs':refs,'params':params,'provider_route_id':'configured','paid_intent_id':None}
            atomic_json(record,dict(saved,envelope=envelope,reconciliation='authenticated-worker-404'))
            result=request(base+'/v1/operations',envelope)
    else:
        operation_id=key[:40]+'-'+uuid.uuid4().hex[:8]
        envelope={'protocol':'postprod-worker/v1','operation_id':operation_id,'node_key':key,'attempt_id':uuid.uuid4().hex,'lease_token':1,'deadline_utc':(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=20)).isoformat(),'op':op,'input_refs':refs,'params':params,'provider_route_id':'configured','paid_intent_id':None}
        # Journal BEFORE dispatch; restart queries this exact operation, never duplicates.
        atomic_json(record,{'operation_id':operation_id,'node_key':key,'envelope':envelope})
        result=request(base+'/v1/operations',envelope)
    started=time.monotonic()
    if result['state']=='FAILED' and any(e['code'] in ['WORKER_INTERRUPTED','OPERATION_DEADLINE_EXPIRED'] for e in result.get('errors',[])):
        saved=json.loads(record.read_text(encoding='utf-8'))
        retries=saved.get('recovery_attempts',0)
        if retries>=2:raise RuntimeError('WORKER_RECOVERY_EXHAUSTED')
        previous=operation_id;operation_id=key[:40]+'-'+uuid.uuid4().hex[:8]
        envelope={'protocol':'postprod-worker/v1','operation_id':operation_id,'node_key':key,'attempt_id':uuid.uuid4().hex,'lease_token':lease_token,'deadline_utc':(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=20)).isoformat(),'op':op,'input_refs':refs,'params':params,'provider_route_id':'configured','paid_intent_id':None}
        atomic_json(record,{'operation_id':operation_id,'node_key':key,'envelope':envelope,'previous_operation_id':previous,'recovery_attempts':retries+1})
        # A new worker attempt can only reuse Gateway's stable intents. An
        # accepted-unknown paid subcall still fails closed without a resend.
        result=request(base+'/v1/operations',envelope)
    while result['state'] in ['PENDING','RUNNING']:
        if time.monotonic()-started>1200:raise TimeoutError('WORKER_PENDING_RECONCILE:'+operation_id)
        time.sleep(1);result=request(base+'/v1/operations/'+operation_id)
    if result['state']!='SUCCEEDED':raise RuntimeError('WORKER_FAILED:'+json.dumps(result.get('errors',[]),ensure_ascii=False))
    for ref in result['output_refs']:input_path(ref)
    if publication_guard:publication_guard()
    store.cache_put(key,result);return result,False
