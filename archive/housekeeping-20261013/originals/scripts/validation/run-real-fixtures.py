import copy,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from workers.common import atomic_json,digest
from scheduler.db import Store
from scheduler.pipeline import Pipeline
store=Store(ROOT/'state/jobs.sqlite');inventory=json.loads((ROOT/'jobs/deployment-fixtures/inventory.json').read_text(encoding='utf-8'))
auth={'decision':'allow_preview_bt709','source_hashes':[x['sha256'] for x in inventory.values()],'authorization':'用户允许技术预览、保留待审阅状态；不加制作说明角标','production_color_qualified':False}
atomic_json(ROOT/'state/deployment/color-preview-authorization.json',auth)
example=json.loads((ROOT/'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8'))
for kind,keys in [('AB',['a1','a2','b1']),('B',['b1']),('A',['a1','a2','a1'])]:
    job=copy.deepcopy(example);job['job_id']='acceptance-'+kind.lower();job['intent']['brief']='火龙果画面介绍';job['intent']['target_duration_ticks']=sum(((inventory[k]['probe']['duration_ticks']+1919)//1920 if k.startswith('b') else inventory[k]['probe']['duration_ticks']//1920)*1920 for k in keys)
    job['providers']={'text':'ark-approved','vision':'ark-approved','tts':'cosy-laox','asr':'mpt-cuda-small','lipsync':None};job['cost']={'paid_enabled':True,'currency':'CNY','job_limit_micros':2000000,'daily_limit_micros':2000000,'max_paid_requests':40};job['assets']=[]
    for i,k in enumerate(keys):
        x=inventory[k];id=k+'-'+str(i+1);prov=ROOT/'jobs/deployment-fixtures'/(k+'-provenance.json')
        atomic_json(prov,{'source_sha256':x['sha256'],'origin':'existing_h3_generated','identity':'laoX' if k=='b1' else 'no_host','product_identity_verified':False,'real_product_test':False,'note':'复用既有H3素材；未重新生成'})
        locked=None
        if k=='b1':
            evidence=ROOT/'jobs/deployment-fixtures'/(k+'-full-source-evidence.json');duration=((x['probe']['duration_ticks']+1919)//1920)*1920
            atomic_json(evidence,{'source_sha256':x['sha256'],'validator':'full-source/v1','validated':True,'in_tick':0,'out_tick':duration,'endpoint_policy':'entire source retained; terminal rounding <= one frame; no interior speech cut','dialogue_completeness_verified':False,'quality':'REVIEW_REQUIRED'})
            locked={'transcript':'买之前','audio_stream_index':x['probe']['stream_indices']['audio'],'evidence_ref':str(evidence)}
        job['assets'].append({'asset_id':id,'path':x['path'],'sha256':x['sha256'],'mode':'spoken_locked' if k=='b1' else 'broll','provenance_ref':str(prov),'locked':locked})
    atomic_json(ROOT/'jobs/deployment-fixtures'/('edit-job-'+kind.lower()+'.json'),job)
    if store.get_job(job['job_id']):
        old=ROOT/'jobs'/job['job_id']/'rev-0001/result.json'
        if old.exists():print(kind,'already rendered',json.loads(old.read_text(encoding='utf-8'))['output_path'],flush=True);continue
    else:store.submit_job('fixture-'+kind.lower()+'-v1',job)
    try:
        result=Pipeline(store,job).run();print(kind,result['technical_status'],result['output_path'],result['qa']['checks'],flush=True)
    except Exception as e:
        store.set_state(job['job_id'],1,'FAILED_FINAL',{'code':str(e).split(':')[0],'message':str(e)[:600]});raise
