"""Counterexamples for per-sentence shot planning and timeline compilation (S5)."""
import hashlib, json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def script_plan(*sents):
    text = ''.join(sents)
    return {'schema_version': 'script-plan/v1', 'job_id': 'j', 'revision': 1, 'script_text': text,
            'script_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'sentences': [{'sentence_id': 's%03d' % (i + 1), 'text': s, 'claim_ids': [], 'visual': vis(s)}
                          for i, s in enumerate(sents)]}


def vis(text):
    from scheduler.script_plan import classify_visual
    return classify_visual(text)


def narration_plan(windows, duration=None):
    end = max((w[1] for w in windows.values()), default=48000)
    return {'schema_version': 'narration-plan/v1', 'source': {'audio_sha256': 'a' * 64, 'actual_duration_ticks': duration or (end + 19200)},
            'sentences': [{'sentence_id': k, 'in_tick': v[0], 'out_tick': v[1], 'status': 'aligned' if v else 'unknown',
                           'alignment_evidence_sha256': 'b' * 64 if v else None} for k, v in windows.items()]}


def cand(aid, use, in_tick, out_tick, text, sha=None, identity=False, usable=True, obs_texts=None):
    sha = sha or hashlib.sha256(aid.encode()).hexdigest()
    from scheduler.shot_semantics import supported_actions_for
    texts = list(obs_texts or [text])
    ticks = [in_tick + i * 48000 for i in range(len(texts))]
    return {'candidate_id': 'cand-' + hashlib.sha256(('%s:%d:%d' % (sha, in_tick, out_tick)).encode()).hexdigest()[:32],
            'asset_id': aid, 'source_asset_id': aid, 'source_sha256': sha, 'in_tick': in_tick, 'out_tick': out_tick,
            'use': use, 'supported_actions': supported_actions_for(ticks, texts, in_tick, out_tick),
            'action_text': text, 'product_description': text,
            'product_identity_verified': identity, 'occlusion': 'unknown', 'clarity': 'unknown',
            'speech_locked': False,
            'evidence': {'analysis_evidence_sha256': 'e' * 64, 'frame_count': max(1, len(texts)),
                         'observation_count': len(texts), 'observation_texts': texts,
                         'observation_ticks': ticks, 'sampling_interval_ticks': 48000,
                         'per_observation': [{'tick': ticks[i], 'actions': [], 'display': 'other'}
                                             for i in range(len(texts))]},
            'fixed_role': None, 'usable': usable, 'unusable_reason': None if usable else 'ANALYSIS_UNUSABLE'}


def library(*cands):
    from scheduler.contracts import canonical_hash
    from scheduler.shot_semantics import RULE_VERSION
    return {'schema_version': 'shot-library/v1',
            'library_sha256': canonical_hash({'candidates': [c['candidate_id'] for c in cands]}),
            'config': {'use_rule_version': RULE_VERSION, 'min_candidate_ticks': 24000},
            'candidates': sorted(cands, key=lambda c: (c['source_sha256'], c['in_tick'])), 'failures': []}


def reusable_interval(cand_or_sha, kind, obj, in_tick, out_tick):
    sha = cand_or_sha['source_sha256'] if isinstance(cand_or_sha, dict) else cand_or_sha
    return {'source_sha256': sha, 'action_kind': kind, 'action_object': obj,
            'in_tick': in_tick, 'out_tick': out_tick, 'operator_id': 'fixture-reviewed',
            'basis': 'fixture simulates the post-review state', 'origin': {'kind': 'fixture'}}


CONSTRAINTS = {'grid_ticks': 1920, 'max_tail_pause_ticks': 19200, 'min_clip_ticks': 1920}


def test_shuffled_irrelevant_inputs_still_pick_right_shots():
    # 4.2s opening sentence; pool contains an irrelevant street shot and a packaging shot.
    # r3: the action shot is served from an operator-CONFIRMED interval (fixture
    # simulates the post-review state); inference alone never selects.
    open_box = cand('src-open', 'open_box', 0, 240000, '双手打开包装盒取出火龙果果实')
    street = cand('src-street', 'other', 0, 240000, '城市街道上车流来往')
    pack = cand('src-pack', 'packaging', 0, 240000, '红色包装盒正面特写标签')
    order_a = library(open_box, street, pack)
    order_b = library(pack, open_box, street)
    sp = script_plan('打开包装，看看里面的果实。')
    np_ = narration_plan({'s001': (0, 201600)})  # 4.2s
    reusable = [reusable_interval(open_box, 'open_packaging', 'packaging', 0, 240000)]
    from scheduler.shot_planner import plan_shots
    a = plan_shots(sp, np_, order_a, CONSTRAINTS, reusable_intervals=reusable)
    b = plan_shots(sp, np_, order_b, CONSTRAINTS, reusable_intervals=reusable)
    assert [c['candidate_id'] for c in a['clips'] if c['source'] == 'pool'] == \
           [c['candidate_id'] for c in b['clips'] if c['source'] == 'pool']
    assert all(c['asset_id'] != 'src-street' for c in a['clips'])
    assert a['missing'] == []
    assert a['clips'][0]['match_evidence'].get('action_evidence') == 'confirmed_interval'


