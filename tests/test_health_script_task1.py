import copy, hashlib, json
from pathlib import Path
import pytest
from scheduler.script_plan import _generation_prompt
from scheduler.script_generation import source_hash
from scheduler.revision_plan import plan_revision
from test_script_skill_api import skill_api, v2_job, H
from test_script_skill import skill_registry
from test_narration_inputs import FakeAudio, base_job, plan_for
from workers.common import atomic_json
from test_script_skill_ui import ui_page
from scheduler.pipeline import Pipeline

# Older acceptance helpers install process-wide mocks. Keep the real target
# captured at collection time so this assertion still exercises production.
REAL_FACT_REVIEW = Pipeline.fact_review


def test_ui_unsaved_edit_disables_confirm_and_hash_is_bound(ui_page):
    page, _, state, _ = ui_page
    text='Saved script.'
    sha=hashlib.sha256(text.encode()).hexdigest()
    state['listed_jobs']=[{'job_id':'edit-test','revision':1,'state':'AWAIT_SCRIPT_CONFIRM'}]
    state['job_detail']={'job_id':'edit-test','revision':1,'state':'AWAIT_SCRIPT_CONFIRM','input_hash':'old',
                         'payload':{'schema_version':'edit-job/v2','script_input':{'mode':'pasted'}},'nodes':[],'result':None,'errors':{}}
    state['review_data']={'script_plan':{'script_text':text,'script_sha256':sha,'sentences':[{'sentence_id':'s1'}]}}
    page.reload();page.get_by_role('button',name='查看',exact=True).click()
    button=page.get_by_role('button',name='我已核对，确认这版整稿')
    ta=page.locator('#stagePanel textarea')
    ta.fill('Unsaved script.')
    assert button.is_disabled()
    ta.fill(text)
    assert button.is_enabled()
    with page.expect_request('**/confirm-script') as request:
        button.click()
    assert request.value.post_data_json['script_sha256']==sha


def test_old_material_bound_hash_remains_reusable(tmp_path):
    from scheduler.db import Store
    from scheduler.script_generation import find_saved_script
    from scheduler.script_plan import build_script_plan
    from scheduler.contracts import canonical_hash
    old=v2_job();old['script_input']={'mode':'generated','script_text':None,'script_sha256':None}
    store=Store(tmp_path/'jobs.sqlite');store.submit_job('old',old)
    text='Saved.'
    plan=build_script_plan({'mode':'pasted','script_text':text,'script_sha256':hashlib.sha256(text.encode()).hexdigest()}, {}, {},job_id=old['job_id'])
    folder=tmp_path/'jobs'/old['job_id']/'rev-0001'
    old_inputs={k:old.get(k) for k in ('script_input','account','account_profile_sha256','profile','product_documents','claims','assets')}
    old_inputs['intent']={k:old.get('intent',{}).get(k) for k in ('brief','target_duration_ticks','product_terms','language')}
    old_inputs['text_provider']=old.get('providers',{}).get('text')
    atomic_json(folder/'script-plan.json',plan);atomic_json(folder/'script-context.json',{'source_hash':canonical_hash(old_inputs)})
    new=copy.deepcopy(old);new.update(revision=2,parent_revision=1);new['assets'][0]['sha256']='f'*64
    store.submit_job('new',new)
    assert find_saved_script(tmp_path,new,store)['plan']['script_text']==text


def test_all_product_documents_are_sent_without_truncation():
    documents={str(i):'甲'*900+f'文件末尾{i}' for i in range(8)}
    prompt=_generation_prompt({'documents':documents}, {})
    assert all(v in prompt for v in documents.values())


def test_over_budget_input_is_explicitly_rejected():
    with pytest.raises(ValueError,match='SCRIPT_INPUT_BUDGET_EXCEEDED'):
        _generation_prompt({'documents':{'large':'甲'*32001}}, {})


def test_assets_do_not_change_script_source_or_revision():
    old=v2_job();old['script_input']['mode']='generated'
    new=copy.deepcopy(old);new['assets'][0]['sha256']='f'*64
    assert source_hash(old)==source_hash(new)
    plan=plan_revision(old,new,{})
    assert 'script.build' in plan['reuse'] and 'script.confirm' in plan['reuse']
    assert 'speech.transcribe' in plan['reuse']
    assert 'video.analyze' in plan['invalidated']
    assert 'edit.plan' in plan['invalidated']


@pytest.mark.parametrize('field', ['claims','product_documents','account','account_profile_sha256','profile'])
def test_writing_input_changes_invalidate_script(field):
    old=v2_job();new=copy.deepcopy(old)
    new[field]=([] if field=='claims' else [{'document_id':'new'}] if field=='product_documents' else 'new-value')
    if new[field]==old.get(field):new[field]=[{'text':'new'}]
    assert source_hash(old)!=source_hash(new)
    assert 'script.build' in plan_revision(old,new,{})['invalidated']


