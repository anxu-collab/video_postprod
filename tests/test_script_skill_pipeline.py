"""Explicit skill routing through the actual script stage; no model/media calls."""
import copy
import hashlib
import json

import pytest

from test_script_skill import skill_registry
from test_script_skill_api import generated_job
from scheduler.db import Store
from workers.common import atomic_json


def make_pipeline(tmp_path, monkeypatch, job):
    import scheduler.pipeline as module
    from scheduler import account_profile
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(account_profile, 'SNAPSHOTS', tmp_path / 'profiles')
    store = Store(tmp_path / 'db.sqlite')
    store.submit_job('first', job)
    return module.Pipeline(store, job)


def evidence():
    return {'brief': '展示杯子的使用动作', 'claims': [], 'documents': {}, 'max_chars': 120, 'max_seconds': 30}


LIBRARY = {'candidates': [], 'library_sha256': None}


def test_missing_skill_fails_before_any_analysis_or_cloud(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path, monkeypatch, generated_job())
    calls = []
    monkeypatch.setattr(pipeline, 'worker', lambda *a, **k: calls.append('worker'))
    monkeypatch.setattr(pipeline, 'chat', lambda *a, **k: calls.append('cloud'))
    with pytest.raises(ValueError, match='SCRIPT_SKILL_REQUIRED'):
        pipeline.run_v2()
    assert calls == []


def test_actual_generation_uses_explicit_snapshot_and_caches_it(tmp_path, monkeypatch, skill_registry):
    sk, _, rules = skill_registry
    job = generated_job(); job['script_input']['skill'] = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    job['script_input']['generation_note'] = '只写杯盖动作'
    pipeline = make_pipeline(tmp_path, monkeypatch, job)
    pipeline.profile['profiles'] = ['warming', 'selling']
    pipeline.profile['prompt_rules']['script'] = ['称呼用你']
    requests = []
    def chat(name, system, prompt, **kwargs):
        requests.append({'name': name, 'system': system, 'prompt': prompt})
        return {'id': 'test-only'}, '轻按杯盖。提起杯子。'
    monkeypatch.setattr(pipeline, 'chat', chat)
    pipeline._prepare_script_source()
    plan = pipeline._build_full_script(evidence(), LIBRARY)
    assert plan['script_text'] == '轻按杯盖。提起杯子。'
    actual = requests[0]
    assert rules.read_text(encoding='utf-8') in actual['system'] and '称呼用你' in actual['system']
    assert '只写杯盖动作' in actual['prompt']
    for bad in ('是健康类短视频', '保健品口播结构格式', '成分等于没写', '具体身体感受', '每句4到14'):
        assert bad not in actual['system'] + actual['prompt']
    record = json.loads((pipeline.work / 'script-generation.json').read_text(encoding='utf-8'))
    assert record['skill']['snapshot_sha256'] == job['script_input']['skill']['snapshot_sha256']
    assert record['system'] == actual['system'] and record['prompt'] == actual['prompt']
    assert record['request_hash'] in actual['name']
    rules.write_text('已改规则不应用于已有任务', encoding='utf-8')
    pipeline._prepare_script_source()
    assert pipeline._build_full_script(evidence(), LIBRARY)['script_text'] == plan['script_text']
    assert len(requests) == 1


def test_two_explicit_skills_change_prompts_not_account_tags(skill_registry):
    from scheduler.script_plan import script_system_prompt
    sk, data, rules = skill_registry
    first = sk.load_skill(sk.pin_skill({'id': 'demo', 'version': '1.0.0'}))
    second = dict(first, id='health-demo', rules='健康测试规则：五句。')
    profile = {'persona': '老X', 'profiles': ['selling'], 'prompt_rules': {'script': []}}
    a = script_system_prompt(profile, first)
    b = script_system_prompt(profile, second)
    assert first['rules'] in a and second['rules'] not in a
    assert second['rules'] in b and first['rules'] not in b
    assert script_system_prompt(dict(profile, profiles=['warming']), first) == a


def test_snapshot_corruption_stops_before_cloud(tmp_path, monkeypatch, skill_registry):
    sk, _, _ = skill_registry
    job = generated_job(); job['script_input']['skill'] = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    pipeline = make_pipeline(tmp_path, monkeypatch, job)
    path = sk.SNAPSHOTS / (job['script_input']['skill']['snapshot_sha256'] + '.json')
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='SCRIPT_SKILL_SNAPSHOT_CORRUPTED'):
        pipeline._prepare_script_source()


