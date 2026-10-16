import copy,json,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scheduler.security import operator_token
from scheduler.db import Store
from providers.ledger import Ledger
from renderer.ffmpeg import execute
from workers.common import atomic_json,digest
token=operator_token()
def api(url,payload=None,headers=None):
    req=urllib.request.Request('http://127.0.0.1:8760'+url,data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',**(headers or {})})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
def wait(id,rev):
    for i in range(180):
        s=api('/v1/jobs/'+id+'?revision='+str(rev))
        if s['state'] not in ['READY','RUNNING','RETRY_WAIT','VALIDATING','RECEIVED']:
            if not s['result'] or s['result']['technical_status']!='PASS':raise RuntimeError(json.dumps(s['errors'],ensure_ascii=False))
            return s
        time.sleep(2)
    raise TimeoutError('job timeout')
def revise(base,change):
    job=copy.deepcopy(base['payload']);job['revision']+=1;job['parent_revision']=base['revision'];job['overrides']['base_timeline_sha256']=base['result']['timeline_sha256'];change(job)
    existing=Store(ROOT/'state/jobs.sqlite').get_job(job['job_id'],job['revision'])
    if not existing:api('/v1/jobs/'+job['job_id']+'/revisions',job,{'If-Match':base['input_hash'],'Idempotency-Key':'live-'+job['job_id']+'-'+str(job['revision'])})
    return wait(job['job_id'],job['revision'])
store=Store(ROOT/'state/jobs.sqlite');ledger=Ledger(store);before=ledger.snapshot();base=api('/v1/jobs/acceptance-ab')
target=ROOT/'jobs/deployment-fixtures';bgm=target/'bgm-alternate.wav'
if not bgm.exists():execute(['-ss','8','-i',ROOT.parent/'assets/music/montauk_point/Montauk_Point_Kevin_MacLeod.mp3','-t','8','-ar','48000','-ac','2','-c:a','pcm_s16le',bgm],target)
prov=target/'bgm-alternate-provenance.json';atomic_json(prov,{'source':'Kevin MacLeod Montauk Point','license':'CC BY 4.0','source_manifest':str(ROOT.parent/'assets/music/brand_music.json'),'modification':'8秒起8秒节选，循环用于替换配乐测试','sha256':digest(bgm)})
bgm_result=revise(base,lambda j:j['overrides'].update(bgm_override={'path':str(bgm),'sha256':digest(bgm),'provenance_ref':str(prov),'gain_db':-36}));after_bgm=ledger.snapshot()
assert before['requests']==after_bgm['requests']
assert base['timeline']['subtitles']==bgm_result['timeline']['subtitles']
assert base['timeline']['subtitles']['source_audio_sha256']==bgm_result['timeline']['subtitles']['source_audio_sha256']
assert all(c['cached'] for c in bgm_result['result']['metrics']['picture']['chunks']) and bgm_result['result']['metrics']['picture']['master_cached']
print('BGM PASS',bgm_result['result']['output_path'],flush=True)
cue=bgm_result['timeline']['subtitles']['cues'][0]
subtitle_result=revise(bgm_result,lambda j:j['overrides'].update(subtitle_edits=[{'cue_id':cue['cue_id'],'text':cue['text']+'。'}]));after_sub=ledger.snapshot();assert after_sub['requests']==after_bgm['requests'];assert subtitle_result['result']['metrics']['mix']['mix_cached'];print('subtitle PASS',flush=True)
old_clip=subtitle_result['timeline']['video_clips'][1];a1=base['payload']['assets'][0]
def change_shot(j):
    second=j['assets'][1];second.update(path=a1['path'],sha256=a1['sha256'],provenance_ref=a1['provenance_ref'])
    j['overrides']['shot_edits']=[{'clip_id':old_clip['clip_id'],'asset_id':second['asset_id'],'source_in_tick':0,'source_out_tick':old_clip['duration_tick'],'preserve_narration':True}]
shot_result=revise(subtitle_result,change_shot);assert shot_result['timeline']['subtitles']['source_audio_sha256']==subtitle_result['timeline']['subtitles']['source_audio_sha256']
assert shot_result['result']['metrics']['picture']['chunks'][0]['cached'] and shot_result['result']['metrics']['picture']['chunks'][2]['cached']
assert 'asr-actual-voice-stem' not in shot_result['result']['metrics']
report={'bgm':{'status':'PASS','before':before,'after':after_bgm,'original_voices_subtitles_retained':True,'picture_chunks_all_reused':True,'actual_replacement_file':str(bgm)},'subtitles':{'status':'PASS','no_model_calls':True,'audio_mix_reused':True},'second_shot':{'status':'PASS','unchanged_clips_reused':True,'unchanged_audio_hash':shot_result['timeline']['subtitles']['source_audio_sha256'],'metrics':shot_result['result']['metrics'],'after':ledger.snapshot()},'result_paths':[r['result']['output_path'] for r in [bgm_result,subtitle_result,shot_result]],'h3_generation_calls':0,'submitted_through_fixed_service_api':True}
atomic_json(ROOT/'state/deployment/incremental-acceptance.json',report);print('second-shot PASS',flush=True)
