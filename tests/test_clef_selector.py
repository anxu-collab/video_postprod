import copy
import pytest


class Repo:
    audits=[]
    def __init__(self,store):pass
    def record_decision(self,*args):self.audits.append(args);return 'decision-test'
    def record_adjustment(self,*args):return 'adjustment-test'
    def cache_get(self,k):return None
    def cache_put(self,k,v):pass


def fixture(monkeypatch,mode='active'):
    from scheduler import clef_selector
    monkeypatch.setattr(clef_selector,'ClefRepository',Repo)
    baseline={'clips':[], 'missing':[{'sentence_id':'s1'}], 'narration':{
        'body_start_tick':0, 'sentences':[{'sentence_id':'s1','in_tick':0,'out_tick':57600,'audio_in_tick':0,'audio_out_tick':57600}]}}
    job={'job_id':'j','revision':1,'clef_selector':{'mode':mode,'backend':'mock'},'intent':{},'v2_overrides':{}}
    sp={'sentences':[{'sentence_id':'s1','text':'果实外观','visual':{'required':[],'preferred':[]}}],'product_terms':[]}
    np={'source':{'actual_duration_ticks':57600},'sentences':[{'sentence_id':'s1','in_tick':0,'out_tick':57600,'status':'aligned'}]}
    cs=[{'candidate_id':'c'+str(i),'asset_id':'a','source_sha256':'a'*64,'in_tick':i*19200,'out_tick':(i+1)*19200,
         'selection_eligible':True,'description':'果实外观','native':None} for i in range(3)]
    library={'sources':[{'asset_id':'a','sha256':'a'*64,'duration_ticks':57600}]}
    return clef_selector,job,sp,np,library,{'candidates':cs},baseline,{}


@pytest.mark.parametrize('mode',['off','shadow'])
def test_off_shadow_deep_equal_baseline(monkeypatch,mode):
    m,*args=fixture(monkeypatch,mode)
    baseline=copy.deepcopy(args[-2])
    class Adapter:
        def decide(self,*a,**kw):raise AssertionError('off must not call')
    result,audit,did=m.select_with_clef(None,*args,adapter=Adapter() if mode=='off' else None)
    assert result==baseline and did=='decision-test'
    assert audit['mode']==mode


def test_three_short_shots_no_stretch_preserves_narration(monkeypatch):
    m,*args=fixture(monkeypatch)
    result,audit,_=m.select_with_clef(None,*args)
    assert len(result['clips'])==3
    assert result['narration']==args[-2]['narration']
    assert result['missing']==[]
    assert sum(c['duration_tick'] for c in result['clips'])==57600
    assert all(c['source_out_tick']-c['source_in_tick']==c['duration_tick'] for c in result['clips'])


def test_repetition_across_sentences_is_allowed(monkeypatch):
    m,*args=fixture(monkeypatch)
    args[1]['sentences'].append(dict(args[1]['sentences'][0],sentence_id='s2'))
    args[2]['sentences'].append({'sentence_id':'s2','in_tick':57600,'out_tick':115200,'status':'aligned'})
    args[-2]['narration']['sentences'].append(dict(args[-2]['narration']['sentences'][0],sentence_id='s2',in_tick=57600,out_tick=115200))
    result,_,_=m.select_with_clef(None,*args)
    assert len(result['clips'])==6
    assert result['clips'][0]['source_in_tick']==result['clips'][3]['source_in_tick']


def test_model_error_and_operator_override_keep_baseline(monkeypatch):
    m,*args=fixture(monkeypatch)
    class Bad:
        def decide(self,*a,**kw):raise TimeoutError('mock timeout')
    result,audit,_=m.select_with_clef(None,*args,adapter=Bad())
    assert result==args[-2] and audit['fallback_reason']
    args[0]['v2_overrides']['clip_replacements']=[{'clip_id':'manual','candidate_id':'c2','reason':'operator'}]
    result,audit,_=m.select_with_clef(None,*args)
    assert result==args[-2] and audit['operator_priority']


def test_manifest_refusal_and_missing_action_confirmation_cannot_be_selected(monkeypatch):
    m,*args=fixture(monkeypatch)
    args[4]['candidates'][0].update(selection_eligible=False,gate_reasons=['MANIFEST_REFUSED'])
    args[1]['sentences'][0]['text']='打开包装。'
    result,audit,_=m.select_with_clef(None,*args)
    assert result==args[-2]
    assert any('ACTION' in r for r in audit['selection_gate_reasons']['s1'].values())


def test_whole_clip_preserves_whole_source_and_all_sentence_usage(monkeypatch):
    m,*args=fixture(monkeypatch)
    args[0]['intent']['selection_mode']='whole_clip'
    args[4]['candidates']=[dict(args[4]['candidates'][0],in_tick=0,out_tick=57600)]
    result,audit,_=m.select_with_clef(None,*args)
    assert result['clips'][0]['source_in_tick']==0
    assert result['clips'][0]['source_out_tick']==57600
    assert result['clips'][0]['sentence_ids']==['s1']
    assert audit['sentences'][0]['in_tick']==0


