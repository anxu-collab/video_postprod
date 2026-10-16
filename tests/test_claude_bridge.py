import json

import pytest


def cli_reply(model,text='好的'):
    return {'type':'result','subtype':'success','is_error':False,'result':text,'session_id':'s-1','total_cost_usd':0.01,
            'usage':{'input_tokens':3,'cache_creation_input_tokens':2,'cache_read_input_tokens':1,'output_tokens':4},
            'modelUsage':{model:{}}}


def gateway(tmp_path,text_route,runner):
    from scheduler.db import Store
    from providers.claude_cli import ClaudeCli
    from providers.gateway import Gateway
    store=Store(tmp_path/'db.sqlite')
    jobs={('job-a',1):{'payload':{'providers':{'text':text_route},'cost':{'paid_enabled':True,'job_limit_micros':2000000,'daily_limit_micros':2000000,'max_paid_requests':40}}}}
    store.get_job=lambda job_id,revision=1:jobs.get((job_id,revision))
    ark=[]
    def transport(payload):
        ark.append(payload);return {'id':'ark-1','choices':[{'message':{'content':'ark'}}],'usage':{'prompt_tokens':5,'completion_tokens':1}}
    return Gateway(store,transport=transport,claude=ClaudeCli(runner=runner,state=tmp_path/'bridge')),ark


def test_claude_route_binds_models_per_node(tmp_path):
    from providers.claude_cli import OPUS,SONNET
    g,_=gateway(tmp_path,'claude-cli',lambda *a:None)
    assert g.route_model('job-a','script-full-v1:abc')==OPUS
    assert g.route_model('job-a','script-short-v1:abc')==OPUS
    for node in ('script-review:x','locked-review:x','redub-review:x','replacement-review:x','clef-review:x'):
        assert g.route_model('job-a',node)==SONNET
    assert g.route_model('job-a','vision-v1:abc')==g.model


def test_ark_route_unchanged_and_refuses_clef_review(tmp_path):
    g,_=gateway(tmp_path,'ark-approved',lambda *a:None)
    assert g.route_model('job-a','script-full-v1:abc')==g.model
    assert g.route_model('job-a','script-review:x')==g.model
    with pytest.raises(ValueError,match='CLEF_TEXT_REVIEW_ROUTE_UNAVAILABLE'):g.route_model('job-a','clef-review:x')


def test_claude_request_replays_cache_and_skips_ark_ledger(tmp_path):
    from providers.claude_cli import OPUS
    calls=[]
    def runner(model,system,prompt,timeout):calls.append((model,system,prompt));return cli_reply(model,'旁白正文')
    g,ark=gateway(tmp_path,'claude-cli',runner)
    token=g.issue('job-a','script-full-v1:abc',model=g.route_model('job-a','script-full-v1:abc'))
    p={'model':OPUS,'messages':[{'role':'system','content':'规则'},{'role':'user','content':'写稿'}]}
    first=g.request(token,p);second=g.request(token,p)
    assert first==second and first['choices'][0]['message']['content']=='旁白正文'
    assert first['usage']=={'prompt_tokens':6,'completion_tokens':4,'total_tokens':10}
    assert calls==[(OPUS,'规则','写稿')] and ark==[] and g.ledger.snapshot()['requests']==0
    with pytest.raises(ValueError,match='UNAPPROVED_MODEL'):g.request(token,dict(p,model=g.model))


def test_claude_model_mismatch_is_not_cached(tmp_path):
    from providers.claude_cli import ClaudeCliError,OPUS,SONNET
    g,_=gateway(tmp_path,'claude-cli',lambda model,*a:cli_reply(OPUS))
    token=g.issue('job-a','script-review:x',model=SONNET)
    p={'model':SONNET,'messages':[{'role':'user','content':'核对'}]}
    with pytest.raises(ClaudeCliError,match='CLAUDE_MODEL_MISMATCH'):g.request(token,p)
    assert not list((tmp_path/'bridge').glob('cache/*/*.json'))


def test_opus_cli_is_medium_and_outside_the_repo(tmp_path,monkeypatch):
    import providers.claude_cli as bridge
    monkeypatch.setattr(bridge.shutil,'which',lambda name: r'C:\Tools\claude.exe')
    seen={}
    def run(args,**kwargs):
        seen['args']=args;seen['cwd']=kwargs['cwd']
        class Done:
            returncode=0
            stdout='{"type":"result","subtype":"success","is_error":false,"result":"ok"}'
            stderr=''
        return Done()
    monkeypatch.setattr(bridge.subprocess,'run',run)
    bridge._run_cli(bridge.OPUS,'规则','写稿',5,state=tmp_path)
    args=seen['args']
    assert args[args.index('--tools')+1]=='' and '--safe-mode' in args and '--bare' not in args
    assert args[args.index('--effort')+1]=='medium' and args[args.index('--system-prompt')+1]=='规则'
    cwd=bridge.Path(seen['cwd']).resolve()
    repo=bridge.ROOT.resolve()
    assert cwd!=repo and repo not in cwd.parents and tmp_path.resolve() not in cwd.parents
    sonnet=bridge.cli_args(r'C:\Tools\claude.exe',bridge.SONNET,'核对')
    assert '--effort' not in sonnet and '--safe-mode' in sonnet