def test_one_sentence_two_relevant_shots_when_window_too_short():
    short1 = cand('s1', 'open_box', 0, 96000, '双手撕开包装袋')
    short2 = cand('s2', 'detail', 0, 96000, '火龙果果肉切面特写籽粒')
    sp = script_plan('打开包装，看看里面的果肉切面。')
    np_ = narration_plan({'s001': (0, 192000)})
    reusable = [reusable_interval(short1, 'open_packaging', 'packaging', 0, 96000)]
    from scheduler.shot_planner import plan_shots
    d = plan_shots(sp, np_, library(short1, short2), CONSTRAINTS, reusable_intervals=reusable)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert len(pool) == 2
    assert {c['asset_id'] for c in pool} == {'s1', 's2'}
    assert pool[0]['duration_tick'] % 1920 == 0 and pool[1]['duration_tick'] % 1920 == 0
    assert pool[0]['timeline_start_tick'] + pool[0]['duration_tick'] == pool[1]['timeline_start_tick']
    assert pool[0]['source_out_tick'] <= 96000 and pool[1]['source_out_tick'] <= 96000


def test_missing_shot_reports_sentence_and_requirement():
    sp = script_plan('打开包装，看看里面。', '果肉看起来很新鲜。')
    np_ = narration_plan({'s001': (0, 96000), 's002': (96000, 192000)})
    only_detail = library(cand('d', 'detail', 0, 240000, '火龙果果肉切面特写新鲜'))
    from scheduler.shot_planner import plan_shots
    d = plan_shots(sp, np_, only_detail, CONSTRAINTS)
    assert [m['sentence_id'] for m in d['missing']] == ['s001']
    assert 'open_packaging' in d['missing'][0]['requirement']


def test_source_window_reuse_forbidden_and_overlap_exception_needs_reason():
    same = cand('src-x', 'packaging', 0, 240000, '火龙果包装盒正面外观展示')
    sp = script_plan('这是火龙果包装的正面。', '火龙果包装的侧面也一样。')
    np_ = narration_plan({'s001': (0, 96000), 's002': (96000, 192000)})
    other = cand('src-y', 'packaging', 0, 240000, '火龙果包装盒侧面外观展示')
    from scheduler.shot_planner import plan_shots
    d = plan_shots(sp, np_, library(same, other), CONSTRAINTS)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert len({c['asset_id'] for c in pool}) == len(pool)
    windows = [(c['source_in_tick'], c['source_out_tick']) for c in pool]
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            if pool[i]['source_sha256'] == pool[j]['source_sha256']:
                assert windows[i][1] <= windows[j][0] or windows[j][1] <= windows[i][0]
    assert all(c.get('overlap_exception') is None for c in pool)