def test_active_decision_compiles_with_locked_opening_and_real_pause_coordinates(monkeypatch):
    from scheduler.shot_planner import compile_edit_decision
    from scheduler.contracts import validate_named
    from scheduler.semantic import validate_timeline
    m,*args=fixture(monkeypatch)
    baseline=args[-2]
    baseline.update(schema_version='edit-decision/v1',job_id='j',revision=1,
        inputs={'script_sha256':'b'*64,'narration_audio_sha256':'c'*64,'library_sha256':'d'*64},tail_pause_ticks=0)
    baseline['narration'].update(audio_sha256='c'*64,body_start_tick=19200)
    baseline['narration']['sentences'][0].update(claim_ids=[],in_tick=4800,audio_in_tick=4800)
    args[2]['sentences'][0]['in_tick']=4800
    fixed={'clip_id':'opening','source':'fixed','candidate_id':None,'asset_id':'fx','source_sha256':'f'*64,
        'source_in_tick':0,'source_out_tick':19200,'timeline_start_tick':0,'duration_tick':19200,
        'sentence_ids':[],'match_reason':'fixed','overlap_exception':None,'fixed_role':'fixed_opening'}
    baseline['clips']=[fixed]
    decision,_,_=m.select_with_clef(None,*args)
    validate_named(decision,'edit-decision-v1')
    assert decision['clips'][0]==fixed
    resources={'sources':{
        'a':{'path':'F:/fixture/a.mp4','sha256':'a'*64,'probe':{'duration_ticks':57600,'stream_indices':{'audio':None}},
             'visual_evidence':{'path':'F:/fixture/a.json','sha256':'e'*64}},
        'fx':{'path':'F:/fixture/fx.mp4','sha256':'f'*64,'probe':{'duration_ticks':19200,'stream_indices':{'audio':0}},
              'visual_evidence':{'path':'F:/fixture/fx.json','sha256':'e'*64},
              'locked':{'audio_stream_index':0,'boundary':{'path':'F:/fixture/boundary.json','sha256':'b'*64}}}},
        'narration':{'path':'F:/fixture/voice.wav','sha256':'c'*64,'probe':{'duration_ticks':57600,'stream_indices':{'audio':0}}},
        'alignment':{'path':'F:/fixture/alignment.json','sha256':'d'*64},
        'voice_stem':{'path':'F:/fixture/stem.wav','sha256':'e'*64,'probe':{'duration_ticks':76800,'stream_indices':{'audio':0}}},
        'subtitle_template':{'template_id':'subtitle-bottom','version':'1.0.0'},'cues':[]}
    timeline=compile_edit_decision(decision,resources)
    validate_timeline(timeline,None,verify_files=False)
    voice=next(a for a in timeline['audio_events'] if a['role']=='narration')
    assert voice['timeline_start_tick']==24000 and voice['source_in_tick']==4800
    assert timeline['video_clips'][0]['mode']=='spoken_locked'


def test_actual_repository_preserves_full_audit_and_cached_requests(tmp_path):
    from scheduler.db import Store
    from scheduler.clef_store import ClefRepository
    from scheduler.clef_selector import select_with_clef
    store=Store(tmp_path/'clef.db')
    job={'job_id':'j','revision':1,'clef_selector':{'mode':'shadow','backend':'mock'},'v2_overrides':{}}
    sp={'sentences':[{'sentence_id':'s1','text':'fruit'}]}
    np={'sentences':[{'sentence_id':'s1','in_tick':0,'out_tick':19200}]}
    snapshot={'schema_version':'clef-candidates/v1','candidates':[{'candidate_id':'c1','asset_id':'a',
        'source_sha256':'a'*64,'in_tick':0,'out_tick':19200,'selection_eligible':True,'description':'fruit'}]}
    baseline={'clips':[],'narration':{'body_start_tick':0,'sentences':[]}}
    repository=ClefRepository(store)
    repository.record_snapshot('j',1,snapshot)
    select_with_clef(store,job,sp,np,{},snapshot,baseline,{})
    _,audit,_=select_with_clef(store,job,sp,np,{},snapshot,baseline,{})
    assert audit['model']['batches'][0]['cached']
    view=repository.view('j',1)
    assert len(view['candidates'])==1 and len(view['decisions'])==2


def test_global_model_failure_keeps_baseline_despite_ok_sentence_rows(monkeypatch):
    m,*args=fixture(monkeypatch)
    class Partial:
        def decide(self,*a,**kw):
            return {'fallback_reason':'RuntimeError:session stop failed','sentences':[{
                'sentence_id':'s1','status':'OK','ranked_ids':['c0','c1','c2'],
                'preferred_ids':['c0'],'alternatives':['c1','c2']}],'batches':[]}
    decision,audit,_=m.select_with_clef(None,*args,adapter=Partial())
    assert decision==args[-2]
    assert audit['fallback_reason']=='RuntimeError:session stop failed'
    assert audit['model']['sentences'][0]['status']=='OK'  # preserve the supplied raw result
