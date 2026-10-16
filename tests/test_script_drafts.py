import copy,json,threading
from pathlib import Path
import pytest
from providers.gateway import Gateway
from scheduler.db import Store


def payload(id='draft-1'):
 return {'draft_id':id,'account':'laoX','product':{'product_id':'p1','name':'产品','documents':[{'document_id':'d1','text':'完整资料，产品含原料甲。'}]},'skill':{'id':'health-supplement-selling','version':'1.1.0'},'brief':'介绍产品','entry_ids':['A01'],'cost':{'paid_enabled':True,'job_limit_micros':2000000,'daily_limit_micros':2000000,'max_paid_requests':200}}

def script():
 return {'sentences':[{'role':r,'text':t,'expression_refs':['A01'] if i==0 else [],'fact_refs':['d1']} for i,(r,t) in enumerate(zip(['pain','explanation','value','support','cta'],['场景。','说明。','价值。','支持。','行动。']))]}

def review(status='pass'):
 return {'status':status,'sentences':[{'index':i,'issues':([] if status=='pass' else ['需调整'])} for i in range(5)]}

@pytest.fixture
def engine(tmp_path,monkeypatch):
 import scheduler.script_skill as sk
 monkeypatch.setattr(sk,'SNAPSHOTS',tmp_path/'skills');monkeypatch.setattr(sk,'ROOT',tmp_path)
 # Registry and rules stay isolated while retaining real registered rule contents.
 source=Path(__file__).resolve().parents[1]
 import shutil
 shutil.copytree(source/'.agents/skills',tmp_path/'.agents/skills')
 (tmp_path/'config').mkdir();shutil.copy(source/'config/script_skills.json',tmp_path/'config/script_skills.json')
 monkeypatch.setattr(sk,'REGISTRY',tmp_path/'config/script_skills.json')
 from scheduler.script_drafts import DraftStore
 from scheduler.script_draft_generation import DraftGenerator
 main=Store(tmp_path/'jobs.sqlite');drafts=DraftStore(tmp_path/'script-drafts.sqlite',tmp_path/'script-drafts')
 calls=[]; replies=[script(),review()]
 def transport(p):
  calls.append(p);value=replies.pop(0)
  if isinstance(value,Exception):raise value
  return {'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)}}],'usage':{'prompt_tokens':10,'completion_tokens':10}}
 gateway=Gateway(main,transport=transport)
 def selector(product,catalog,**kw):
  return {'status':'partial','selected_ids':['A01'],'decisions':[{'entry_id':'A01','status':'applicable','reason_code':'product_context_fit'}],'coverage':{'not_evaluated_ids':['A02']}}
 gen=DraftGenerator(drafts,gateway,selector=selector)
 yield drafts,gen,main,calls,replies
 drafts.close();main.connection.close()

def test_create_is_isolated_immutable_and_idempotent(engine):
 d,g,m,c,r=engine;p=payload();a=d.create(p,'key');assert a['status']=='created'
 assert d.create(p,'key')==a
 assert m.connection.execute('select count(*) from jobs').fetchone()[0]==0
 assert not c and (d.folder('draft-1')/'input.json').exists()
 p['brief']='改变';
 with pytest.raises(ValueError,match='CONFLICT'):d.create(p,'key')

@pytest.mark.parametrize('id',['../x','CON','a.','a:b','NUL.txt'])
def test_ids_reject_windows_unsafe(engine,id):
 with pytest.raises(ValueError):engine[0].create(payload(id),'key')

def test_input_requires_cost_skill_product_and_explicit_account(engine):
 d=engine[0]
 for key in ['cost','skill','product','account']:
  p=payload();del p[key]
  with pytest.raises(ValueError):d.create(p,key)
 p=payload();p['skill']['id']='missing'
 with pytest.raises(ValueError):d.create(p,'unknown')

def test_generate_review_confirm_and_verified_handoff(engine):
 d,g,m,c,r=engine;d.create(payload(),'key');out=g.generate('draft-1')
 assert out['status']=='ready_for_user_review' and len(c)==2
 assert g.generate('draft-1')==out and len(c)==2
 assert out['applicability']['status']=='partial'
 with pytest.raises(ValueError):d.confirm('draft-1','bad','op')
 done=d.confirm('draft-1',out['script_sha256'],'op');assert done['status']=='confirmed'
 h=d.handoff('draft-1');assert h['script_input']['mode']=='pasted'
 d.validate_handoff({'account':'laoX','script_input':h['script_input']})
 with pytest.raises(ValueError):d.validate_handoff({'account':'other','script_input':h['script_input']})
 h['script_input']['script_text']='伪造'
 with pytest.raises(ValueError):d.validate_handoff({'account':'laoX','script_input':h['script_input']})
 assert m.connection.execute('select count(*) from jobs').fetchone()[0]==0
 assert m.connection.execute('select count(*) from paid_intents').fetchone()[0]==2
 writer=json.loads(c[0]['messages'][1]['content']);auditor=json.loads(c[1]['messages'][1]['content'])
 assert writer['source']==auditor['source']
 assert writer['source']['product']['documents'][0]['text']==payload()['product']['documents'][0]['text']

def test_writer_and_review_use_configured_ark_tier_output_limits(engine):
 d,g,m,c,r=engine;d.create(payload(),'tier-limits');out=g.generate('draft-1')
 assert out['status']=='ready_for_user_review' and len(c)==2
 assert [call['max_tokens'] for call in c]==[12288,2048]

def test_manual_child_reviews_without_rewrite(engine):
 d,g,m,c,r=engine;d.create(payload(),'key');g.generate('draft-1')
 p=payload('draft-2');p.update(parent_id='draft-1',script_text='手改一。\n手改二。\n手改三。\n手改四。\n手改五。');r.append(review())
 d.create(p,'child');out=g.generate('draft-2')
 assert len(c)==3 and out['script_text']==p['script_text']
 assert out['status']=='ready_for_user_review'
 with pytest.raises(ValueError):d.handoff('draft-2')

def test_unknown_paid_result_never_retries(engine):
 d,g,m,c,r=engine;r[:]=[TimeoutError('unknown')];d.create(payload(),'k')
 a=g.generate('draft-1');assert a['status']=='service_error'
 assert g.generate('draft-1')==a and len(c)==1
 assert m.connection.execute("select state from paid_intents").fetchone()[0]=='SUBMISSION_UNKNOWN'

def test_concurrent_generation_claim_rejected(engine):
 d,g,*_=engine;d.create(payload(),'k');assert d.claim('draft-1')
 with pytest.raises(ValueError,match='RUNNING'):g.generate('draft-1')

def test_needs_evidence_stops_and_cannot_confirm(engine):
 d,g,m,c,r=engine;r[:]=[script(),review('needs_evidence')];d.create(payload(),'k');out=g.generate('draft-1')
 assert out['status']=='needs_evidence' and len(c)==2
 with pytest.raises(ValueError):d.confirm('draft-1',out['script_sha256'],'op')

def test_maximum_two_repairs(engine):
 d,g,m,c,r=engine;r[:]=[script(),review('revise')]*3;d.create(payload(),'k');out=g.generate('draft-1')
 assert out['status']=='needs_review' and len(c)==6

@pytest.mark.parametrize('bad',[{}, {'sentences':[]}, {'sentences':[{'role':'pain','text':'x','expression_refs':['FAKE'],'fact_refs':[]}]*5}])
def test_invalid_writer_never_passes(engine,bad):
 d,g,m,c,r=engine;r[:]=[bad];d.create(payload(),'k');assert g.generate('draft-1')['status']=='service_error'

@pytest.mark.parametrize('result',[{'status':'service_error','selected_ids':['A01']},{'status':'completed','selected_ids':[]}])
def test_unusable_applicability_no_paid_request(engine,result):
 d,g,m,c,r=engine;g.selector=lambda *a,**k:result;d.create(payload(),'k');out=g.generate('draft-1')
 assert out['status'] in ['service_error','needs_review'] and not c

def test_creation_pins_catalog_and_rejects_unknown_candidate(engine,monkeypatch):
 d,g,m,c,r=engine
 p=payload();p['entry_ids']=['UNKNOWN']
 with pytest.raises(ValueError,match='ENTRY'):d.create(p,'invalid-entry')
 p=payload();p['product']['documents'][0]['document_id']='NONE'
 with pytest.raises(ValueError,match='DOCUMENT'):d.create(p,'invalid-document')
 d.create(payload(),'k')
 import scheduler.script_drafts as generation
 monkeypatch.setattr(generation,'load_catalog',lambda: (_ for _ in ()).throw(AssertionError('catalog reloaded')))
 assert g.generate('draft-1')['status']=='ready_for_user_review'
 assert 'catalog_sha256' in d.get('draft-1')['input']


def test_full_serialized_prompt_over_budget_is_rejected_without_charge(engine):
 d,g,m,c,r=engine;p=payload();p['brief']='长'*33000
 d.create(p,'k');out=g.generate('draft-1')
 assert out['status']=='service_error' and 'BUDGET' in out['issues'][0]
 assert not c and m.connection.execute('select count(*) from paid_intents').fetchone()[0]==0


def test_prompt_subset_includes_applicable_painpoint(engine):
 d,g,m,c,r=engine
 from scheduler.expression_catalog import load_catalog
 catalog=load_catalog();ids=[e['id'] for e in catalog['entries'] if e['kind']=='expression'][:9]+['PAIN-BLOOD-002']
 g.selector=lambda *a,**k:{'status':'completed','selected_ids':ids}
 d.create(payload(),'k');g.generate('draft-1')
 source=json.loads(c[0]['messages'][1]['content'])['source']
 assert any(e['kind']=='painpoint' for e in source['expressions']) and len(source['expressions'])<=8


def test_best_candidate_retained_when_later_repairs_get_worse(engine):
 d,g,m,c,r=engine
 v=review('revise');v['sentences'][0]['issues']=[]
 r[:]=[script(),v,script(),review('revise'),script(),review('revise')]
 d.create(payload(),'k');out=g.generate('draft-1')
 assert out['best_candidate']['round']==0 and len(out['rounds'])==3

@pytest.mark.parametrize('bad',[{}, {'status':'pass','sentences':[]},review('revise')|{'status':'pass'}])
def test_review_errors_do_not_pass(engine,bad):
 d,g,m,c,r=engine;r[:]=[script(),bad];d.create(payload(),'k')
 assert g.generate('draft-1')['status']=='service_error'


def test_fact_excerpt_and_unknown_expression_rejected(engine):
 d,g,m,c,r=engine
 for i,change in enumerate([{'fact_refs':[{'document_id':'d1','excerpt':'虚构原文'}]},{'expression_refs':['fake']}]):
  draft=script();draft['sentences'][0].update(change);r[:]=[draft]
  d.create(payload('ref-'+str(i)),str(i));assert g.generate('ref-'+str(i))['status']=='service_error'


def test_new_rewrite_uses_its_own_paid_node_identity(engine):
 d,g,m,c,r=engine
 d.create(payload(),'one');g.generate('draft-1')
 p=payload('draft-2');p['parent_id']='draft-1';r[:]=[script(),review()]
 d.create(p,'two');g.generate('draft-2')
 assert len(c)==4 and m.connection.execute('select count(*) from paid_intents').fetchone()[0]==4


def test_failed_budget_never_calls_transport(engine):
 d,g,m,c,r=engine;p=payload();p['cost']['job_limit_micros']=1
 d.create(p,'k');out=g.generate('draft-1')
 assert out['status']=='service_error' and not c
 assert m.connection.execute('select count(*) from paid_intents').fetchone()[0]==0

def test_actual_parallel_generation_rejects_second_request(engine):
 d,g,m,c,r=engine;d.create(payload(),'k');entered=threading.Event();release=threading.Event();original=g.selector
 def waiting(*a,**kw):
  entered.set();assert release.wait(5);return original(*a,**kw)
 g.selector=waiting;results=[]
 t=threading.Thread(target=lambda:results.append(g.generate('draft-1')));t.start()
 try:
  assert entered.wait(5)
  with pytest.raises(ValueError,match='RUNNING'):g.generate('draft-1')
 finally:release.set();t.join(5)
 assert results[0]['status']=='ready_for_user_review' and len(c)==2
@pytest.mark.parametrize('field',['product_id','name'])
def test_blank_product_identity_rejected_at_create(engine,field):
 p=payload();p['product'][field]='  '
 with pytest.raises(ValueError,match='PRODUCT'):engine[0].create(p,'k')


def test_manual_revision_issues_do_not_invoke_writer(engine):
 d,g,m,c,r=engine;d.create(payload(),'k');g.generate('draft-1')
 p=payload('manual');p.update(parent_id='draft-1',script_text='一。\n二。\n三。\n四。\n五。');r[:]=[review('revise')]
 d.create(p,'m');out=g.generate('manual')
 assert out['status']=='needs_review' and out['script_text']==p['script_text'] and len(c)==3

def test_compound_units_handoff_keeps_five_sentences_and_exact_text(engine):
 from scheduler.script_plan import build_script_plan,split_sentences
 d,g,m,c,r=engine;draft=script();draft['sentences'][0]['text']='第一点。还有第二个分句！'
 r[:]=[draft,review()];d.create(payload(),'k');out=g.generate('draft-1');d.confirm('draft-1',out['script_sha256'],'op')
 h=d.handoff('draft-1');d.validate_handoff(h)
 plan=build_script_plan(h['script_input'],{'brief':'介绍'},{'library_sha256':None})
 assert len(plan['sentences'])==5 and plan['sentences'][0]['text']=='第一点。还有第二个分句！'
 assert plan['script_text']==out['script_text'] and plan['script_sha256']==out['script_sha256']
 assert [s['sentence_id'] for s in plan['sentences']]==['s001','s002','s003','s004','s005']
 ordinary=dict(h['script_input']);ordinary.pop('draft_source')
 old=build_script_plan(ordinary,{},{});assert len(old['sentences'])==6


def test_writer_unit_internal_newline_rejected(engine):
 d,g,m,c,r=engine;draft=script();draft['sentences'][0]['text']='第一句。\n第二句。';r[:]=[draft]
 d.create(payload(),'k');out=g.generate('draft-1')
 assert out['status']=='service_error' and 'LINE' in out['issues'][0]

def test_selected_account_script_rules_are_frozen_and_shared(engine,tmp_path,monkeypatch):
 from scheduler import account_profile as ap
 d,g,m,c,r=engine
 originals=ap.registry(); copies={}
 for account in ['laozhou','laoX']:
  path=tmp_path/(account+'-profile.json')
  path.write_bytes(originals[account].read_bytes());copies[account]=str(path)
 registry=tmp_path/'account-registry.json'
 registry.write_text(json.dumps({'schema_version':'postprod-account-registry/v1','accounts':copies}),encoding='utf-8')
 monkeypatch.setattr(ap,'REGISTRY',registry)
 original=json.loads(Path(copies['laozhou']).read_text(encoding='utf-8-sig'))
 expected=original['prompt_rules']['script'];assert expected
 p=payload();p['account']='laozhou';d.create(p,'account-rules')
 changed=copy.deepcopy(original);changed['prompt_rules']['script']=['临时测试的新版本规则']
 Path(copies['laozhou']).write_text(json.dumps(changed,ensure_ascii=False),encoding='utf-8')
 out=g.generate('draft-1');assert out['status']=='ready_for_user_review'
 writer=json.loads(c[0]['messages'][1]['content'])['source']
 reviewer=json.loads(c[1]['messages'][1]['content'])['source']
 assert writer==reviewer and writer['account']['script_rules']==expected
 assert writer['account']['account']=='laozhou'
 assert set(writer['account'])=={'account','display_name','persona','profile_sha256','script_rules'}
 assert out['input']['account_snapshot']['script_rules']==expected
 assert all('source.account.script_rules' in request['messages'][0]['content'] for request in c)
 # A different account keeps its own configured script rules, never those of the prior draft.
 other=d.create(payload('other-account'),'other-account')
 assert other['input']['account_snapshot']['script_rules']==ap.current('laoX')[0]['prompt_rules']['script']

def test_applicability_failure_retains_original_audit(engine):
 d,g,m,c,r=engine
 g.selector=lambda *a,**kw:{'status':'service_error','selected_ids':[],'error':'ValueError:CLEF_GPU_MEMORY_INSUFFICIENT:available=6880','requests':[{'error':'precheck failed'}]}
 d.create(payload(),'audit');out=g.generate('draft-1')
 assert out['status']=='service_error' and out['issues']==['APPLICABILITY_SERVICE_ERROR']
 assert out['applicability']['error'].endswith('available=6880')
 saved=json.loads((d.folder('draft-1')/'result.json').read_text(encoding='utf-8'))
 assert saved['applicability']==out['applicability'] and not c
