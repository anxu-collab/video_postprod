import sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scheduler.db import Store
from providers.gateway import Gateway
from workers.common import atomic_json
g=Gateway(Store(ROOT/'state/jobs.sqlite'));token=g.issue('deployment-qualification','ark-text-health-v1')
result=g.request(token,{'model':g.model,'messages':[{'role':'user','content':'请只回复：连接成功'}],'max_tokens':16})
atomic_json(ROOT/'state/deployment/ark-connection.json',{'model':g.model,'request_id':result.get('id'),'content':result['choices'][0]['message']['content'],'usage':result['usage'],'ledger':g.ledger.snapshot(),'price_bound_ref':'config/ark-price-bound.json'})
print(json.dumps({'content':result['choices'][0]['message']['content'],'requests':g.ledger.snapshot()['requests']},ensure_ascii=False))
