import copy,json
from pathlib import Path
import pytest,asyncio,types
ROOT=Path(__file__).resolve().parents[1]
class Client:
    def __init__(self,app):self.app=app
    def call(self,method,path,payload=None,headers=None):
        body=json.dumps(payload).encode() if payload is not None else b'';messages=[]
        async def run():
            sent=False
            async def receive():
                nonlocal sent
                if not sent:sent=True;return {'type':'http.request','body':body,'more_body':False}
                return {'type':'http.disconnect'}
            async def send(message):messages.append(message)
            h={'content-type':'application/json',**(headers or {})}
            await self.app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','scheme':'http','method':method,'path':path,'raw_path':path.encode(),'query_string':b'','root_path':'','headers':[(k.lower().encode(),v.encode()) for k,v in h.items()],'client':('127.0.0.1',1234),'server':('127.0.0.1',8760)},receive,send)
        asyncio.run(run());data=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body');status=next(m['status'] for m in messages if m['type']=='http.response.start')
        return types.SimpleNamespace(status_code=status,json=lambda:json.loads(data))
    def post(self,path,json=None,headers=None):return self.call('POST',path,json,headers)
    def get(self,path,headers=None):return self.call('GET',path,headers=headers)
def test_job_api_requires_operator_and_preserves_immutable_revisions(tmp_path):
    from scheduler.service import create_app
    from scheduler.db import Store
    from providers.gateway import Gateway
    store=Store(tmp_path/'db.sqlite');app=create_app(store,Gateway(store),'operator-test',run_queue=False)
    c=Client(app);job=json.loads((ROOT/'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8'));job['providers']={'text':'ark-approved','vision':'ark-approved','tts':'cosy-laox','asr':'mpt-cuda-small','lipsync':None}
    assert c.post('/v1/jobs',json=job,headers={'Idempotency-Key':'one'}).status_code==401
    h={'Authorization':'Bearer operator-test','Idempotency-Key':'one'}
    assert c.post('/v1/jobs',json=job,headers=h).status_code==202
    assert c.post('/v1/jobs',json=job,headers=h).status_code==202
    other=copy.deepcopy(job);other['intent']['brief']='different'
    assert c.post('/v1/jobs',json=other,headers=h).status_code==409
    other=copy.deepcopy(job);other.update(revision=2,parent_revision=1)
    assert c.post('/v1/jobs/demo-ab/revisions',json=other,headers={**h,'If-Match':'wrong','Idempotency-Key':'two'}).status_code==409
    result=c.get('/v1/jobs/demo-ab',headers=h).json()
    assert result['state']=='READY' and result['quality_accepted'] is False
def test_accepted_quality_cannot_come_from_input_json(tmp_path):
    from scheduler.service import accepted_quality
    assert accepted_quality([],artifact='a'*64,has_locked=True) is False
    records=[{'artifact_hash':'b'*64,'scope':s,'decision':'accept'} for s in ['visual','color','audio','subtitles','content','lipsync','boundaries']]
    assert accepted_quality(records,'a'*64,True) is False
