# -*- coding: utf-8 -*-
"""Focused whole-clip selection tests (USER_SCOPE_WHOLE_CLIP_SELECTION, r4).

The whole source FILE is the unit: coarse relevance, narrative ordering, dedup,
adjacent sentences may share one whole clip, no internal cuts / loops / speed
changes, no confirmed windows required. Short material is reported honestly."""
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def asset(aid, sha=None):
    return {'asset_id': aid, 'sha256': sha or hashlib.sha256(aid.encode()).hexdigest(), 'role': 'product',
            'mode': 'broll', 'analysis': {'mode': 'single_shot'}}


def ref(aid, coverage, duration, sha=None, identity=False):
    sha = sha or hashlib.sha256(aid.encode()).hexdigest()
    return {'asset_id': aid, 'source_sha256': sha, 'usable': True, 'duration_ticks': duration,
            'coverage': coverage, 'detect': None, 'product_identity_verified': identity,
            'analysis_evidence_sha256': hashlib.sha256(json.dumps(coverage).encode()).hexdigest()}


def batch(in_tick, out_tick, text, tick=None):
    return {'in_tick': in_tick, 'out_tick': out_tick, 'status': 'success',
            'observations': [{'text': text, 'frame_id': 'f', 'source_tick': tick if tick is not None else in_tick}]}


def build(assets, refs):
    from scheduler.shot_library import build_shot_library
    return build_shot_library(assets, refs, {'job_id': 'j', 'revision': 1})


def plan_wc(script_text, windows, library, replacements=None, product_terms=('火龙果',)):
    from scheduler.script_plan import build_script_plan
    from scheduler.shot_planner import plan_whole_clips
    sp = build_script_plan({'mode': 'pasted', 'script_text': script_text,
                            'script_sha256': hashlib.sha256(script_text.encode()).hexdigest()},
                           {'brief': 'b', 'claims': [], 'documents': {}, 'product_terms': list(product_terms)},
                           library)
    np_ = {'schema_version': 'narration-plan/v1',
           'source': {'audio_sha256': 'a' * 64, 'actual_duration_ticks': max(v[1] for v in windows.values()) + 19200},
           'sentences': [{'sentence_id': k, 'in_tick': v[0], 'out_tick': v[1], 'status': 'aligned',
                          'alignment_evidence_sha256': 'b' * 64} for k, v in windows.items()]}
    return plan_whole_clips(sp, np_, library,
                            {'grid_ticks': 1920, 'max_tail_pause_ticks': 19200, 'min_clip_ticks': 1920,
                             'fixed_assets': []}, whole_clip_replacements=replacements)


def real_library():
    open_src = ref('src-open', [batch(0, 240000, '双手打开火龙果包装盒，取出果肉，展示白色果肉和黑色籽粒')], 240000)
    detail_src = ref('src-detail', [batch(0, 192000, '火龙果果肉特写，白色果肉黑色籽粒切面细节')], 192000)
    street = ref('src-street', [batch(0, 96000, '城市街道上车流来往行人走过')], 96000)
    third = ref('src-third', [batch(0, 144000, '台面上的完整火龙果与切开的两半摆放展示')], 144000)
    return build([asset('src-open'), asset('src-detail'), asset('src-street'), asset('src-third')],
                 [open_src, detail_src, street, third])


SCRIPT3 = '打开包装，看看里面的果肉。果肉是白心的。籽粒是黑色的。'
WIN3 = {'s001': (0, 96000), 's002': (96000, 192000), 's003': (192000, 288000)}


def test_t1_whole_sources_selected_merged_irrelevant_excluded_no_windows_needed():
    lib = real_library()
    d = plan_wc(SCRIPT3, WIN3, lib)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert pool, 'whole-clip flow must select whole sources without any confirmed window'
    assert d['missing'] == []
    assert all(c['asset_id'] != 'src-street' for c in pool), 'clearly unrelated source must be excluded'
    # whole-file windows only: start at 0, end == grid-ceiled probe duration
    durations = {'src-open': 240000, 'src-detail': 192000, 'src-third': 144000}
    for c in pool:
        assert c['source_in_tick'] == 0
        assert c['source_out_tick'] == durations[c['asset_id']]
        assert c['source_out_tick'] - c['source_in_tick'] == c['duration_tick']
    # dedup: no source used twice
    assert len({c['asset_id'] for c in pool}) == len(pool)
    # total picture covers the narration body
    assert sum(c['duration_tick'] for c in pool) >= 288000
    # narrative order: clip covering s001 comes first
    assert 's001' in pool[0]['sentence_ids']