def test_confirm_rejects_text_hash_mismatch_and_accepts_legacy_no_hash(skill_api):
    client,store,root=skill_api;job=v2_job();store.submit_job('create',job);store.set_state(job['job_id'],1,'AWAIT_SCRIPT_CONFIRM')
    folder=root/'jobs'/job['job_id']/'rev-0001';text='已保存整稿。';sha=hashlib.sha256(text.encode()).hexdigest()
    atomic_json(folder/'script-plan.json',{'script_text':text,'script_sha256':sha,'sentences':[{'sentence_id':'s001','text':text}]})
    response=client.post(f"/v2/jobs/{job['job_id']}/confirm-script",{'operator_id':'op','script_sha256':'a'*64},H)
    assert response.status_code==409 and 'SCRIPT_CONFIRM_HASH_MISMATCH' in str(response.json())
    assert store.get_job(job['job_id'])['state']=='AWAIT_SCRIPT_CONFIRM'
    assert client.post(f"/v2/jobs/{job['job_id']}/confirm-script",{'operator_id':'op'},H).status_code==200


@pytest.mark.parametrize('source_account',['another',None])
def test_history_account_mismatch_or_unknown_fails(tmp_path,source_account):
    audio=FakeAudio(tmp_path);job=base_job();job['account']='laoX'
    job['narration_input'].update(mode='history',history={'job_id':'other','revision':1,'narration_sha256':audio.sha256})
    prior={'schema_version':'narration-plan/v1','_account':source_account,'source':{'audio_path':str(audio.path),'audio_sha256':audio.sha256,'voice':'laox'},'confirmation':{'confirmed':True,'operator_id':'op','audio_sha256_at_confirm':audio.sha256}}
    with pytest.raises(ValueError,match='NARRATION_HISTORY_ACCOUNT_'):
        plan_for(job,resolver=lambda *_:prior)


def test_no_fact_rules_reports_skipped_without_grounding():
    from scheduler.pipeline import Pipeline
    instance=object.__new__(Pipeline);instance.profile={'prompt_rules':{'fact_review':[]}}
    result=REAL_FACT_REVIEW(instance,'稿件',[],[],'review')
    assert result['status']=='SKIPPED' and result['grounded'] is None


def test_generation_budget_includes_skill_and_system_before_cloud(tmp_path,monkeypatch,skill_registry):
    from test_script_skill_pipeline import make_pipeline, evidence, LIBRARY
    sk,_,_=skill_registry
    job=v2_job();job['script_input']={'mode':'generated','script_text':None,'script_sha256':None,
                                    'skill':sk.pin_skill({'id':'demo','version':'1.0.0'})}
    pipeline=make_pipeline(tmp_path,monkeypatch,job);pipeline._prepare_script_source()
    pipeline.script_source['snapshot']['rules']='x'*32001
    monkeypatch.setattr(pipeline,'chat',lambda *_args,**_kwargs:pytest.fail('over budget must stop before cloud'))
    with pytest.raises(ValueError,match='SCRIPT_INPUT_BUDGET_EXCEEDED'):
        pipeline._build_full_script(evidence(),LIBRARY)


def test_history_library_filters_account_and_checks_confirmation_hash(skill_api,tmp_path):
    client,store,root=skill_api;job=v2_job();store.submit_job('history-job',job)
    audio=FakeAudio(tmp_path);folder=root/'jobs'/job['job_id']/'rev-0001'
    plan={'source':{'audio_path':str(audio.path),'audio_sha256':audio.sha256,'actual_duration_ticks':96000,'mode':'uploaded'},
          'script':{'sentence_count':1,'script_sha256':'a'*64},
          'confirmation':{'confirmed':True,'operator_id':'op','audio_sha256_at_confirm':audio.sha256}}
    atomic_json(folder/'narration-plan.json',plan)
    assert client.get('/v2/narration-library?account=another',headers=H).json()['entries']==[]
    matching=client.get('/v2/narration-library?account='+job['account'],headers=H).json()['entries']
    assert len(matching)==1 and matching[0]['account']==job['account']
    response=client.get(f"/v2/narration-library/{job['job_id']}/1/audio?account=another",headers=H)
    assert response.status_code==409
    plan['confirmation']['audio_sha256_at_confirm']='b'*64;atomic_json(folder/'narration-plan.json',plan)
    assert client.get('/v2/narration-library?account='+job['account'],headers=H).json()['entries']==[]
