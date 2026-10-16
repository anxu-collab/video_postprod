"""Incremental reuse for v2 revisions with the real operation cache (A5/A6)."""
import hashlib, importlib.util, json, time, uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/acceptance-orchestration-v2.py'


def load_script():
    spec = importlib.util.spec_from_file_location('acceptance_orchestration_v2', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_state(client, h, job_id, revision, wanted, timeout=240):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f'/v1/jobs/{job_id}?revision={revision}', headers=h).json()
        last = r['state']
        if last in wanted:
            return r
        time.sleep(0.4)
    raise TimeoutError('WAIT_STATE_TIMEOUT last=%s want=%s' % (last, wanted))


@pytest.fixture
def isolated_jobs_root(tmp_path):
    """r6 (WHOLE_CLIP_REVIEW_REPAIR_2): pipeline/service job artifacts for these
    tests go to a TEMP root under the project — fake stage confirmations never
    enter the public jobs history the narration library reads. Patches the exact
    module roots involved and always restores them."""
    import scheduler.pipeline as pipeline_module
    import scheduler.service as service_module
    import tempfile
    isolated = Path(tempfile.mkdtemp(prefix='orchv2-iso-jobs-', dir=str(ROOT / 'state/deployment/orchestration-v2')))
    (isolated / 'jobs').mkdir(parents=True, exist_ok=True)
    (isolated / 'ui').mkdir(exist_ok=True)   # StaticFiles mount target under the patched root
    __import__('shutil').copytree(ROOT / 'contracts', isolated / 'contracts', dirs_exist_ok=True)  # job-template reads
    _sh = __import__('shutil')
    _music_dir = isolated.parent / 'assets/music/montauk_point'
    _music_dir.mkdir(parents=True, exist_ok=True)
    _real_music = ROOT.parent / 'assets/music/montauk_point/Montauk_Point_Kevin_MacLeod.mp3'
    if _real_music.is_file():
        _sh.copy2(_real_music, _music_dir / _real_music.name)
    _brand = ROOT.parent / 'assets/music/brand_music.json'
    if _brand.is_file():
        _sh.copy2(_brand, isolated.parent / 'assets/music/brand_music.json')
    old_pipeline, old_service = pipeline_module.ROOT, service_module.ROOT
    pipeline_module.ROOT = isolated
    service_module.ROOT = isolated
    try:
        yield isolated
    finally:
        pipeline_module.ROOT = old_pipeline
        service_module.ROOT = old_service


