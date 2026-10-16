"""Clef audit API (P3): operator protection, full-candidate views, DB-bound media.

Behavior tests for the read-only /v2 clef endpoints added to scheduler/service.py.
They deliberately assert semantic properties from the frozen interface:
every candidate stays visible (failures, locked original-audio, unselected),
decisions/batches survive to the API redacted, media serves only DB-bound
hash-verified candidates, and queries filter by job/asset/sentence/time.
"""
import asyncio, hashlib, json, types, urllib.parse
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


class Client:
    """Raw ASGI client with query-string support (test_jobs_api's has none)."""

    def __init__(self, app):
        self.app = app

    def call(self, method, path, payload=None, headers=None, params=None):
        body = json.dumps(payload).encode() if payload is not None else b''
        query = urllib.parse.urlencode(params or {}).encode()
        messages = []

        async def run():
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return {'type': 'http.disconnect'}

            async def send(message):
                messages.append(message)

            h = {'content-type': 'application/json', **(headers or {})}
            await self.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'scheme': 'http',
                            'method': method, 'path': path, 'raw_path': path.encode(), 'query_string': query,
                            'root_path': '', 'headers': [(k.lower().encode(), v.encode()) for k, v in h.items()],
                            'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8760)}, receive, send)

        asyncio.run(run())
        data = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        return types.SimpleNamespace(status_code=status, content=data, json=lambda: json.loads(data))

    def get(self, path, headers=None, params=None):
        return self.call('GET', path, None, headers, params)

    def post(self, path, json=None, headers=None):
        return self.call('POST', path, json, headers)


AUTH = {'Authorization': 'Bearer operator-test'}


def make_client(tmp_path):
    from scheduler.db import Store
    from scheduler.service import create_app
    from providers.gateway import Gateway
    store = Store(tmp_path / 'db.sqlite')
    return store, Client(create_app(store, Gateway(store), 'operator-test', run_queue=False))


def seed_job(store, job_id, revision=1):
    with store.transaction() as c:
        c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                  (job_id, revision, 'hash', None, 'DONE', None, 'now',
                   json.dumps({'job_id': job_id, 'revision': revision}), None))


def make_snapshot(tmp_media=None):
    """Mixed pool: success + failed coverage, locked original-audio asset,
    library-only candidate and an asset with nothing but a failure row."""
    from scheduler.clef_candidates import build_candidate_snapshot
    sha_a = hashlib.sha256(b'source-a').hexdigest()
    assets = [
        {'asset_id': 'a1', 'sha256': sha_a, 'product_id': 'p1', 'role': 'product',
         'path': str(tmp_media or 'a1.mp4')},
        {'asset_id': 'opening', 'sha256': 'c' * 64, 'role': 'fixed_opening', 'path': 'opening.mp4'},
    ]
    refs = [
        {'asset_id': 'a1', 'source_sha256': sha_a, 'duration_ticks': 480000, 'coverage': [
            {'in_tick': 0, 'out_tick': 24000, 'status': 'success', 'scene': 'kitchen'},
            {'in_tick': 24000, 'out_tick': 48000, 'status': 'failed', 'error': 'timeout'}]},
        {'asset_id': 'opening', 'source_sha256': 'c' * 64, 'duration_ticks': 96000, 'coverage': [
            {'in_tick': 0, 'out_tick': 96000, 'status': 'success'}]},
    ]
    library = {'candidates': [
        {'candidate_id': 'old-1', 'source_asset_id': 'a1', 'source_sha256': sha_a,
         'source_path': str(tmp_media or 'a1.mp4'), 'in_tick': 0, 'out_tick': 24000}],
        'failures': [{'asset_id': 'ghost', 'reason': 'ANALYSIS_MISSING_FOR_ASSET'}]}
    return build_candidate_snapshot(assets, refs, library)


def repo_of(store):
    from scheduler.clef_store import ClefRepository
    return ClefRepository(store)


