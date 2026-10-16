"""Endpoint gating for the ordinary subtitle-review UI flow (no JSON for users)."""
import hashlib, json, tempfile, time, uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def make_app(tmp_path, operator_origin=None):
    from scheduler.db import Store
    from providers.gateway import Gateway
    from scheduler.service import create_app
    store = Store(tmp_path / 'jobs.sqlite')
    app = create_app(store, Gateway(store), 'operator-test', run_queue=False,
                     **({'operator_origin': operator_origin} if operator_origin else {}))
    return app, store


def client_of(app):
    import importlib.util
    spec = importlib.util.spec_from_file_location('acc', ROOT / 'scripts/acceptance-orchestration-v2.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AsgiClient(app), module


def test_v2_template_and_import_narration(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    app, store = make_app(tmp_path)
    c, module = client_of(app)
    h = {'Authorization': 'Bearer operator-test'}
    t = c.get('/v2/job-template?account=laoX', headers=h).json()
    assert t['schema_version'] == 'edit-job/v2' and t['narration_input']['mode'] == 'auto'
    wav = module.make_fixture_narration(tmp_path / 'n.wav', 2)
    r = c.post('/v1/imports?filename=n.wav&kind=narration', headers=h, raw_body=wav.read_bytes())
    assert r.status_code == 200, r.content
    body = r.json()
    assert body['probe']['stream_indices']['audio'] is not None
    # wrong type rejected
    r = c.post('/v1/imports?filename=n.txt&kind=narration', headers=h, raw_body=b'x')
    assert r.status_code == 422


def test_confirm_and_decision_endpoints_gate_on_state(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    app, store = make_app(tmp_path)
    c, module = client_of(app)
    module.install_mocks()
    h = {'Authorization': 'Bearer operator-test'}
    # fabricate a stored v2 job without running the pipeline
    job = c.get('/v2/job-template?account=laoX', headers=h).json()
    job_id = 'gate-' + uuid.uuid4().hex[:6]
    job['job_id'] = job_id
    job['script_input'] = {'mode': 'pasted', 'script_text': '一句。', 'script_sha256': hashlib.sha256('一句。'.encode()).hexdigest(), 'generation_note': None}
    clip = module.make_fixture_video(tmp_path / 'c.mp4', 'Navy', 2)
    up = c.post('/v1/imports?filename=c.mp4&kind=video', headers=h, raw_body=clip.read_bytes()).json()
    job['assets'] = [{'asset_id': 'a', 'path': up['path'], 'sha256': up['sha256'], 'mode': 'broll', 'role': 'product',
                      'analysis': {'mode': 'single_shot'}, 'provenance_ref': up['provenance_ref'], 'locked': None}]
    r = c.post('/v2/jobs', job, {**h, 'Idempotency-Key': job_id})
    assert r.status_code == 202, r.content
    # confirm endpoints refuse outside their await states
    assert c.post(f'/v2/jobs/{job_id}/confirm-script', {'revision': 1, 'operator_id': 'x'}, headers=h).status_code == 409
    assert c.post(f'/v2/jobs/{job_id}/confirm-narration', {'revision': 1, 'operator_id': 'x'}, headers=h).status_code == 409
    assert c.post(f'/v2/jobs/{job_id}/subtitle-decisions', {'revision': 1, 'sentence_id': 's001', 'choice': 'asr_error_confirmed', 'operator_id': 'x'}, headers=h).status_code == 409
    # operator id required
    r = c.post(f'/v2/jobs/{job_id}/subtitle-decisions', {'revision': 1}, headers=h)
    assert r.status_code in (409, 422)
    # review-data readable but empty until the pipeline produces artifacts
    data = c.get(f'/v2/jobs/{job_id}/review-data?revision=1', headers=h).json()
    assert data['script_plan'] is None and data['confirmations'] == {}
    # narration audio endpoint 404 before a plan exists
    assert c.get(f'/v2/jobs/{job_id}/narration-audio?revision=1', headers=h).status_code == 404
    # shot alternatives need a decision artifact
    assert c.get(f'/v2/jobs/{job_id}/shot-alternatives?clip_id=x&revision=1', headers=h).status_code == 404


def test_narration_library_lists_only_confirmed_readable(tmp_path):
    """Isolated from the shared jobs root (r5 repair): the endpoint reads
    service.ROOT, so point it at a temp tree — fake confirmations never enter
    the real narration history, and the 50-entry cap cannot evict the fixture.
    Uses a real ISO timestamp; no public jobs/cache/audio are touched."""
    import sys
    sys.path.insert(0, str(ROOT))
    app, store = make_app(tmp_path)
    c, module = client_of(app)
    h = {'Authorization': 'Bearer operator-test'}
    from workers.common import atomic_json, digest
    audio = module.make_fixture_narration(tmp_path / 'hist.wav', 3)
    isolated = tmp_path / 'isolated-root'
    folder = isolated / 'jobs' / ('hist-' + uuid.uuid4().hex[:6]) / 'rev-0001'
    folder.mkdir(parents=True)
    atomic_json(folder / 'narration-plan.json', {
        'schema_version': 'narration-plan/v1', 'job_id': folder.parent.name, 'revision': 1,
        'source': {'mode': 'uploaded', 'preset': None, 'voice': 'laox', 'audio_path': str(audio),
                   'audio_sha256': digest(audio), 'actual_duration_ticks': 144000,
                   'origin': {'kind': 'user_imported_audio', 'ref': None}, 'config_hash': None},
        'script': {'script_sha256': '0' * 64, 'sentence_count': 2},
        'sentences': [], 'confirmation': None})
    import scheduler.service as service_module
    original_root = service_module.ROOT
    service_module.ROOT = isolated
    try:
        lib = c.get('/v2/narration-library', headers=h).json()
        assert not [e for e in lib['entries'] if e['job_id'] == folder.parent.name], 'unconfirmed must not appear'
        plan = json.loads((folder / 'narration-plan.json').read_text(encoding='utf-8'))
        plan['confirmation'] = {'confirmed': True, 'operator_id': 'op',
                                'utc': '2026-10-11T07:30:00+00:00',
                                'audio_sha256_at_confirm': digest(audio)}
        atomic_json(folder / 'narration-plan.json', plan)
        lib = c.get('/v2/narration-library', headers=h).json()
        entry = next(e for e in lib['entries'] if e['job_id'] == folder.parent.name)
        assert entry['audio_sha256'] == digest(audio) and entry['duration_ticks'] == 144000
        # corrupting the audio invalidates the listing (hash check on read)
        plan['source']['audio_sha256'] = '1' * 64
        plan['confirmation']['audio_sha256_at_confirm'] = '1' * 64
        atomic_json(folder / 'narration-plan.json', plan)
        lib = c.get('/v2/narration-library', headers=h).json()
        assert not [e for e in lib['entries'] if e['job_id'] == folder.parent.name]
    finally:
        service_module.ROOT = original_root


def test_operator_origin_isolation_exact_and_default_unchanged(tmp_path):
    """r5b (DRILL5_ISOLATION_CONFIG_REVIEW): the production default accepts only
    8760; an explicitly configured isolated instance accepts only ITS exact
    origin; other origins and wrong credentials stay refused everywhere —
    including a representative real POST, not just GETs."""
    import sys
    sys.path.insert(0, str(ROOT))
    from scheduler.service import create_app
    from scheduler.db import Store
    from providers.gateway import Gateway
    import importlib.util
    spec = importlib.util.spec_from_file_location('acc', ROOT / 'scripts/acceptance-orchestration-v2.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    H = lambda origin: {'Authorization': 'Bearer operator-test', 'Content-Type': 'application/json',
                        **({'Origin': origin} if origin else {})}
    BAD = {'Authorization': 'Bearer wrong', 'Content-Type': 'application/json',
           'Origin': 'http://127.0.0.1:8763'}

    counter = [0]

    def fresh(origin=None):
        counter[0] += 1
        store = Store(tmp_path / ('jobs-origin%d.sqlite' % counter[0]))
        kwargs = {'operator_origin': origin} if origin else {}
        app = create_app(store, Gateway(store), 'operator-test', run_queue=False, **kwargs)
        return module.AsgiClient(app), store

    def seed_job(c, job_id):
        clip = module.make_fixture_video(tmp_path / (job_id + '.mp4'), 'Navy', 2)
        up = c.post('/v1/imports?filename=x.mp4&kind=video', headers=H(None), raw_body=clip.read_bytes()).json()
        job = c.get('/v2/job-template?account=laoX', headers=H(None)).json()
        job['job_id'] = job_id
        job['script_input'] = {'mode': 'pasted', 'script_text': '一句。',
                               'script_sha256': hashlib.sha256('一句。'.encode()).hexdigest(), 'generation_note': None}
        job['assets'] = [{'asset_id': 'a', 'path': up['path'], 'sha256': up['sha256'], 'mode': 'broll',
                          'role': 'product', 'analysis': {'mode': 'single_shot'},
                          'provenance_ref': up['provenance_ref'], 'locked': None}]
        r = c.post('/v2/jobs', job, {**H(None), 'Idempotency-Key': job_id})
        assert r.status_code == 202, r.content

    # default instance: 8760 ok, 8763 refused (GET and real POST)
    c1, _ = fresh()
    assert c1.get('/v1/jobs', headers=H('http://127.0.0.1:8760')).status_code == 200
    assert c1.get('/v1/jobs', headers=H('http://127.0.0.1:8763')).status_code == 403
    seed_job(c1, 'origin-default')
    r = c1.post('/v2/jobs/origin-default/confirm-script',
                {'revision': 1, 'operator_id': 'x'}, H('http://127.0.0.1:8763'))
    assert r.status_code == 403, 'POST from a non-configured origin must be refused'
    r = c1.post('/v2/jobs/origin-default/confirm-script',
                {'revision': 1, 'operator_id': 'x'}, H('http://127.0.0.1:8760'))
    assert r.status_code in (200, 409)  # 409 = state gate, but ORIGIN/AUTH passed

    # isolated instance: exact 8763 accepted (real POST included), 8760 refused,
    # wrong credentials refused even with the right origin
    c2, _ = fresh('http://127.0.0.1:8763')
    assert c2.get('/v1/jobs', headers=H('http://127.0.0.1:8763')).status_code == 200
    assert c2.get('/v1/jobs', headers=H('http://127.0.0.1:8760')).status_code == 403
    assert c2.get('/v1/jobs', headers=H('http://localhost:8763')).status_code == 403
    seed_job(c2, 'origin-8763')
    r = c2.post('/v2/jobs/origin-8763/confirm-script',
                {'revision': 1, 'operator_id': 'x'}, H('http://127.0.0.1:8763'))
    assert r.status_code in (200, 409), 'configured origin + valid token must pass the gates'
    r = c2.post('/v2/jobs/origin-8763/confirm-script',
                {'revision': 1, 'operator_id': 'x'}, H('http://127.0.0.1:8760'))
    assert r.status_code == 403
    r = c2.post('/v2/jobs/origin-8763/confirm-script',
                {'revision': 1, 'operator_id': 'x'}, BAD)
    assert r.status_code == 401

    # only the two exact local origins may ever be configured
    import pytest as _pytest
    with _pytest.raises(ValueError, match='OPERATOR_ORIGIN_NOT_ALLOWED'):
        fresh('http://127.0.0.1:9999')
    with _pytest.raises(ValueError, match='OPERATOR_ORIGIN_NOT_ALLOWED'):
        fresh('*')