def test_locked_and_full_source_windows_never_free_cut():
    # A fixed asset passed as a pool candidate (data corruption) is refused, and a
    # fixed clip may not be trimmed to a partial window during compilation.
    from scheduler.shot_planner import plan_shots, compile_edit_decision
    sp = script_plan('打开包装，看看里面。')
    np_ = narration_plan({'s001': (0, 96000)})
    rogue = cand('f', 'open_box', 0, 240000, '口播画面')
    rogue['fixed_role'] = 'fixed_opening'
    with pytest.raises(ValueError):
        plan_shots(sp, np_, library(rogue), CONSTRAINTS)
    decision = {'schema_version': 'edit-decision/v1', 'job_id': 'j', 'revision': 1,
                'inputs': {}, 'clips': [{'clip_id': 'clip-fixed', 'source': 'fixed', 'fixed_role': 'fixed_opening',
                                         'asset_id': 'fx', 'source_sha256': hashlib.sha256(b'fx').hexdigest(),
                                         'source_in_tick': 19200, 'source_out_tick': 96000,
                                         'timeline_start_tick': 0, 'duration_tick': 76800,
                                         'sentence_ids': [], 'match_reason': 'fixed', 'overlap_exception': None}],
                'missing': [], 'tail_pause_ticks': 0,
                'narration': {'audio_sha256': 'a' * 64, 'body_start_tick': 0, 'sentences': []}}
    resources = {'sources': {'fx': {'asset_id': 'fx', 'path': 'F:/x/fx.mp4', 'sha256': hashlib.sha256(b'fx').hexdigest(),
                                    'probe': {'duration_ticks': 96000, 'stream_indices': {'video': 0, 'audio': 0}},
                                    'visual_evidence': {'path': 'F:/x/fx-a.json', 'sha256': hashlib.sha256(b'e').hexdigest()},
                                    'locked': {'audio_stream_index': 0, 'boundary': {'path': 'F:/x/fx-b.json', 'sha256': hashlib.sha256(b'bd').hexdigest(),
                                                                                      'in_tick': 0, 'out_tick': 96000}, 'transcript': 'x'}}},
                 'narration': {'path': 'F:/x/v.wav', 'sha256': 'a' * 64,
                               'probe': {'duration_ticks': 96000, 'stream_indices': {'audio': 0}}},
                 'alignment': {'path': 'F:/x/al.json', 'sha256': 'b' * 64},
                 'voice_stem': {'path': 'F:/x/st.wav', 'sha256': 'c' * 64,
                                'probe': {'duration_ticks': 96000, 'stream_indices': {'audio': 0}}},
                 'subtitle_template': {'template_id': 'subtitle-bottom', 'version': '1.0.0'},
                 'cues': []}
    with pytest.raises(ValueError):
        compile_edit_decision(decision, resources)  # partial window 19200..96000 != full source


def test_compile_edit_decision_produces_valid_timeline_v1():
    from scheduler.shot_planner import plan_shots, compile_edit_decision
    sp = script_plan('打开包装，看看里面。', '果肉很新鲜。')
    np_ = narration_plan({'s001': (0, 96000), 's002': (96000, 192000)})
    open_cand = cand('a', 'open_box', 0, 240000, '打开包装取出果实')
    reusable = [reusable_interval(open_cand, 'open_packaging', 'packaging', 0, 96000)]
    d = plan_shots(sp, np_, library(open_cand, cand('b', 'detail', 0, 240000, '果实特写新鲜')),
                   CONSTRAINTS, reusable_intervals=reusable)
    resources = {
        'sources': {
            'a': {'asset_id': 'a', 'path': 'F:/x/a.mp4', 'sha256': hashlib.sha256(b'a').hexdigest(),
                  'probe': {'duration_ticks': 240000, 'stream_indices': {'video': 0, 'audio': None}},
                  'visual_evidence': {'path': 'F:/x/a-analysis.json', 'sha256': hashlib.sha256(b'ea').hexdigest()}},
            'b': {'asset_id': 'b', 'path': 'F:/x/b.mp4', 'sha256': hashlib.sha256(b'b').hexdigest(),
                  'probe': {'duration_ticks': 240000, 'stream_indices': {'video': 0, 'audio': None}},
                  'visual_evidence': {'path': 'F:/x/b-analysis.json', 'sha256': hashlib.sha256(b'eb').hexdigest()}},
        },
        'narration': {'path': 'F:/x/voice.wav', 'sha256': 'a' * 64,
                      'probe': {'duration_ticks': 240000, 'stream_indices': {'audio': 0}}},
        'alignment': {'path': 'F:/x/align.json', 'sha256': 'b' * 64},
        'voice_stem': {'path': 'F:/x/stem.wav', 'sha256': 'c' * 64,
                       'probe': {'duration_ticks': 240000, 'stream_indices': {'audio': 0}}},
        'subtitle_template': {'template_id': 'subtitle-bottom', 'version': '1.0.0'},
        'cues': [{'cue_id': 'cue-1', 'start_tick': 0, 'end_tick': 96000, 'text': '打开包装，看看里面。', 'claim_ids': []}],
    }
    t = compile_edit_decision(d, resources)
    assert t['schema_version'] == 'timeline/v1'
    from scheduler.semantic import validate_timeline
    validate_timeline(t, None, verify_files=False)
    assert t['duration_ticks'] % 1920 == 0
    narration_events = [e for e in t['audio_events'] if e['role'] == 'narration']
    # contiguous sentence windows merge into one event: a 1:1 copy of the confirmed audio
    assert len(narration_events) == 1
    assert narration_events[0]['source_in_tick'] == 0 and narration_events[0]['source_out_tick'] == 192000
    assert t['duration_ticks'] >= 192000