def test_auto_narration_synthesizes_once_then_cache_reuses(tmp_path, isolated_jobs_root):
    module = load_script()
    module.install_mocks()
    import tempfile
    from scheduler.db import Store
    from providers.gateway import Gateway
    from scheduler.service import create_app
    tmp = Path(tempfile.mkdtemp(prefix='orchv2-incr-'))
    store = Store(tmp / 'jobs.sqlite')
    app = create_app(store, Gateway(store), 'operator-test', run_queue=False)
    app.state.queue.thread.start()
    client = module.AsgiClient(app)
    h = {'Authorization': 'Bearer operator-test'}
    try:
        clip = module.make_fixture_video(isolated_jobs_root / 'body.mp4', 'Teal', 4)
        module.MOCK_ANALYSIS[digest_of(clip)] = [{'in_tick': 0, 'out_tick': 192000, 'text': '双手打开包装盒取出火龙果果实'}]
        script_text = '打开包装，看看里面的果实。'
        uploaded = upload(client, h, clip)
        r = client.post('/v1/color-previews', {'decision': 'allow_preview_bt709', 'source_hashes': [uploaded['sha256']]}, headers=h)
        assert r.status_code == 200
        job = client.get('/v2/job-template?account=laoX', headers=h).json()
        job_id = 'incr-' + uuid.uuid4().hex[:6]
        job['job_id'] = job_id
        job['intent']['brief'] = '火龙果画面介绍'
        job['intent']['target_duration_ticks'] = 1920 * 60
        job['script_input'] = {'mode': 'pasted', 'script_text': script_text,
                               'script_sha256': hashlib.sha256(script_text.encode()).hexdigest(), 'generation_note': None}
        job['narration_input'] = {'mode': 'auto', 'preset': 'laox_warming_compact', 'uploaded': None, 'history': None}
        job['assets'] = [{'asset_id': 'a-body', 'path': uploaded['path'], 'sha256': uploaded['sha256'], 'mode': 'broll',
                          'role': 'product', 'analysis': {'mode': 'single_shot'},
                          'provenance_ref': uploaded['provenance_ref'], 'locked': None}]
        # r3: the opening sentence is served from a fixture-confirmed interval
        # (simulated post-review state); inference alone never auto-selects
        job['v2_overrides']['confirmed_windows'] = [
            {'sentence_id': 's001', 'source_asset_id': 'a-body', 'source_sha256': uploaded['sha256'],
             'in_tick': 0, 'out_tick': 174720, 'action_kind': 'open_packaging', 'action_object': 'packaging',
             'basis': 'fixture: reviewed opening interval', 'operator_id': 'fixture-reviewed',
             'allow_source_window_overlap': False}]
        r = client.post('/v2/jobs', job, {**h, 'Idempotency-Key': job_id})
        assert r.status_code == 202, r.content
        st = wait_state(client, h, job_id, 1, {'AWAIT_SCRIPT_CONFIRM', 'FAILED_FINAL'})
        assert st['state'] == 'AWAIT_SCRIPT_CONFIRM', st.get('errors')
        client.post(f'/v2/jobs/{job_id}/confirm-script', {'revision': 1, 'operator_id': 'incr'}, headers=h)
        # after confirm, the pipeline synthesizes; catch its output sha by polling review-data narration plan
        deadline = time.time() + 120
        plan = None
        while time.time() < deadline:
            data = client.get(f'/v2/jobs/{job_id}/review-data?revision=1', headers=h).json()
            plan = data.get('narration_plan')
            if plan:
                break
            time.sleep(0.5)
        assert plan, 'narration plan never appeared'
        module.MOCK_ASR[plan['source']['audio_sha256']] = module.asr_words_for(script_text, plan['source']['actual_duration_ticks'])
        st = wait_state(client, h, job_id, 1, {'AWAIT_NARRATION_CONFIRM', 'FAILED_FINAL'})
        assert st['state'] == 'AWAIT_NARRATION_CONFIRM', st.get('errors')
        tts_after_first = module.CALLS['speech.synthesize']
        assert tts_after_first == 1, 'auto mode must synthesize the full script exactly once'
        client.post(f'/v2/jobs/{job_id}/confirm-narration', {'revision': 1, 'operator_id': 'incr'}, headers=h)
        st = wait_state(client, h, job_id, 1, {'REVIEW_REQUIRED', 'FAILED_FINAL', 'AWAIT_SUBTITLE_REVIEW'})
        if st['state'] == 'AWAIT_SUBTITLE_REVIEW':
            data = client.get(f'/v2/jobs/{job_id}/review-data?revision=1', headers=h).json()
            for item in data['subtitle_review']['items']:
                if item['status'] == 'matched':
                    continue
                rr = client.post(f'/v2/jobs/{job_id}/subtitle-decisions',
                                 {'revision': 1, 'sentence_id': item['sentence_id'], 'choice': 'asr_error_confirmed',
                                  'notes': 'ok', 'operator_id': 'incr'}, headers=h)
                assert rr.status_code == 200, rr.content
            st = wait_state(client, h, job_id, 1, {'REVIEW_REQUIRED', 'FAILED_FINAL'})
        assert st['state'] == 'REVIEW_REQUIRED' and st['result']['technical_status'] == 'PASS', st.get('errors')

        # subtitle-only revision: the operation cache must serve TTS/ASR again
        cues = st['timeline']['subtitles']['cues']
        rev2 = json.loads(json.dumps(job))
        rev2['revision'] = 2; rev2['parent_revision'] = 1
        rev2['overrides']['base_timeline_sha256'] = st['result']['timeline_sha256']
        rev2['overrides']['subtitle_edits'] = [{'cue_id': cues[0]['cue_id'], 'text': cues[0]['text'] + '（改）'}]
        rev2['changed_paths'] = ['/overrides/subtitle_edits']
        r = client.post(f'/v1/jobs/{job_id}/revisions', rev2, {**h, 'If-Match': st['input_hash'], 'Idempotency-Key': job_id + '-r2'})
        assert r.status_code == 202, r.content
        st2 = wait_state(client, h, job_id, 2, {'REVIEW_REQUIRED', 'FAILED_FINAL'})
        assert st2['state'] == 'REVIEW_REQUIRED' and st2['result']['technical_status'] == 'PASS', st2.get('errors')
        assert module.CALLS['speech.synthesize'] == tts_after_first, 'cached TTS must not re-invoke the transport'
        assert st2['result']['metrics']['voice']['cached'] is True, 'voice stem should be a cache hit'
    finally:
        app.state.queue.stop.set()


