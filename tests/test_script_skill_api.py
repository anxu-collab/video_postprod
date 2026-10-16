import copy
import shutil

import pytest

from test_script_skill import skill_registry
from test_contracts_v2 import v2_job as base_job
from test_subtitle_ui_api import client_of


@pytest.fixture
def skill_api(tmp_path, monkeypatch, skill_registry):
    import scheduler.service as svc
    import scheduler.account_profile as ap
    from scheduler.db import Store
    from providers.gateway import Gateway
    root = tmp_path / 'app'
    (root / 'ui').mkdir(parents=True)
    shutil.copytree(svc.ROOT / 'contracts', root / 'contracts')
    monkeypatch.setattr(svc, 'ROOT', root)
    monkeypatch.setattr(ap, 'SNAPSHOTS', root / 'account-profiles')
    store = Store(tmp_path / 'jobs.sqlite')
    app = svc.create_app(store, Gateway(store), 'operator-test', run_queue=False)
    client, _ = client_of(app)
    yield client, store, root
    store.connection.close()


H = {'Authorization': 'Bearer operator-test'}


def v2_job():
    from scheduler.service import FIXED_ROUTES
    job = base_job()
    job['providers'] = dict(FIXED_ROUTES, text='ark-text-agent', tts='cosy-laox')
    job['templates']['bgm'] = None
    return job


def generated_job():
    job = v2_job()
    job['script_input'] = {'mode': 'generated', 'script_text': None, 'script_sha256': None}
    return job


@pytest.mark.parametrize('endpoint', ['/v1/jobs', '/v2/jobs'])
def test_generation_missing_skill_never_enters_queue(skill_api, endpoint):
    client, store, _ = skill_api
    response = client.post(endpoint, generated_job(), {**H, 'Idempotency-Key': 'missing'})
    assert response.status_code == 422 and 'SCRIPT_SKILL_REQUIRED' in str(response.json())
    assert store.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_submission_pins_skill_and_catalog_has_no_paths(skill_api, skill_registry):
    client, store, _ = skill_api
    listing = client.get('/v2/script-skills', headers=H)
    assert listing.status_code == 200 and listing.json()[0]['id'] == 'demo'
    assert 'rules_path' not in listing.json()[0]
    job = generated_job(); job['script_input']['skill'] = {'id': 'demo', 'version': '1.0.0'}
    response = client.post('/v2/jobs', job, {**H, 'Idempotency-Key': 'explicit'})
    assert response.status_code == 202, response.json()
    saved = store.get_job(job['job_id'])['payload']
    assert len(saved['script_input']['skill']['snapshot_sha256']) == 64
    # Same raw request is idempotent even after source files change.
    skill_registry[2].write_text('后续修改的正文', encoding='utf-8')
    retry = client.post('/v2/jobs', job, {**H, 'Idempotency-Key': 'explicit'})
    assert retry.status_code == 202, retry.json()


def test_revision_cannot_remove_explicit_skill(skill_api):
    client, store, _ = skill_api
    job = generated_job(); job['script_input']['skill'] = {'id': 'demo', 'version': '1.0.0'}
    assert client.post('/v2/jobs', job, {**H, 'Idempotency-Key': 'create'}).status_code == 202
    old = store.get_job(job['job_id']); new = copy.deepcopy(old['payload'])
    new.update(revision=2, parent_revision=1); del new['script_input']['skill']
    response = client.post('/v1/jobs/' + job['job_id'] + '/revisions', new,
                           {**H, 'Idempotency-Key': 'revision', 'If-Match': old['input_hash']})
    assert response.status_code == 422 and 'SCRIPT_SKILL_REQUIRED' in str(response.json())


def test_pasted_submission_does_not_require_skill(skill_api):
    client, _, _ = skill_api
    response = client.post('/v2/jobs', v2_job(), {**H, 'Idempotency-Key': 'pasted'})
    assert response.status_code == 202, response.json()


@pytest.mark.parametrize('selection,code', [
    ({'id': 'missing', 'version': '1.0.0'}, 'SCRIPT_SKILL_UNKNOWN'),
    ({'id': 'demo', 'version': '1.0.0', 'snapshot_sha256': 'f'*64}, 'SCRIPT_SKILL_SNAPSHOT_MISSING'),
])
def test_bad_binding_cannot_enter_queue(skill_api, selection, code):
    client, store, _ = skill_api
    job = generated_job(); job['script_input']['skill'] = selection
    r = client.post('/v2/jobs', job, {**H, 'Idempotency-Key': 'bad'})
    assert r.status_code == 422 and code in str(r.json())
    assert store.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_history_revision_without_skill_can_reuse_existing_script(skill_api):
    from scheduler.script_plan import build_script_plan
    from workers.common import atomic_json
    import hashlib
    client, store, root = skill_api
    job = generated_job()
    old = store.submit_job('historical', job)
    text = '保留历史原稿。'
    plan = build_script_plan({'mode': 'pasted', 'script_text': text, 'script_sha256': hashlib.sha256(text.encode()).hexdigest()},
                             {'brief': '原始要求', 'claims': []}, {'candidates': []}, job_id=job['job_id'])
    plan['source_mode'] = 'generated'
    atomic_json(root / 'jobs' / job['job_id'] / 'rev-0001/script-plan.json', plan)
    new = copy.deepcopy(job); new.update(revision=2, parent_revision=1)
    r = client.post('/v1/jobs/' + job['job_id'] + '/revisions', new,
                   {**H, 'Idempotency-Key': 'history-edit', 'If-Match': old['input_hash']})
    assert r.status_code == 202, r.json()


def test_history_without_artifact_must_choose_skill_on_revision(skill_api):
    client, store, _ = skill_api
    job = generated_job(); old = store.submit_job('historical', job)
    new = copy.deepcopy(job); new.update(revision=2, parent_revision=1)
    r = client.post('/v1/jobs/' + job['job_id'] + '/revisions', new,
                   {**H, 'Idempotency-Key': 'history-edit', 'If-Match': old['input_hash']})
    assert r.status_code == 422 and 'SCRIPT_SKILL_REQUIRED' in str(r.json())


def test_legacy_v1_cannot_submit_unbound_generation(skill_api):
    client, store, _ = skill_api
    job = v2_job()
    job['schema_version'] = 'edit-job/v1'
    for field in ('script_input', 'narration_input', 'v2_overrides'): del job[field]
    for asset in job['assets']:
        del asset['role']; del asset['analysis']
    response = client.post('/v1/jobs', job, {**H, 'Idempotency-Key': 'v1-bypass'})
    assert response.status_code == 422 and 'SCRIPT_SKILL_REQUIRED' in str(response.json())
    assert store.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
