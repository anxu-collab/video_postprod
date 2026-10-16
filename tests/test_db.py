import concurrent.futures,json
from pathlib import Path
import pytest

def test_job_idempotency_backup_and_restart(tmp_path):
    from scheduler.db import Store
    s=Store(tmp_path/'jobs.sqlite');p={'job_id':'test_job','revision':1,'parent_revision':None}
    a=s.submit_job('k',p);assert s.submit_job('k',p)==a
    with pytest.raises(ValueError):s.submit_job('k',dict(p,revision=2))
    s.backup(tmp_path/'backup.sqlite')
    assert Store(tmp_path/'backup.sqlite').get_job('test_job')==a

def test_unknown_intent_holds_budget_after_restart(tmp_path):
    from scheduler.db import Store
    from providers.ledger import Ledger,SubmissionUnknown
    s=Store(tmp_path/'jobs.sqlite');l=Ledger(s)
    a=l.reserve('r','job',50000);l.unknown(a['intent_id'],'timeout')
    l=Ledger(Store(tmp_path/'jobs.sqlite'))
    with pytest.raises(SubmissionUnknown):l.reserve('r','job',50000)
    assert l.snapshot()['requests']==1 and l.snapshot()['held_micros']==50000

def test_concurrent_budget_is_atomic(tmp_path):
    from scheduler.db import Store
    from providers.ledger import Ledger,BudgetHold
    l=Ledger(Store(tmp_path/'jobs.sqlite'),global_limit_micros=100)
    def reserve(i):
        try:return l.reserve(str(i),'job',80)['intent_id']
        except BudgetHold:return None
    with concurrent.futures.ThreadPoolExecutor(2) as pool:r=list(pool.map(reserve,[1,2]))
    assert sum(x is not None for x in r)==1

def test_paid_success_reuses_receipt_and_cache_checks_files(tmp_path):
    from scheduler.db import Store
    from providers.ledger import Ledger
    from workers.common import digest
    s=Store(tmp_path/'jobs.sqlite');l=Ledger(s)
    a=l.reserve('r','job',100);l.complete(a['intent_id'],{'id':'receipt'},50)
    assert l.reserve('r','job',100)['result']=={'id':'receipt'}
    assert l.snapshot()['requests']==1
    f=tmp_path/'artifact';f.write_text('a');s.cache_put('node',{'output_refs':[{'path':str(f),'sha256':digest(f)}]})
    assert s.cache_get('node')
    f.write_text('b');assert s.cache_get('node') is None