def digest_of(path):
    from workers.common import digest
    return digest(path)


def upload(client, h, path, kind='video'):
    r = client.post('/v1/imports?filename=' + path.name + '&kind=' + kind, headers=h, raw_body=path.read_bytes())
    assert r.status_code == 200, r.content
    return r.json()


# ---- R2 round-1 repair: confirmed narration is never silently trimmed ----

LONG_TAIL_SCRIPT = '打开包装，看看里面的果肉。果肉是白心的。'


def _long_tail_words():
    """Real-audio-shaped ASR: 21 chars at 9600 ticks each (≈4.2s of speech) while the
    confirmed audio is 9s — a 4.8s trailing silence the system must NOT cut."""
    words = []
    for i, ch in enumerate(LONG_TAIL_SCRIPT):
        if ch in '，。':
            continue
        words.append({'text': ch, 'start_tick': i * 9600, 'end_tick': (i + 1) * 9600})
    return words


def _build_long_tail_job(module, tmp, tail_trim=None, short_candidates=False, root=None):
    """Synchronous v2 job fixture: 9s uploaded narration with a long silent tail.
    Media must live under the project root (import allowlist)."""
    import shutil
    from scheduler.db import Store
    from providers.gateway import Gateway
    from scheduler.service import create_app
    from scheduler.pipeline import Pipeline, import_file
    from workers.common import atomic_json, digest

    ws = (root or ROOT) / ('media-' + uuid.uuid4().hex[:6]) if root else ROOT / ('state/deployment/orchestration-v2/tmp-tailtest-' + uuid.uuid4().hex[:6])
    ws.mkdir(parents=True)
    clip_seconds = 4 if short_candidates else 11
    body = module.make_fixture_video(ws / 'body.mp4', 'Teal', clip_seconds)
    narration = module.make_fixture_narration(ws / 'narr.wav', 9)
    module.MOCK_ANALYSIS[digest(body)] = (
        [{'in_tick': 0, 'out_tick': 192000, 'text': '双手打开包装盒取出火龙果'}] if short_candidates else
        [{'in_tick': 0, 'out_tick': 192000, 'text': '双手打开包装盒取出火龙果'},
         {'in_tick': 192000, 'out_tick': 528000, 'text': '白色火龙果果肉黑色籽粒的特写展示'}])
    module.MOCK_ASR[digest(narration)] = _long_tail_words()

    ups = {name: import_file(p) for name, p in [('body', body), ('narr', narration)]}
    # same authorization the UI color checkbox writes, for technical previews
    auth_path = (root or ROOT) / 'state/deployment/color-preview-authorization.json'
    auth = json.loads(auth_path.read_text(encoding='utf-8')) if auth_path.exists() else {'source_hashes': []}
    auth.update(decision='allow_preview_bt709',
                source_hashes=sorted(set(auth['source_hashes'] + [digest(ups['body'])])),
                production_color_qualified=False, operator_record_utc='test')
    atomic_json(auth_path, auth)
    for name in ['body']:
        Path(str(ups[name]) + '.prov.json').write_text(
            json.dumps({'source_sha256': digest(ups[name]), 'origin': 'test'}), encoding='utf-8')
    job = {
        'schema_version': 'edit-job/v2', 'job_id': 'tail-' + uuid.uuid4().hex[:6], 'revision': 1,
        'parent_revision': None, 'account': 'laoX', 'profile': 'product_intro',
        'intent': {'brief': '火龙果画面介绍', 'language': 'zh', 'target_duration_ticks': 1920 * 120,
                   'product_badge_requested': False, 'product_terms': ['火龙果']},
        'product_documents': [], 'claims': [],
        'assets': [{'asset_id': 'a-body', 'path': str(ups['body']), 'sha256': digest(ups['body']),
                    'mode': 'broll', 'role': 'product', 'analysis': {'mode': 'single_shot'},
                    'provenance_ref': str(ups['body']) + '.prov.json', 'locked': None}],
        'script_input': {'mode': 'pasted', 'script_text': LONG_TAIL_SCRIPT,
                         'script_sha256': hashlib.sha256(LONG_TAIL_SCRIPT.encode()).hexdigest(),
                         'generation_note': None},
        'narration_input': {'mode': 'uploaded', 'preset': None,
                            'uploaded': {'path': str(ups['narr']), 'sha256': digest(ups['narr'])}, 'history': None},
        'providers': {'text': 'ark-approved', 'vision': 'ark-approved', 'tts': 'cosy-laox',
                      'asr': 'mpt-cuda-small', 'lipsync': None},
        'templates': {'subtitle': {'template_id': 'subtitle-bottom', 'version': '1.0.0'},
                      'bgm': None, 'effects': [], 'sfx': [], 'product_badge': {'enabled': False, 'template': None}},
        'overrides': {'base_timeline_sha256': None, 'subtitle_edits': [], 'bgm_override': None,
                      'shot_edits': [], 'speech_edits': [], 'template_overrides': []},
        'v2_overrides': {'clip_replacements': [], 'sentence_redubs': [], 'tail_trim': tail_trim,
                         # r3: fixture-confirmed interval for the opening sentence
                         'confirmed_windows': [
                             {'sentence_id': 's001', 'source_asset_id': 'a-body',
                              'source_sha256': digest(ups['body']), 'in_tick': 0, 'out_tick': 124800,
                              'action_kind': 'open_packaging', 'action_object': 'packaging',
                              'basis': 'fixture: reviewed opening interval', 'operator_id': 'fixture-reviewed',
                              'allow_source_window_overlap': False}]},
        'cost': {'paid_enabled': False, 'currency': 'CNY', 'job_limit_micros': 0, 'daily_limit_micros': 0, 'max_paid_requests': 0},
        'output': {'width': 768, 'height': 1376, 'fps': 25, 'audio_sample_rate': 48000},
        'review_policy': {'visual': 'manual', 'lipsync': 'manual', 'content': 'evidence_required'},
        'changed_paths': [],
    }
    from scheduler.contracts import validate_job
    validate_job(job)
    store = Store(ws / 'jobs.sqlite')
    store.submit_job('tail-key-' + uuid.uuid4().hex[:6], job)
    work = (root or ROOT) / 'jobs' / job['job_id'] / 'rev-0001'
    atomic_json(work / 'stage-confirmations.json', {
        'SCRIPT_CONFIRM': {'sha256': job['script_input']['script_sha256'], 'operator_id': 'test', 'utc': '2026-10-11T08:00:00+00:00'},
        'NARRATION_CONFIRM': {'sha256': digest(ups['narr']), 'operator_id': 'test', 'utc': '2026-10-11T08:00:00+00:00'}})
    return store, job, str(ups['narr'])


