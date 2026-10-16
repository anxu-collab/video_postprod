import pytest

def test_gateway_timeout_never_submits_twice(tmp_path):
    from scheduler.db import Store
    from providers.gateway import Gateway
    from providers.ledger import SubmissionUnknown
    calls=[]
    def timeout(payload):calls.append(payload);raise TimeoutError('accepted unknown')
    g=Gateway(Store(tmp_path/'db.sqlite'),transport=timeout);token=g.issue('deployment-qualification','node')
    p={'model':g.model,'messages':[{'role':'user','content':'你好'}],'max_tokens':10}
    with pytest.raises(TimeoutError):g.request(token,p)
    with pytest.raises(SubmissionUnknown):g.request(token,p)
    assert len(calls)==1 and g.ledger.snapshot()['requests']==1

def test_gateway_deduplicates_ignores_provider_cache_discount(tmp_path):
    from scheduler.db import Store
    from providers.gateway import Gateway
    calls=[]
    def transport(payload):
        calls.append(payload);return {'id':'paid-1','choices':[{'message':{'content':'好'}}],'usage':{'prompt_tokens':5,'completion_tokens':1}}
    g=Gateway(Store(tmp_path/'db.sqlite'),transport=transport);token=g.issue('deployment-qualification','node')
    p={'model':g.model,'messages':[{'role':'user','content':'你好'}]}
    assert g.request(token,p)==g.request(token,p)
    assert len(calls)==1 and g.ledger.snapshot()['requests']==1
    assert calls[0]['thinking']=={'type':'enabled'} and calls[0]['max_tokens']==2048
    with pytest.raises(ValueError):g.request(token,dict(p,model='another'))