def test_t2_short_material_reported_not_faked():
    lib = real_library()
    only_short = build([asset('src-open'), asset('src-street')],
                       [ref('src-open', [batch(0, 96000, '双手打开火龙果包装盒取出果肉')], 96000),
                        ref('src-street', [batch(0, 48000, '街道车流')], 48000)])
    d = plan_wc(SCRIPT3, WIN3, only_short)
    assert d['missing'] and d['missing'][0]['reason'].startswith('WHOLE_CLIP_MATERIAL_SHORT')
    assert '缺' in d['missing'][0]['reason']


def test_t3_replacement_swaps_whole_source_and_reflows():
    lib = real_library()
    d1 = plan_wc(SCRIPT3, WIN3, lib)
    target = next(c for c in d1['clips'] if c['source'] == 'pool')
    before = {c['clip_id']: (c['timeline_start_tick'], c['duration_tick']) for c in d1['clips']}
    # swap the first whole clip onto the third source (relevant, unused)
    d2 = plan_wc(SCRIPT3, WIN3, lib, replacements=[
        {'clip_id': target['clip_id'], 'source_asset_id': 'src-third', 'reason': '换一条整素材'}])
    swapped = next(c for c in d2['clips'] if c['clip_id'] == target['clip_id'])
    assert swapped['asset_id'] == 'src-third'
    assert swapped['source_in_tick'] == 0 and swapped['source_out_tick'] == 144000
    # later clips shift by the duration delta; narration rows unchanged
    delta = 144000 - before[target['clip_id']][1]
    later = [c for c in d2['clips'] if c['timeline_start_tick'] > swapped['timeline_start_tick']
             and c['source'] == 'pool']
    assert all(c['timeline_start_tick'] == before[c['clip_id']][0] + delta for c in later)
    assert d2['narration'] == d1['narration']
    # a source already used elsewhere is refused
    other = next(c for c in d2['clips'] if c['source'] == 'pool' and c['clip_id'] != target['clip_id'])
    with pytest.raises(ValueError, match='ALREADY_USED'):
        plan_wc(SCRIPT3, WIN3, lib, replacements=[
            {'clip_id': target['clip_id'], 'source_asset_id': other['asset_id'], 'reason': 'x'}])
    # an unrelated source is refused outright
    with pytest.raises(ValueError, match='NOT_IN_LIBRARY'):
        plan_wc(SCRIPT3, WIN3, lib, replacements=[
            {'clip_id': target['clip_id'], 'source_asset_id': 'src-street', 'reason': 'x'}])


def test_t4_adjacent_sentences_share_one_whole_clip():
    lib = real_library()
    d = plan_wc('看看这个火龙果。外观是这样的。', {'s001': (0, 96000), 's002': (96000, 192000)}, lib)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    # both generic sentences share ONE whole source; a coverage-fill extra may
    # follow (combination), but never a second per-sentence anchor
    anchors = [c for c in pool if c['sentence_ids']]
    assert len(anchors) == 1 and anchors[0]['sentence_ids'] == ['s001', 's002']
    assert sum(c['duration_tick'] for c in pool) >= 192000 and d['missing'] == []


# ---- r6 repair behaviors (WHOLE_CLIP_REVIEW_REPAIR_2) ----

def test_r6_explicit_product_terms_reject_similar_different_product():
    for script, desc in [('火龙果果肉里布满黑色籽粒。', '西瓜果肉里布满黑色籽粒特写'),
                         ('这款火龙果放在白色盘子里展示。', '耳机放在白色盘子里展示')]:
        lib = build([asset('unrelated')], [ref('unrelated', [batch(0, 144000, desc)], 144000)])
        d = plan_wc(script, {'s001': (0, 96000)}, lib)
        assert [c for c in d['clips'] if c['source'] == 'pool'] == [], desc
        assert d['missing'][0]['reason'].startswith('WHOLE_CLIP_PRODUCT_NOT_SHOWN')


def test_r6_matching_product_still_selects_without_action_windows():
    lib = build([asset('fruit')], [ref('fruit', [batch(0, 144000, '西瓜果肉里布满黑色籽粒特写')], 144000)])
    d = plan_wc('火龙果果肉里布满黑色籽粒。', {'s001': (0, 96000)}, lib)
    # description similarity alone never substitutes; term not shown -> rejected
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    lib2 = build([asset('fruit')], [ref('fruit', [batch(0, 144000, '火龙果果肉黑色籽粒特写')], 144000)])
    d2 = plan_wc('火龙果果肉里布满黑色籽粒。', {'s001': (0, 96000)}, lib2)
    assert [c for c in d2['clips'] if c['source'] == 'pool'] and d2['missing'] == []


