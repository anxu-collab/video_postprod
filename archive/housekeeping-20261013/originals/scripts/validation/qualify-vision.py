import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scheduler.db import Store
from scheduler.worker_client import request,operation
from workers.common import output_ref,atomic_json
inventory=json.loads((ROOT/'jobs/deployment-fixtures/inventory.json').read_text(encoding='utf-8'))
store=Store(ROOT/'state/jobs.sqlite')
for key in ['a1','a2','b1']:
    item=inventory[key]
    provider=request('http://127.0.0.1:8760/internal/provider-token',{'job_id':'deployment-qualification','node_key':'vision-v1:'+item['sha256']})
    result,cached=operation(store,'narrato','video.analyze',[output_ref(item['path'],'video')],{'provider':provider,'brief':'老X火龙果视频：只记录可见外观、人物和动作，不推断功效或口感。'})
    atomic_json(ROOT/'jobs/deployment-fixtures'/(key+'-vision-operation.json'),dict(result,cached=cached))
    print(key,result['state'],result['metrics'],flush=True)