def _pcm_of(path, start_sample, count):
    import array, wave
    with wave.open(str(path), 'rb') as f:
        assert f.getframerate() == 48000 and f.getnchannels() == 2 and f.getsampwidth() == 2
        f.setpos(min(start_sample, f.getnframes()))
        a = array.array('h', f.readframes(count))
    return a


def test_long_tail_confirmed_narration_not_cut_by_default(tmp_path, isolated_jobs_root):
    module = load_script()
    module.install_mocks()
    from scheduler.pipeline import Pipeline
    store, job, narr_path = _build_long_tail_job(module, tmp_path, root=isolated_jobs_root)
    result = Pipeline(store, job).run()
    assert result['technical_status'] == 'PASS', result['qa']
    timeline = json.loads((isolated_jobs_root / 'jobs' / job['job_id'] / 'rev-0001' / 'timeline.json').read_text(encoding='utf-8'))
    events = [e for e in timeline['audio_events'] if e['role'] == 'narration']
    assert events, 'narration events missing'
    # contiguous sentence windows merge into ONE event equal to the confirmed audio:
    # original coordinates, full 9s tail, no cut, no compression
    assert len(events) == 1, [ (e['source_in_tick'], e['source_out_tick']) for e in events ]
    assert events[0]['source_in_tick'] == 0 and events[0]['timeline_start_tick'] == 0
    assert events[0]['source_out_tick'] == 9 * 48000, 'tail was cut: %s' % events[0]['source_out_tick']
    assert events[0]['duration_tick'] == 9 * 48000
    # pre-mix voice stem is sample-identical to the confirmed audio
    refs = {r['ref_id']: r for r in timeline['resources']}
    stem = refs[timeline['voice_stem_ref']]['path']
    body_samples = timeline['duration_ticks']
    src = _pcm_of(narr_path, 0, body_samples)
    out = _pcm_of(stem, 0, body_samples)
    assert len(src) == len(out) >= body_samples * 2 - 4
    diff = max((abs(a - b) for a, b in zip(src, out)), default=65536)
    assert diff <= 2, 'voice stem differs from confirmed narration (max lsb diff %d)' % diff


