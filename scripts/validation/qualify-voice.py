import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scheduler.db import Store
from scheduler.worker_client import request,operation
from workers.common import atomic_json
store=Store(ROOT/'state/jobs.sqlite')
for key in ['a1','a2']:
    vision=json.loads((ROOT/'jobs/deployment-fixtures'/(key+'-vision-operation.json')).read_text(encoding='utf-8'))
    evidence=json.loads(Path(vision['output_refs'][0]['path']).read_text(encoding='utf-8'))
    observations=[o['text'] for batch in evidence['coverage'] for o in batch.get('observations',[])]
    provider=request('http://127.0.0.1:8760/internal/provider-token',{'job_id':'deployment-qualification','node_key':'script-short-v1:'+evidence['asset_hash']})
    script,cached=operation(store,'mpt','script.generate',[],{'provider':provider,'brief':'火龙果画面介绍','claim_ids':[],'prompt':'根据以下画面观察，写一句不超过12个汉字的客观旁白，只说画面里可见的动作和颜色。不要给选购建议或口感功效。不添加身份开场。不扩充资料。观察：'+json.dumps(observations,ensure_ascii=False)})
    atomic_json(ROOT/'jobs/deployment-fixtures'/(key+'-script-operation.json'),dict(script,cached=cached))
    text=json.loads(Path(script['output_refs'][0]['path']).read_text(encoding='utf-8'))['text']
    if len(text)>18:raise RuntimeError('SCRIPT_LENGTH_REVIEW_REQUIRED')
    print(key,'script',text,flush=True)
    voice,cached=operation(store,'mpt','speech.synthesize',[],{'mode':'broll','preset':'laox_warming_compact','text':text})
    atomic_json(ROOT/'jobs/deployment-fixtures'/(key+'-voice-operation.json'),dict(voice,cached=cached));print(key,'voice',voice['metrics'],flush=True)