def test_r6_identity_verified_source_admits_without_term_text():
    lib = build([asset('fruit')], [ref('fruit', [batch(0, 144000, '果肉特写')], 144000, identity=True)])
    d = plan_wc('火龙果果肉里布满黑色籽粒。', {'s001': (0, 96000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] and d['missing'] == []


def test_r6_whole_clip_replacement_refuses_different_product():
    # the swap endpoint resolves candidate units through the same
    # _whole_source_units gate, so a similar-but-different product source is
    # not even in the unit map -> NOT_IN_LIBRARY refusal
    from scheduler.shot_planner import plan_whole_clips
    lib = build([asset('fruit'), asset('other')],
                [ref('fruit', [batch(0, 144000, '火龙果果肉黑色籽粒特写')], 144000),
                 ref('other', [batch(0, 144000, '西瓜果肉黑色籽粒特写')], 144000)])
    with pytest.raises(ValueError, match='WHOLE_CLIP_REPLACEMENT_SOURCE_NOT_IN_LIBRARY'):
        plan_wc('火龙果果肉。', {'s001': (0, 96000)}, lib, replacements=[
            {'clip_id': 'clip-wc-01', 'source_asset_id': 'other', 'reason': '换'}])




# ---- r5 repair behaviors (WHOLE_CLIP_REVIEW_REPAIR_1) ----

def test_r5_longer_relevant_whole_clip_preferred_for_longer_narration():
    refs = [ref('short', [batch(0, 96000, '火龙果果肉特写')], 96000),
            ref('long', [batch(0, 288000, '火龙果果肉特写')], 288000)]
    lib = build([asset('short'), asset('long')], refs)
    d = plan_wc('看看火龙果果肉。', {'s001': (0, 240000)}, lib)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert d['missing'] == [], '5s narration with a 6s relevant whole source is NOT short'
    assert [c['asset_id'] for c in pool] == ['long']


def test_r5_combination_of_whole_clips_covers_long_narration():
    refs = [ref(aid, [batch(0, 144000, '火龙果果肉特写')], 144000) for aid in ('fruit-a', 'fruit-b', 'fruit-c')]
    lib = build([asset(a) for a in ('fruit-a', 'fruit-b', 'fruit-c')], refs)
    d = plan_wc('看看火龙果果肉。', {'s001': (0, 384000)}, lib)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert d['missing'] == [], '3x3s relevant whole sources cover an 8s narration'
    assert len(pool) == 3 and sum(c['duration_tick'] for c in pool) >= 384000
    assert all(c['source_in_tick'] == 0 for c in pool)  # whole files, no cuts


def test_r5_real_shortage_still_reported_honestly():
    lib = build([asset('only'), asset('street')],
                [ref('only', [batch(0, 96000, '火龙果果肉特写')], 96000),
                 ref('street', [batch(0, 48000, '街道车流')], 48000)])
    d = plan_wc('看看火龙果果肉。', {'s001': (0, 384000)}, lib)
    assert d['missing'] and d['missing'][0]['reason'].startswith('WHOLE_CLIP_MATERIAL_SHORT')
    # only the 2s relevant source counts; the unrelated street never fills
    assert '6.0' in d['missing'][0]['reason']
    assert [c for c in d['clips'] if c['source'] == 'pool' and c['asset_id'] == 'street'] == []


def test_r5_empty_product_terms_do_not_admit_clearly_unrelated():
    lib = build([asset('street')], [ref('street', [batch(0, 144000, '城市街道车流行人')], 144000)])
    d = plan_wc('看看火龙果果肉。', {'s001': (0, 96000)}, lib, product_terms=())
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'].startswith('WHOLE_CLIP_RELEVANCE_BASIS_MISSING')


def test_r5_whole_clip_replacement_revision_invalidates_render_not_audio():
    import copy as _copy
    from scheduler.revision_plan import plan_revision
    old = {'assets': [], 'intent': {'selection_mode': 'whole_clip'},
           'script_input': {'mode': 'pasted'}, 'narration_input': {'mode': 'uploaded'},
           'templates': {}, 'overrides': {}, 'v2_overrides': {}}
    new = _copy.deepcopy(old)
    new['v2_overrides']['whole_clip_replacements'] = [
        {'clip_id': 'clip-wc-01', 'source_asset_id': 'long', 'reason': '换一条'}]
    plan = plan_revision(old, new, {})
    assert 'edit.plan' in plan['invalidated'] and 'video.chunk' in plan['invalidated']
    assert {'mix.raw', 'mix.loudnorm', 'mux.final', 'picture.master'} <= set(plan['invalidated'])
    assert 'speech.synthesize' in plan['reuse'] and 'video.analyze' in plan['reuse']
    new2 = _copy.deepcopy(old)
    new2['intent']['selection_mode'] = 'precise'
    plan2 = plan_revision(old, new2, {})
    assert 'edit.plan' in plan2['invalidated'] and 'speech.synthesize' in plan2['reuse']


def test_t5_precise_mode_still_requires_confirmed_evidence_for_actions():
    lib = real_library()
    from scheduler.shot_planner import plan_shots
    from scheduler.script_plan import build_script_plan
    sp = build_script_plan({'mode': 'pasted', 'script_text': '打开包装，看看火龙果。',
                            'script_sha256': hashlib.sha256('打开包装，看看火龙果。'.encode()).hexdigest()},
                           {'brief': 'b', 'claims': [], 'documents': {}, 'product_terms': ['火龙果']}, lib)
    np_ = {'schema_version': 'narration-plan/v1',
           'source': {'audio_sha256': 'a' * 64, 'actual_duration_ticks': 115200},
           'sentences': [{'sentence_id': 's001', 'in_tick': 0, 'out_tick': 96000, 'status': 'aligned',
                          'alignment_evidence_sha256': 'b' * 64}]}
    d = plan_shots(sp, np_, lib, {'grid_ticks': 1920, 'max_tail_pause_ticks': 19200, 'min_clip_ticks': 1920})
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'
    # whole-clip mode has no such requirement
    dwc = plan_wc('打开包装，看看火龙果。', {'s001': (0, 96000)}, lib)
    assert [c for c in dwc['clips'] if c['source'] == 'pool'] and dwc['missing'] == []


def test_t6_whole_clip_pipeline_end_to_end_uploaded_narration_tts_zero(tmp_path):
    """Full v2 pipeline in whole_clip mode with the mock transport: whole-file
    windows only, irrelevant source excluded, uploaded narration synthesizes
    nothing, render PASS."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('acc', ROOT / 'scripts/acceptance-orchestration-v2.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_mocks()
    import tempfile
    from scheduler.db import Store
    from providers.gateway import Gateway
    from scheduler.service import create_app
    tmp = Path(tempfile.mkdtemp(prefix='wc-e2e-'))
    store = Store(tmp / 'jobs.sqlite')
    app = create_app(store, Gateway(store), 'operator-test', run_queue=False)
    app.state.queue.thread.start()
    client = module.AsgiClient(app)
    h = {'Authorization': 'Bearer operator-test'}

    def upload(path, kind='video'):
        r = client.post('/v1/imports?filename=' + path.name + '&kind=' + kind, headers=h, raw_body=path.read_bytes())
        assert r.status_code == 200, r.content
        return r.json()

    body = module.make_fixture_video(tmp / 'body.mp4', 'Teal', 6)
    extra = module.make_fixture_video(tmp / 'extra.mp4', 'Sienna', 4)
    street = module.make_fixture_video(tmp / 'street.mp4', 'SlateGray', 3)
    narr = module.make_fixture_narration(tmp / 'narr.wav', 5)
    module.MOCK_ANALYSIS[module.digest(body) if hasattr(module, 'digest') else __import__('workers.common', fromlist=['digest']).digest(body)] = [
        {'in_tick': 0, 'out_tick': 288000, 'text': '双手打开火龙果包装盒，展示白色果肉黑色籽粒'}]
    from workers.common import digest
    module.MOCK_ANALYSIS[digest(extra)] = [{'in_tick': 0, 'out_tick': 192000, 'text': '火龙果果肉切面特写细节'}]
    module.MOCK_ANALYSIS[digest(street)] = [{'in_tick': 0, 'out_tick': 144000, 'text': '城市街道车流行人'}]
    script_text = '打开包装，看看里面的果肉。果肉是白心的。'
    module.MOCK_ASR[digest(narr)] = module.asr_words_for(script_text, 5 * 48000)

    up_body, up_extra, up_street, up_narr = upload(body), upload(extra), upload(street), upload(narr, 'narration')
    client.post('/v1/color-previews', {'decision': 'allow_preview_bt709',
                                       'source_hashes': [up_body['sha256'], up_extra['sha256'], up_street['sha256']]}, headers=h)
    job = client.get('/v2/job-template?account=laoX', headers=h).json()
    job_id = 'wc-e2e-' + __import__('uuid').uuid4().hex[:6]
    job['job_id'] = job_id
    job['intent']['brief'] = '火龙果画面介绍'
    job['intent']['product_terms'] = ['火龙果']
    job['intent']['selection_mode'] = 'whole_clip'
    job['intent']['target_duration_ticks'] = 1920 * 60
    job['script_input'] = {'mode': 'pasted', 'script_text': script_text,
                           'script_sha256': hashlib.sha256(script_text.encode()).hexdigest(), 'generation_note': None}
    job['narration_input'] = {'mode': 'uploaded', 'preset': None,
                              'uploaded': {'path': up_narr['path'], 'sha256': up_narr['sha256']}, 'history': None}
    job['assets'] = [
        {'asset_id': 'a-street', 'path': up_street['path'], 'sha256': up_street['sha256'], 'mode': 'broll',
         'role': 'product', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_street['provenance_ref'], 'locked': None},
        {'asset_id': 'a-body', 'path': up_body['path'], 'sha256': up_body['sha256'], 'mode': 'broll',
         'role': 'product', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_body['provenance_ref'], 'locked': None},
        {'asset_id': 'a-extra', 'path': up_extra['path'], 'sha256': up_extra['sha256'], 'mode': 'broll',
         'role': 'product', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_extra['provenance_ref'], 'locked': None},
    ]
    r = client.post('/v2/jobs', job, {**h, 'Idempotency-Key': job_id})
    assert r.status_code == 202, r.content

    deadline = __import__('time').time() + 240
    state = None
    while __import__('time').time() < deadline:
        state = client.get(f'/v1/jobs/{job_id}?revision=1', headers=h).json()
        if state['state'] in {'AWAIT_SCRIPT_CONFIRM', 'FAILED_FINAL'}:
            break
        __import__('time').sleep(0.4)
    assert state['state'] == 'AWAIT_SCRIPT_CONFIRM', state.get('errors')
    client.post(f'/v2/jobs/{job_id}/confirm-script', {'revision': 1, 'operator_id': 'wc-e2e'}, headers=h)
    while __import__('time').time() < deadline:
        state = client.get(f'/v1/jobs/{job_id}?revision=1', headers=h).json()
        if state['state'] in {'AWAIT_NARRATION_CONFIRM', 'FAILED_FINAL'}:
            break
        __import__('time').sleep(0.4)
    assert state['state'] == 'AWAIT_NARRATION_CONFIRM', state.get('errors')
    assert module.CALLS['speech.synthesize'] == 0, 'uploaded narration must synthesize nothing'
    client.post(f'/v2/jobs/{job_id}/confirm-narration', {'revision': 1, 'operator_id': 'wc-e2e'}, headers=h)
    while __import__('time').time() < deadline:
        state = client.get(f'/v1/jobs/{job_id}?revision=1', headers=h).json()
        if state['state'] in {'AWAIT_SUBTITLE_REVIEW', 'REVIEW_REQUIRED', 'FAILED_FINAL', 'AWAIT_SHOT_GAP'}:
            break
        __import__('time').sleep(0.4)
    if state['state'] == 'AWAIT_SUBTITLE_REVIEW':
        data = client.get(f'/v2/jobs/{job_id}/review-data?revision=1', headers=h).json()
        for item in data['subtitle_review']['items']:
            if item['status'] == 'matched':
                continue
            rr = client.post(f'/v2/jobs/{job_id}/subtitle-decisions',
                             {'revision': 1, 'sentence_id': item['sentence_id'], 'choice': 'asr_error_confirmed',
                              'notes': 'fixture', 'operator_id': 'wc-e2e'}, headers=h)
            assert rr.status_code == 200, rr.content
        while __import__('time').time() < deadline:
            state = client.get(f'/v1/jobs/{job_id}?revision=1', headers=h).json()
            if state['state'] in {'REVIEW_REQUIRED', 'FAILED_FINAL', 'AWAIT_SHOT_GAP'}:
                break
            __import__('time').sleep(0.4)
    assert state['state'] == 'REVIEW_REQUIRED', state.get('errors')
    result = state['result']
    assert result['technical_status'] == 'PASS', result.get('qa')
    timeline = state['timeline']
    pool = [c for c in timeline['video_clips'] if c['mode'] == 'broll']
    assert pool, 'whole-clip selection must place whole sources'
    assert all(c['base_source_ref'] != 'a-street' for c in pool), 'irrelevant whole source excluded'
    probe_lengths = {'a-body': 288000, 'a-extra': 192000}
    for c in pool:
        assert c['source_in_tick'] == 0
        assert c['source_out_tick'] == probe_lengths[c['base_source_ref']]
        assert c['source_out_tick'] - c['source_in_tick'] == c['duration_tick'], 'whole-file window, no cuts'
    assert module.CALLS['speech.synthesize'] == 0, 'no synthesis anywhere in the whole-clip flow'
    app.state.queue.stop.set()