def test_long_tail_short_candidates_report_gap_instead_of_cutting(tmp_path, isolated_jobs_root):
    module = load_script()
    module.install_mocks()
    from scheduler.pipeline import Pipeline
    store, job, _ = _build_long_tail_job(module, tmp_path, short_candidates=True, root=isolated_jobs_root)
    with pytest.raises(ValueError) as exc:
        Pipeline(store, job).run()
    assert 'SHOT_COVERAGE_MISSING' in str(exc.value)
    assert 's002' in str(exc.value)


def test_cue_ticks_include_fixed_opening_offset_and_redub_shift():
    """Real finding: cues used audio coordinates and ran over the fixed opening.
    Cue ticks must be body_start + sentence timeline position - audio window start."""
    from scheduler.pipeline import Pipeline

    class Dummy:
        pass
    dummy = Dummy()
    review = {'items': [
        {'sentence_id': 's001', 'status': 'matched', 'script_text': '第一句在这里。', 'asr_text': '第一句在这里',
         'words': [{'text': '第一句', 'start_tick': 6720, 'end_tick': 48000}, {'text': '在这里', 'start_tick': 48000, 'end_tick': 87360}],
         'window': {'in_tick': 6720, 'out_tick': 87360}, 'coverage': 1.0, 'diff_type': 'none', 'decision': None},
        {'sentence_id': 's002', 'status': 'matched', 'script_text': '第二句。', 'asr_text': '第二句',
         'words': [{'text': '第二句', 'start_tick': 0, 'end_tick': 40000}],
         'window': {'in_tick': 0, 'out_tick': 40000}, 'coverage': 1.0, 'diff_type': 'none', 'decision': None},
    ]}
    effective = [
        {'sentence_id': 's001', 'in_tick': 6720, 'out_tick': 115200, 'audio_in_tick': 6720, 'audio_out_tick': 115200, 'redub': False, 'claim_ids': []},
        # s002 redubbed: own audio [0,40000), timeline shifted to 115200
        {'sentence_id': 's002', 'in_tick': 115200, 'out_tick': 155200, 'audio_in_tick': 0, 'audio_out_tick': 40000, 'redub': True, 'claim_ids': []},
    ]
    redubs = {'s002': {'sentence_id': 's002', 'text': '第二句。'}}
    cues = Pipeline._redub_cues(dummy, review, effective, redubs, body_start=215040)
    assert cues, 'cues missing'
    first = cues[0]
    assert first['start_tick'] == 215040 + 6720, first  # 221760 — over the body, not the opening
    assert all(c['start_tick'] >= 215040 for c in cues), cues
    second = [c for c in cues if c['text'].startswith('第二句')][0]
    assert second['start_tick'] == 215040 + 115200, second  # redub timeline position


def test_long_tail_trimmed_only_with_explicit_operator_choice(tmp_path, isolated_jobs_root):
    module = load_script()
    module.install_mocks()
    from scheduler.pipeline import Pipeline
    store, job, _ = _build_long_tail_job(module, tmp_path, root=isolated_jobs_root,
                                         tail_trim={'max_silence_ticks': 19200, 'reason': '操作者选择裁掉长静音尾部'})
    result = Pipeline(store, job).run()
    assert result['technical_status'] == 'PASS'
    timeline = json.loads((isolated_jobs_root / 'jobs' / job['job_id'] / 'rev-0001' / 'timeline.json').read_text(encoding='utf-8'))
    events = [e for e in timeline['audio_events'] if e['role'] == 'narration']
    words_end = (len(LONG_TAIL_SCRIPT)) * 9600  # last word end before tail
    assert events[-1]['source_out_tick'] <= words_end + 19200, events[-1]
    assert any('显式选择裁剪尾部静音' in h for h in result['quality_holds'])