def saved_legacy_script(pipeline):
    from scheduler.script_plan import build_script_plan
    text = '保留历史已确认稿。'
    plan = build_script_plan({'mode': 'pasted', 'script_text': text, 'script_sha256': hashlib.sha256(text.encode()).hexdigest()},
                             evidence(), LIBRARY, job_id=pipeline.id, revision=pipeline.rev)
    plan['source_mode'] = 'generated'
    atomic_json(pipeline.work / 'script-plan.json', plan)
    return plan


def test_historical_artifact_can_be_reused_without_regeneration(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path, monkeypatch, generated_job())
    plan = saved_legacy_script(pipeline)
    monkeypatch.setattr(pipeline, 'chat', lambda *a, **k: pytest.fail('history must not regenerate'))
    pipeline._prepare_script_source()
    assert pipeline._build_full_script(evidence(), LIBRARY)['script_text'] == plan['script_text']


def test_skill_change_never_inherits_old_confirmations_even_for_same_text(tmp_path, monkeypatch, skill_registry):
    sk, _, _ = skill_registry
    job = generated_job(); job['script_input']['skill'] = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    pipeline = make_pipeline(tmp_path, monkeypatch, job)
    same_text_hash = 'a'*64
    atomic_json(pipeline.work / 'stage-confirmations.json', {
        k: {'sha256': same_text_hash, 'operator_id': 'person'} for k in ('SCRIPT_CONFIRM', 'NARRATION_CONFIRM')})
    new = copy.deepcopy(job); new.update(revision=2, parent_revision=1)
    new['script_input']['skill']['snapshot_sha256'] = 'b'*64
    pipeline.store.submit_job('second', new)
    from scheduler.pipeline import Pipeline
    second = Pipeline(pipeline.store, new)
    for kind in ('SCRIPT_CONFIRM', 'NARRATION_CONFIRM'):
        with pytest.raises(ValueError, match=kind+'_REQUIRED'):
            second._gate(kind, same_text_hash)


def test_unchanged_skill_revision_reuses_script_and_confirmation(tmp_path, monkeypatch, skill_registry):
    sk, _, _ = skill_registry
    job = generated_job(); job['script_input']['skill'] = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    pipeline = make_pipeline(tmp_path, monkeypatch, job)
    monkeypatch.setattr(pipeline, 'chat', lambda *a, **k: ({'id': 'test'}, '轻按杯盖。'))
    pipeline._prepare_script_source(); plan = pipeline._build_full_script(evidence(), LIBRARY)
    atomic_json(pipeline.work / 'stage-confirmations.json', {'SCRIPT_CONFIRM': {'sha256': plan['script_sha256'], 'operator_id': 'person'}})
    new = copy.deepcopy(job); new.update(revision=2, parent_revision=1)
    new['v2_overrides']['clip_replacements'] = []
    new['intent']['selection_mode'] = 'whole_clip'
    pipeline.store.submit_job('second', new)
    from scheduler.pipeline import Pipeline
    second = Pipeline(pipeline.store, new)
    monkeypatch.setattr(second, 'chat', lambda *a, **k: pytest.fail('must reuse'))
    second._prepare_script_source()
    assert second._build_full_script(evidence(), LIBRARY)['script_text'] == plan['script_text']
    assert second._gate('SCRIPT_CONFIRM', plan['script_sha256'])['operator_id'] == 'person'


def test_skill_switch_invalidates_script_but_keeps_analysis():
    from scheduler.revision_plan import plan_revision
    old = generated_job(); old['script_input']['skill'] = {'id': 'a', 'version': '1.0.0'}
    new = copy.deepcopy(old); new['script_input']['skill']['id'] = 'b'
    plan = plan_revision(old, new, {})
    assert 'script.build' in plan['invalidated'] and 'script.confirm' in plan['invalidated']
    assert 'video.analyze' in plan['reuse'] and 'material.inspect' in plan['reuse']


