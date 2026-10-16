import copy,json,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scheduler.security import operator_token
from workers.common import atomic_json
token=operator_token()
def api(url,p=None,headers=None):
    req=urllib.request.Request('http://127.0.0.1:8760'+url,data=json.dumps(p,ensure_ascii=False).encode() if p is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',**(headers or {})})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
base=api('/v1/jobs/acceptance-ab?revision=3');failed=api('/v1/jobs/acceptance-ab?revision=4');p=copy.deepcopy(failed['payload']);p.update(revision=5,parent_revision=4);p['overrides']['base_timeline_sha256']=base['result']['timeline_sha256']
try:api('/v1/jobs/acceptance-ab?revision=5')
except urllib.error.HTTPError as e:
    if e.code!=404:raise
    api('/v1/jobs/acceptance-ab/revisions',p,{'If-Match':failed['input_hash'],'Idempotency-Key':'live-shot-repair-v5'})
for i in range(180):
    s=api('/v1/jobs/acceptance-ab?revision=5')
    if s['state'] not in ['READY','RUNNING','RETRY_WAIT']:
        if not s['result']:raise RuntimeError(json.dumps(s['errors'],ensure_ascii=False))
        assert s['result']['technical_status']=='PASS'
        assert s['timeline']['subtitles']['source_audio_sha256']==base['timeline']['subtitles']['source_audio_sha256']
        assert s['result']['metrics']['picture']['chunks'][0]['cached'] and s['result']['metrics']['picture']['chunks'][2]['cached']
        assert not s['result']['metrics']['picture']['chunks'][1]['cached']
        assert 'asr-actual-voice-stem' not in s['result']['metrics']
        atomic_json(ROOT/'state/deployment/second-shot-acceptance.json',{'status':'PASS','source_video_changed':True,'unchanged_clips_reused':True,'tts_and_asr_calls':0,'voice_hash':s['timeline']['subtitles']['source_audio_sha256'],'metrics':s['result']['metrics'],'cost':s['cost'],'output_path':s['result']['output_path'],'rejected_revision':4,'quality_remains':'REVIEW_REQUIRED'});print('second-shot PASS',s['result']['output_path'],flush=True);break
    time.sleep(2)
else:raise TimeoutError('second-shot timeout')