def test_clef_endpoints_require_operator_session(tmp_path):
    store, c = make_client(tmp_path)
    seed_job(store, 'cj')
    probes = [('/v2/jobs/cj/clef', None), ('/v2/clef/assets/a1/history', None),
              ('/v2/clef/decisions', {'job_id': 'cj'}),
              ('/v2/jobs/cj/clef-media', {'candidate_id': 'x'})]
    for path, params in probes:
        assert c.get(path, params=params).status_code == 401, path
    for path, params in probes:
        r = c.get(path, params=params, headers={**AUTH, 'Origin': 'http://evil.example'})
        assert r.status_code == 403, path
    # authenticated request for a missing job is 404, never a data leak
    assert c.get('/v2/jobs/none/clef', headers=AUTH).status_code == 404


def test_clef_view_shows_every_candidate_and_redacts_secrets(tmp_path):
    pytest.importorskip('scheduler.clef_store')
    store, c = make_client(tmp_path)
    seed_job(store, 'cj')
    repo = repo_of(store)
    snapshot = make_snapshot()
    total = len(snapshot['candidates'])
    assert total >= 4  # success, failed coverage, locked opening, ghost failure
    assert any(row['quality_status'] == 'failed' for row in snapshot['candidates'])
    assert any(row['in_tick'] is None and row['asset_id'] == 'ghost' for row in snapshot['candidates'])
    assert any(row['audio_policy'] == 'must_preserve_original' and not row['selection_eligible']
               for row in snapshot['candidates'])
    repo.record_snapshot('cj', 1, snapshot)
    audit = {'schema_version': 'clef-audit/v1', 'mode': 'shadow',
             'model': {'model_version': 'TEST-adapter', 'rule_version': 'r1',
                       'sentences': [{'sentence_id': 's1', 'status': 'OK', 'ranked_ids': ['old-1'],
                                      'preferred_ids': ['old-1'], 'alternatives': [],
                                      'candidate_results': [{'candidate_id': row['candidate_id']}
                                                            for row in snapshot['candidates']]}],
                       'batches': [{'batch_id': 1, 'sentence_id': 's1', 'phase': 'batch',
                                    'request': {'headers': {'Authorization': 'Bearer sk-clef-123456'},
                                                'body': {'api_key': 'kv-778899'}},
                                    'response': {'answers': {'selection': {'choice': 'old-1'}}},
                                    'error': None, 'elapsed_ms': 12.5}],
                       'fallback_reason': None},
             'fallback_reason': None}
    repo.record_decision('cj', 1, snapshot, audit, {'sentences': [{'sentence_id': 's1'}], 'clips': []})
    r = c.get('/v2/jobs/cj/clef', params={'revision': 1}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    # full pool, no first-8 truncation, no eligible-only filter
    assert len(data['candidates']) == total
    assert len({row['candidate_id'] for row in data['candidates']}) == total
    assert any(row['quality_status'] == 'failed' for row in data['candidates'])
    assert any(row['in_tick'] is None and row['asset_id'] == 'ghost' for row in data['candidates'])
    assert any(row['audio_policy'] == 'must_preserve_original' and not row['selection_eligible']
               for row in data['candidates'])
    assert data['snapshots'] and data['decisions']
    results = data['decisions'][0]['audit']['model']['sentences'][0]['candidate_results']
    assert len(results) == total  # per-sentence binding covers every candidate
    text = r.content.decode('utf-8')
    assert 'sk-clef-123456' not in text and 'kv-778899' not in text
    # empty job: honest empty view, not an error
    seed_job(store, 'fresh')
    empty = c.get('/v2/jobs/fresh/clef', headers=AUTH).json()
    assert empty['candidates'] == [] and empty['decisions'] == []


def test_clef_decisions_query_filters_by_job_sentence_asset_and_time(tmp_path):
    pytest.importorskip('scheduler.clef_store')
    store, c = make_client(tmp_path)
    seed_job(store, 'cj')
    seed_job(store, 'other')
    repo = repo_of(store)
    snap_a = make_snapshot()
    snap_b = make_snapshot()
    for row in snap_b['candidates']:
        row['asset_id'] = 'zz' + str(row['asset_id'])
        row['source_asset_id'] = row['asset_id']
    repo.record_decision('cj', 1, snap_a, {'model': {'sentences': []}},
                         {'sentences': [{'sentence_id': 's1'}], 'clips': []})
    repo.record_decision('other', 1, snap_b, {'model': {'sentences': []}},
                         {'sentences': [{'sentence_id': 's2'}], 'clips': []})
    rows = c.get('/v2/clef/decisions', headers=AUTH).json()['decisions']
    assert len(rows) >= 2
    rows_j = c.get('/v2/clef/decisions', params={'job_id': 'cj'}, headers=AUTH).json()['decisions']
    assert rows_j and all(row['job_id'] == 'cj' for row in rows_j)
    assert len(rows_j) < len(rows)
    rows_s = c.get('/v2/clef/decisions', params={'sentence_id': 's2'}, headers=AUTH).json()['decisions']
    assert rows_s and all('s2' in json.dumps(row) for row in rows_s)
    assert any(row['job_id'] == 'other' for row in rows_s)
    rows_asset = c.get('/v2/clef/decisions', params={'asset_id': 'a1'}, headers=AUTH).json()['decisions']
    assert rows_asset and all(row['job_id'] == 'cj' for row in rows_asset)
    stable_asset = 'asset-' + next(row['source_sha256'] for row in snap_a['candidates'] if row['source_sha256'])
    rows_global = c.get('/v2/clef/decisions', params={'asset_id': stable_asset}, headers=AUTH).json()['decisions']
    assert {row['job_id'] for row in rows_global} == {'cj', 'other'}
    history_alias = c.get('/v2/clef/assets/a1/history', headers=AUTH).json()
    assert all(row['job_id'] == 'cj' for row in history_alias['decisions'])
    history_global = c.get('/v2/clef/assets/' + stable_asset + '/history', headers=AUTH).json()
    assert {row['job_id'] for row in history_global['decisions']} == {'cj', 'other'}
    future = '2999-01-01T00:00:00+00:00'
    past = '2000-01-01T00:00:00+00:00'
    assert c.get('/v2/clef/decisions', params={'since': future}, headers=AUTH).json()['decisions'] == []
    assert c.get('/v2/clef/decisions', params={'until': past}, headers=AUTH).json()['decisions'] == []
    wide = c.get('/v2/clef/decisions', params={'since': past, 'until': future}, headers=AUTH).json()['decisions']
    assert len(wide) >= 2


def test_clef_asset_history_returns_usage_decisions_adjustments(tmp_path):
    pytest.importorskip('scheduler.clef_store')
    store, c = make_client(tmp_path)
    seed_job(store, 'cj')
    repo = repo_of(store)
    snapshot = make_snapshot()
    repo.record_snapshot('cj', 1, snapshot)
    did = repo.record_decision('cj', 1, snapshot, {'model': {'sentences': []}},
                               {'sentences': [{'sentence_id': 's1'}], 'clips': []})
    repo.record_usage('cj', 1, did, {'render_evidence': {'status': 'SUCCEEDED', 'technical_status': 'PASS',
                                                         'output_sha256': 'f' * 64},
                                     'clips': [{'asset_id': 'a1', 'candidate_id': 'old-1'}]})
    repo.record_adjustment('cj', 1, {'kind': 'manual-note', 'asset_id': 'a1', 'note': '操作者人工调整说明'})
    r = c.get('/v2/clef/assets/a1/history', headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert {'usage', 'decisions', 'adjustments'} <= set(data)
    assert len(data['usage']) == 1 and data['usage'][0]['decision_id'] == did
    assert data['decisions'] and data['decisions'][0]['decision_id'] == did
    assert data['adjustments'] and data['adjustments'][0]['payload']['kind'] == 'manual-note'
    # unknown asset: empty history, not an error
    assert c.get('/v2/clef/assets/nobody/history', headers=AUTH).json()['usage'] == []


def test_clef_media_serves_only_db_bound_hash_verified_candidates(tmp_path):
    pytest.importorskip('scheduler.clef_store')
    store, c = make_client(tmp_path)
    seed_job(store, 'cj')
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'FAKE_MP4_BYTES_FOR_BINDING_TEST')
    from workers.common import digest
    sha = digest(media)
    snapshot = make_snapshot(tmp_media=media)
    for row in snapshot['candidates']:
        if row['asset_id'] == 'a1':
            row['source_path'] = str(media)
            row['source_sha256'] = sha
            row['file_hash'] = sha
    repo_of(store).record_snapshot('cj', 1, snapshot)
    target = next(row for row in snapshot['candidates']
                  if row['asset_id'] == 'a1' and row['in_tick'] is not None)
    r = c.get('/v2/jobs/cj/clef-media', params={'candidate_id': target['candidate_id'], 'revision': 1}, headers=AUTH)
    assert r.status_code == 200 and r.content == media.read_bytes()
    # a path parameter is never honored: only DB-bound candidate ids resolve media
    r2 = c.get('/v2/jobs/cj/clef-media',
               params={'candidate_id': target['candidate_id'], 'path': str(tmp_path / 'other.mp4')},
               headers=AUTH)
    assert r2.status_code == 200 and r2.content == media.read_bytes()
    assert c.get('/v2/jobs/cj/clef-media', params={'candidate_id': 'nope'}, headers=AUTH).status_code == 404
    media.write_bytes(b'TAMPERED')
    assert c.get('/v2/jobs/cj/clef-media', params={'candidate_id': target['candidate_id']},
                 headers=AUTH).status_code == 409
    assert c.get('/v2/jobs/cj/clef-media', params={'candidate_id': target['candidate_id']}).status_code == 401


def test_v2_template_defaults_clef_shadow_and_submit_accepts_all_modes(tmp_path):
    store, c = make_client(tmp_path)
    schema = json.loads((ROOT / 'contracts/edit-job-v2.schema.json').read_text(encoding='utf-8'))
    supported = 'clef_selector' in (schema.get('properties') or {})
    p = c.get('/v2/job-template', params={'account': 'laoX'}, headers=AUTH).json()
    if not supported:
        assert 'clef_selector' not in p  # never hand out a payload the contract rejects
        return
    assert p['clef_selector']['mode'] == 'shadow'
    script = '我是老X，今天介绍的是测试商品。'
    p['revision'] = 1
    p['parent_revision'] = None
    p['script_input'] = {'mode': 'pasted', 'script_text': script,
                         'script_sha256': hashlib.sha256(script.encode('utf-8')).hexdigest(),
                         'generation_note': None}
    p['assets'] = [{'asset_id': 'clef-test-source', 'path': 'tests/fixtures/demo-a.mp4',
                    'sha256': '1' * 64, 'mode': 'broll', 'role': 'product',
                    'analysis': {'mode': 'single_shot'},
                    'provenance_ref': 'tests/fixtures/demo-a.provenance.json', 'locked': None}]
    for mode in ('active', 'off', 'shadow'):
        job = {**p, 'job_id': 'clef-ui-' + mode, 'clef_selector': {'mode': mode}}
        r = c.post('/v2/jobs', json=job, headers={**AUTH, 'Idempotency-Key': 'clef-' + mode})
        assert r.status_code == 202, (mode, r.content)
    bad = {**p, 'job_id': 'clef-ui-bad', 'clef_selector': {'mode': 'sideways'}}
    assert c.post('/v2/jobs', json=bad, headers={**AUTH, 'Idempotency-Key': 'clef-bad'}).status_code == 422
