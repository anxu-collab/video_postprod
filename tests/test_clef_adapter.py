import pytest


def candidates(n=3):
    return [{'candidate_id': 'c'+str(i), 'selection_eligible': True,
             'source_sha256': str(i)*64, 'in_tick': 0, 'out_tick': 19200,
             'description': 'fruit'} for i in range(n)]


SENTENCES = [{'sentence_id': 's1', 'text': 'fruit', 'in_tick': 0, 'out_tick': 57600}]


def choose(payload):
    options = payload['questions']['selection']['criteria']
    return {'model': 'injected-test', 'answers': {'selection': {
        'choice': next(k for k in options if k not in ('NONE','NEEDS_REVIEW'))}}}


def test_native_choice_no_reason_or_scores_is_valid_and_all_candidates_retained():
    from scheduler.clef_adapter import ClefDecisionAdapter
    result = ClefDecisionAdapter(transport=choose).decide(SENTENCES, candidates())
    row = result['sentences'][0]
    assert row['status'] == 'OK'
    assert len(row['candidate_results']) == 3
    assert all(c['reason'] is None for c in row['candidate_results'])
    assert result['batches'][0]['request']['questions']['selection']['type'] == 'choice'
    assert 'images' not in result['batches'][0]['request']


@pytest.mark.parametrize('response', [{'choice': 'unknown'}, 'not JSON', {'choice': None}])
def test_invalid_reply_falls_back_with_full_pool(response):
    from scheduler.clef_adapter import ClefDecisionAdapter
    result = ClefDecisionAdapter(transport=lambda p: response).decide(SENTENCES, candidates())
    assert result['sentences'][0]['status'] == 'ERROR'
    assert len(result['sentences'][0]['candidate_results']) == 3
    assert result['fallback_reason']


def test_budget_does_not_truncate_audit_or_mix_batch_scores():
    from scheduler.clef_adapter import ClefDecisionAdapter
    result = ClefDecisionAdapter({'batch_size': 2, 'max_requests': 1}, choose).decide(SENTENCES, candidates(7))
    assert result['sentences'][0]['status'] == 'NEEDS_REVIEW'
    assert len(result['sentences'][0]['candidate_results']) == 7
    assert sum(b['response'] is not None for b in result['batches']) == 1
    assert result['batches'][-1]['error']
    assert result['sentences'][0]['preferred_ids'] == []


def test_common_finalist_comparison_not_raw_cross_batch_probability():
    from scheduler.clef_adapter import ClefDecisionAdapter
    calls=[]
    def transport(p):
        calls.append(p)
        ids=[x for x in p['questions']['selection']['criteria'] if x not in ('NONE','NEEDS_REVIEW')]
        choice=ids[-1] if p['state']['phase']=='finalists' else ids[0]
        return {'choice': choice, 'scores': {x: .99 if x=='c0' else .01 for x in ids}}
    row=ClefDecisionAdapter({'batch_size': 2}, transport).decide(SENTENCES,candidates(4))['sentences'][0]
    assert row['preferred_ids']==['c2']
    assert calls[-1]['state']['phase']=='finalists'


def test_mock_is_explicit_and_cache_bound_to_hash_and_versions():
    from scheduler.clef_adapter import ClefDecisionAdapter
    class Cache:
        def __init__(self): self.values={}
        def cache_get(self,k):return self.values.get(k)
        def cache_put(self,k,v):self.values[k]=v
    cache=Cache(); calls=[]
    def transport(p):calls.append(p);return choose(p)
    adapter=ClefDecisionAdapter(transport=transport)
    adapter.decide(SENTENCES,candidates(),cache)
    result=adapter.decide(SENTENCES,candidates(),cache)
    assert len(calls)==3 and all(b['cached'] for b in result['batches'])
    changed=candidates();changed[0]['source_sha256']='f'*64
    adapter.decide(SENTENCES,changed,cache)
    assert len(calls)==4
    mock=ClefDecisionAdapter({'backend':'mock'}).decide(SENTENCES,candidates())
    assert mock['model_version'].startswith('MOCK')


def test_model_uncertainty_has_explicit_sentinel_and_all_candidates():
    from scheduler.clef_adapter import ClefDecisionAdapter
    result=ClefDecisionAdapter({'backend':'mock'},lambda p:{'choice':'NEEDS_REVIEW'}).decide(SENTENCES,candidates())
    assert result['sentences'][0]['status']=='NEEDS_REVIEW'
    assert len(result['sentences'][0]['candidate_results'])==3
    q=result['batches'][0]['request']['questions']['selection']
    assert set(q)=={'type','instructions','criteria'}


def test_full_snapshot_not_sent_to_small_context_and_all_rows_retained():
    import json
    from scheduler.clef_adapter import ClefDecisionAdapter
    pool=candidates(8)
    for c in pool:c.update(description='果实外观'*4000,native={'evidence':{'observations':['x'*100000]}})
    result=ClefDecisionAdapter({'backend':'mock'}).decide(SENTENCES,pool)
    assert len(result['sentences'][0]['candidate_results'])==8
    for b in result['batches']:
        assert len(json.dumps(b['request'],ensure_ascii=False))<=4500
        assert all('native' not in c for c in b['request']['state']['candidates'])
        assert all(c['metadata_truncated'] for c in b['request']['state']['candidates'])