def test_legacy_background_generation_stops_before_inference(tmp_path, monkeypatch):
    job = generated_job(); job['schema_version'] = 'edit-job/v1'
    for field in ('script_input', 'narration_input', 'v2_overrides'): del job[field]
    for asset in job['assets']:
        del asset['role']; del asset['analysis']
    pipeline = make_pipeline(tmp_path, monkeypatch, job)
    calls = []
    monkeypatch.setattr(pipeline, 'worker', lambda *a, **k: calls.append('worker'))
    monkeypatch.setattr(pipeline, 'chat', lambda *a, **k: calls.append('cloud'))
    with pytest.raises(ValueError, match='SCRIPT_SKILL_REQUIRED'):
        pipeline.run_v1()
    assert calls == []


def test_legacy_existing_timeline_edit_is_not_forced_to_rewrite():
    from scheduler.script_generation import validate_legacy_generation
    job = generated_job(); job['schema_version'] = 'edit-job/v1'
    parent = copy.deepcopy(job)
    job['overrides']['subtitle_edits'] = [{'cue_id': 'cue-1', 'text': '改字幕'}]
    validate_legacy_generation(job, parent)
    job['overrides']['speech_edits'] = [{'event_id': 'speech-1', 'text': '改台词'}]
    with pytest.raises(ValueError, match='SCRIPT_SKILL_REQUIRED'):
        validate_legacy_generation(job, parent)


def test_explicit_rewrite_same_skill_changes_request_and_requires_confirmation(tmp_path, monkeypatch, skill_registry):
    sk, _, _ = skill_registry
    job = generated_job(); job['script_input']['skill'] = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    pipeline = make_pipeline(tmp_path, monkeypatch, job)
    requests = []
    def chat(name, *args, **kwargs):
        requests.append(name)
        return {'id': 'test'}, '相同的一句话。'
    monkeypatch.setattr(pipeline, 'chat', chat)
    pipeline._prepare_script_source(); plan = pipeline._build_full_script(evidence(), LIBRARY)
    atomic_json(pipeline.work / 'stage-confirmations.json', {'SCRIPT_CONFIRM': {'sha256': plan['script_sha256'], 'operator_id': 'person'}})
    new = copy.deepcopy(job); new.update(revision=2, parent_revision=1)
    new['script_input']['regeneration_id'] = 'explicit-rewrite-2'
    pipeline.store.submit_job('rewrite', new)
    from scheduler.pipeline import Pipeline
    second = Pipeline(pipeline.store, new)
    monkeypatch.setattr(second, 'chat', chat)
    second._prepare_script_source(); rebuilt = second._build_full_script(evidence(), LIBRARY)
    assert rebuilt['script_text'] == plan['script_text']
    assert len(requests) == 2 and requests[0] != requests[1]
    with pytest.raises(ValueError, match='SCRIPT_CONFIRM_REQUIRED'):
        second._gate('SCRIPT_CONFIRM', rebuilt['script_sha256'])


def test_real_chat_envelope_carries_explicit_skill_to_gateway(tmp_path, monkeypatch, skill_registry):
    import io
    import urllib.request
    from providers.gateway import Gateway
    sk, _, _ = skill_registry
    job = generated_job(); job['script_input']['skill'] = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    job['cost'].update(paid_enabled=True, job_limit_micros=2000000, daily_limit_micros=2000000, max_paid_requests=40)
    pipeline = make_pipeline(tmp_path, monkeypatch, job)
    payloads = []
    def transport(payload):
        payloads.append(payload)
        return {'id': 'isolated-transport', 'choices': [{'message': {'content': '轻按杯盖。'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}
    gateway = Gateway(pipeline.store, transport=transport)
    def provider(name):
        model = gateway.route_model(job['job_id'], name)
        return {'model': model, 'operation_token': gateway.issue(job['job_id'], name, model=model), 'base_url': 'http://isolated.invalid'}
    def urlopen(request, **kwargs):
        assert request.full_url == 'http://isolated.invalid/chat/completions'
        response = gateway.request(request.get_header('Authorization').removeprefix('Bearer '), json.loads(request.data))
        return io.BytesIO(json.dumps(response).encode())
    monkeypatch.setattr(pipeline, 'provider', provider)
    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    pipeline._prepare_script_source()
    assert pipeline._build_full_script(evidence(), LIBRARY)['script_text'] == '轻按杯盖。'
    assert len(payloads) == 1
    assert payloads[0]['messages'][0]['role'] == 'system'
    assert sk.load_skill(job['script_input']['skill'])['rules'] in payloads[0]['messages'][0]['content']
    assert payloads[0]['messages'][1]['role'] == 'user'