def test_claude_request_cap_per_job(tmp_path,monkeypatch):
    import providers.claude_cli as bridge
    monkeypatch.setattr(bridge,'MAX_REQUESTS_PER_JOB',1)
    g,_=gateway(tmp_path,'claude-cli',lambda model,*a:cli_reply(model))
    token=g.issue('job-a','script-review:x',model=bridge.SONNET)
    g.request(token,{'model':bridge.SONNET,'messages':[{'role':'user','content':'一'}]})
    with pytest.raises(ValueError,match='CLAUDE_REQUEST_CAP'):
        g.request(token,{'model':bridge.SONNET,'messages':[{'role':'user','content':'二'}]})


CANDIDATES=[{'candidate_id':'c1','description':'手撕开薯片袋','observations':[{'tick':0,'text':'双手撕开袋口'}],'start_sec':0.0,'end_sec':2.0},
            {'candidate_id':'c2','description':'桌上静置的薯片','observations':[],'start_sec':0.0,'end_sec':3.0}]
SENTENCES=[{'sentence_id':'s1','text':'先把袋子撕开'},{'sentence_id':'s2','text':'倒出来看看'}]
MODEL={'sentences':[{'sentence_id':'s1','status':'OK','preferred_ids':['c1']},{'sentence_id':'s2','status':'OK','preferred_ids':['c2']}]}


def test_clef_text_review_statuses():
    from scheduler.clef_review import build_items,review
    items=build_items(SENTENCES,CANDIDATES,MODEL)
    assert [i['clef_choice'] for i in items]==['c1','c2'] and items[0]['逐帧观察']==['双手撕开袋口']
    approve=json.dumps({'sentences':[{'sentence_id':'s1','verdict':'approve','reason':'对'},{'sentence_id':'s2','verdict':'approve','reason':'对'}]})
    assert review(items,lambda s,u:approve)['status']=='APPROVED'
    reject='结论如下：'+json.dumps({'sentences':[{'sentence_id':'s1','verdict':'approve'},{'sentence_id':'s2','verdict':'reject','reason':'没有倒出动作'}]})
    assert review(items,lambda s,u:reject)['status']=='REJECTED'
    partial=json.dumps({'sentences':[{'sentence_id':'s1','verdict':'approve'}]})
    out=review(items,lambda s,u:partial)
    assert out['status']=='NEEDS_REVIEW' and out['verdicts'][1]['verdict']=='needs_review'
    def broken(s,u):raise TimeoutError('cli')
    assert review(items,broken)['status']=='ERROR'


def test_select_with_clef_text_review_gates_active_mode(tmp_path,monkeypatch):
    import scheduler.clef_selector as selector
    from scheduler.db import Store
    store=Store(tmp_path/'db.sqlite')
    class Repo:
        def __init__(self,store):pass
        def record_adjustment(self,*a):pass
        def record_decision(self,*a):return 'decision-1'
    monkeypatch.setattr(selector,'ClefRepository',Repo)
    applied=[]
    monkeypatch.setattr(selector,'_active',lambda *a:(applied.append(1) or ({'clips':[],'clef':True},{})))
    class Adapter:
        def decide(self,sentences,candidates,repository):return dict(MODEL,fallback_reason=None)
    job={'job_id':'job-a','revision':1,'clef_selector':{'mode':'active'},'assets':[]}
    script_plan={'sentences':[{'sentence_id':'s1','text':'先把袋子撕开'},{'sentence_id':'s2','text':'倒出来看看'}],'product_terms':['薯片']}
    narration={'sentences':[{'sentence_id':'s1','in_tick':0,'out_tick':48000},{'sentence_id':'s2','in_tick':48000,'out_tick':96000}]}
    baseline={'clips':[],'narration':{'body_start_tick':0}}
    args=(store,job,script_plan,narration,{},{'candidates':CANDIDATES},baseline,{})
    reject=lambda s,u:json.dumps({'sentences':[{'sentence_id':'s1','verdict':'approve'},{'sentence_id':'s2','verdict':'reject','reason':'不符'}]})
    decision,audit,_=selector.select_with_clef(*args,adapter=Adapter(),reviewer=reject)
    assert applied==[] and decision==baseline and audit['fallback_reason']=='CLAUDE_TEXT_REVIEW_REJECTED'
    assert audit['text_review']['verdicts'][1]['verdict']=='reject'
    approve=lambda s,u:json.dumps({'sentences':[{'sentence_id':'s1','verdict':'approve'},{'sentence_id':'s2','verdict':'approve'}]})
    decision,audit,_=selector.select_with_clef(*args,adapter=Adapter(),reviewer=approve)
    assert applied==[1] and decision.get('clef') is True and audit['text_review']['status']=='APPROVED'