def test_retries_keep_every_failed_attempt():
    from scheduler.clef_adapter import ClefDecisionAdapter
    count=[0]
    def transport(p):
        count[0]+=1
        if count[0]==1:raise TimeoutError('injected first attempt')
        return choose(p)
    result=ClefDecisionAdapter({'max_retries':1},transport).decide(SENTENCES,candidates(2))
    attempts=result['batches'][0]['attempts']
    assert len(attempts)==2 and attempts[0]['error'] and attempts[1]['response']


def test_cache_reuses_stable_source_across_job_local_aliases():
    from scheduler.clef_adapter import ClefDecisionAdapter
    cache={}
    class Repo:
        def cache_get(self,k):return cache.get(k)
        def cache_put(self,k,v):cache[k]=v
    pool=candidates(2)
    for c in pool:c['asset_id']='job-one-local'
    adapter=ClefDecisionAdapter({'backend':'mock'})
    adapter.decide(SENTENCES,pool,Repo())
    for c in pool:c['asset_id']='job-two-local'
    other=[dict(SENTENCES[0],sentence_id='different-job-sentence')]
    result=adapter.decide(other,pool,Repo())
    assert all(b['cached'] for b in result['batches'])


def test_busy_gpu_falls_back_once_before_hash_or_service_start(monkeypatch):
    from scheduler import clef_adapter
    checks=[]
    def busy():checks.append('precheck');raise RuntimeError('CLEF_GPU_MEMORY_INSUFFICIENT')
    monkeypatch.setattr(clef_adapter.ClefDecisionAdapter,'_gpu_precheck',staticmethod(busy))
    monkeypatch.setattr(clef_adapter,'_model_digest',lambda *a:pytest.fail('must not read model on busy GPU'))
    monkeypatch.setattr(clef_adapter.importlib.util,'spec_from_file_location',lambda *a:pytest.fail('must not load controller'))
    result=clef_adapter.ClefDecisionAdapter().decide(SENTENCES*2,candidates())
    assert checks==['precheck']
    assert all(s['status']=='ERROR' and len(s['candidate_results'])==3 for s in result['sentences'])
    assert result['batches'][0]['phase']=='precheck'
    assert 'GPU_MEMORY_INSUFFICIENT' in result['fallback_reason']


def test_cache_invalidates_when_truncated_metadata_changes():
    from scheduler.clef_adapter import ClefDecisionAdapter
    cache={}
    class Repo:
        def cache_get(self,k):return cache.get(k)
        def cache_put(self,k,v):cache[k]=v
    pool=candidates(2);pool[0]['description']='x'*600+'before'
    adapter=ClefDecisionAdapter({'backend':'mock'})
    adapter.decide(SENTENCES,pool,Repo())
    pool[0]['description']='x'*600+'after'
    result=adapter.decide(SENTENCES,pool,Repo())
    assert not result['batches'][0]['cached']


def test_request_cached_and_precheck_times_are_real_timezone_aware(monkeypatch):
    from datetime import datetime
    from scheduler import clef_adapter
    cache={}
    class Repo:
        def cache_get(self,k):return cache.get(k)
        def cache_put(self,k,v):cache[k]=v
    adapter=clef_adapter.ClefDecisionAdapter({'backend':'mock'})
    live=adapter.decide(SENTENCES,candidates(2),Repo())
    cached=adapter.decide(SENTENCES,candidates(2),Repo())
    def busy():raise RuntimeError('CLEF_GPU_MEMORY_INSUFFICIENT')
    monkeypatch.setattr(clef_adapter.ClefDecisionAdapter,'_gpu_precheck',staticmethod(busy))
    precheck=clef_adapter.ClefDecisionAdapter().decide(SENTENCES,candidates(2))
    assert cached['batches'][0]['cached']
    for result in (live,cached,precheck):
        for batch in result['batches']:
            start=datetime.fromisoformat(batch['request_started_utc'])
            finish=datetime.fromisoformat(batch['request_finished_utc'])
            assert start.tzinfo and finish.tzinfo and start<=finish
            for attempt in batch.get('attempts',[]):
                astart=datetime.fromisoformat(attempt['request_started_utc'])
                afinish=datetime.fromisoformat(attempt['request_finished_utc'])
                assert astart.tzinfo and afinish.tzinfo and start<=astart<=afinish<=finish


def test_session_exit_failure_invalidates_previously_successful_rows(monkeypatch):
    import contextlib
    from scheduler.clef_adapter import ClefDecisionAdapter
    @contextlib.contextmanager
    def broken_session(self):
        yield choose
        raise RuntimeError('session stop failed')
    monkeypatch.setattr(ClefDecisionAdapter,'_session',broken_session)
    result=ClefDecisionAdapter({'backend':'mock'}).decide(SENTENCES,candidates(2))
    assert 'session stop failed' in result['fallback_reason']
    assert result['sentences'][0]['status']=='ERROR'
    assert result['sentences'][0]['preferred_ids']==[]
    assert result['batches'][0]['response']  # successful calls remain audit evidence
