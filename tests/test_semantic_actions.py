# -*- coding: utf-8 -*-
"""Focused semantic-action constraints (r3, approved by REDIAGNOSIS_PLAN_REVIEW.md).

Negatives replay the REAL job edit-1791685920586/rev-0002 observation literals
(read-only copies; the job's own sidecars are never rewritten). Temporal rules:
an action window is auto-selectable ONLY from an attested run — >=2 adjacent
observation ticks each independently attesting the same (kind, object,
in-progress), optionally end-bounded by an adjacent completion observation.
A lone attesting tick never blesses a span (attested_tick, insufficient).
"""
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ---- real observation literals (jobs/edit-1791685920586/rev-0002/shot-library.json) ----
SHOT2_B1 = '场景为带有模糊棕褐色木质背景的台面，台面上摆放着对半切开的白心火龙果、对半切开的红心火龙果，背景处还露出部分完整火龙果；画面左下角有一只手掌五指分开、掌心朝向上方，位于白心火龙果的前方位置。'
SHOT2_B2 = '场景与摆放的火龙果无变化，画面左下角的手掌位置略微下移，整体仍停留在白心火龙果的前方区域。'
SHOT2_B3 = '场景和火龙果的展示状态保持不变，手掌继续向下小幅移动，逐渐离开原本在台面上方的相对靠上位置。'
SHOT2_B4 = '画面中的手掌完全移出画面，完整展示出白心火龙果、红心火龙果的截面以及它们带肉质鳞片的外皮，火龙果静置在台面上，背景和其他火龙果的部分也保持原样。'
SHOT2_B5 = '画面中展示了两块对半切开的火龙果，左侧为白心火龙果，白色果肉上分布着大量黑色小籽，外皮为红色并带有红绿渐变的肉质鳞片；右侧为红心火龙果，紫红色果肉上同样分布着大量黑色小籽，二者放置在带有蓝灰纹理的台面上，背景是模糊的棕色调区域。'
SHOT2_B6 = '画面仍展示两块对半切开的火龙果，整体布局、物体与背景环境和前一帧基本一致，仅画面的取景范围相较前一帧有所收窄，火龙果的果肉纹理细节呈现得更为突出。'
SHOT1_KNIFE = '场景为带有斑驳纹理的深色台面，背景是模糊的棕色调环境，一双手分别握住完整火龙果的两侧，火龙果正中间被竖直刀刃贯穿，火龙果外皮为玫红色，带有绿红渐变的肉质鳞片。'
SHOT1_SPLIT1 = '双手向两侧微微用力，将火龙果顺着中间的切口分开，露出内部白色的果肉，果肉表面分布着大量黑色细小籽。'
SHOT1_SPLIT2 = '双手继续向两侧挪动，将火龙果的两半完全分开，展示出两半火龙果完整的横截面，白心黑籽的果肉完全呈现。'
SHOT1_PLACED = '双手从画面两侧移出，两半火龙果平稳放置在台面上，完整露出两半火龙果的全部内部结构与外层果皮。'
SHOT1_SHOW1 = '画面展示两块对半切开的白心火龙果，果皮为玫红色，果肉呈白色且分布大量黑色小籽，火龙果放置在带斑驳痕迹的蓝绿色台面上，背景是模糊的棕色调区域，后方还隐约可见其他火龙果的局部，左侧火龙果的顶部绿色鳞片清晰完整。'
SHOT1_SHOW2 = '画面里的两块切开的白心火龙果位置略有微调，左侧火龙果顶部的部分绿色鳞片移出了画面左边缘，火龙果的果肉、果皮细节与背景环境保持可见状态。'
SHOT1_DRIFT = '画面中的两块切开的白心火龙果位置继续小幅变动，火龙果展示出的内部果肉和黑色籽分布状态无明显变化，整体场景环境保持一致。'

SHA2 = hashlib.sha256(b'real-shot-2').hexdigest()
SHA1 = hashlib.sha256(b'real-shot-1').hexdigest()


def obs(text, tick):
    return {'text': text, 'frame_id': 'f%d' % tick, 'source_tick': tick}


def batch(in_tick, out_tick, *observations):
    return {'in_tick': in_tick, 'out_tick': out_tick, 'status': 'success',
            'observations': list(observations)}


def real_refs():
    """Real coverage shapes from rev-0002 (shot-1 = splitting source, shot-2 = display source)."""
    return [
        {'asset_id': 'shot-1', 'source_sha256': SHA1, 'usable': True,
         'duration_ticks': 316416, 'detect': None, 'product_identity_verified': False,
         'analysis_evidence_sha256': '1' * 64,
         'coverage': [
             batch(0, 48000, obs(SHOT1_KNIFE, 0)),
             batch(48000, 110000, obs(SHOT1_SPLIT1, 48000), obs(SHOT1_SPLIT2, 96000)),
             batch(110000, 264000, obs(SHOT1_PLACED, 144000), obs(SHOT1_SHOW1, 192000), obs(SHOT1_SHOW2, 240000)),
             batch(264000, 301008, obs(SHOT1_DRIFT, 288000)),
         ]},
        {'asset_id': 'shot-2', 'source_sha256': SHA2, 'usable': True,
         'duration_ticks': 248832, 'detect': None, 'product_identity_verified': False,
         'analysis_evidence_sha256': '2' * 64,
         'coverage': [
             batch(0, 120000, obs(SHOT2_B1, 0), obs(SHOT2_B2, 48000), obs(SHOT2_B3, 96000)),
             batch(120000, 192000, obs(SHOT2_B4, 144000)),
             batch(192000, 248000, obs(SHOT2_B5, 192000), obs(SHOT2_B6, 246000)),
         ]},
    ]


def real_assets():
    return [{'asset_id': 'shot-1', 'sha256': SHA1, 'role': 'product', 'mode': 'broll',
             'analysis': {'mode': 'single_shot'}},
            {'asset_id': 'shot-2', 'sha256': SHA2, 'role': 'product', 'mode': 'broll',
             'analysis': {'mode': 'single_shot'}}]


def build_real_library():
    from scheduler.shot_library import build_shot_library
    return build_shot_library(real_assets(), real_refs(), {'job_id': 'j', 'revision': 1})


def plan(script_text, windows, library, confirmed=None, product_terms=('火龙果',), reusable=None):
    from scheduler.script_plan import build_script_plan
    from scheduler.shot_planner import plan_shots
    sp = build_script_plan({'mode': 'pasted', 'script_text': script_text,
                            'script_sha256': hashlib.sha256(script_text.encode()).hexdigest()},
                           {'brief': '火龙果画面介绍', 'claims': [], 'documents': {},
                            'product_terms': list(product_terms)}, library)
    np_ = {'schema_version': 'narration-plan/v1',
           'source': {'audio_sha256': 'a' * 64, 'actual_duration_ticks': max(w[1] for w in windows.values()) + 19200},
           'sentences': [{'sentence_id': k, 'in_tick': v[0], 'out_tick': v[1], 'status': 'aligned',
                          'alignment_evidence_sha256': 'b' * 64} for k, v in windows.items()]}
    return plan_shots(sp, np_, library, {'grid_ticks': 1920, 'max_tail_pause_ticks': 19200,
                                         'min_clip_ticks': 1920},
                      confirmed_windows=confirmed, reusable_intervals=reusable)


def confirmed_interval(sha, kind, obj, in_tick, out_tick, basis='夹具模拟的已审核区间（operator_reviewed）'):
    """Fixture simulating the state AFTER a real operator interval review."""
    return {'source_sha256': sha, 'action_kind': kind, 'action_object': obj,
            'in_tick': in_tick, 'out_tick': out_tick, 'operator_id': 'operator-reviewed',
            'basis': basis, 'origin': {'kind': 'fixture'}}


# ---- N1: the real wrong pick must be impossible; s001 must report missing packaging ----

def test_n1_real_cut_display_hand_movement_never_supports_open_packaging():
    lib = build_real_library()
    for c in lib['candidates']:
        for a in c.get('supported_actions', []):
            assert a['kind'] != 'open_packaging', c['candidate_id']
    d = plan('打开包装，看看火龙果。', {'s001': (0, 115200)}, lib)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert pool == [], 's001 must not auto-match any window: the library has no packaging footage'
    assert d['missing'] and d['missing'][0]['sentence_id'] == 's001'
    assert d['missing'][0]['reason'] == 'NO_RELATED_CANDIDATE_FOR_REQUIRED_USE'
    assert 'open_packaging' in d['missing'][0]['requirement']


def test_n1b_per_observation_and_aggregate_reject_the_fabricated_action():
    from scheduler.shot_semantics import parse_observation
    for text in (SHOT2_B1, SHOT2_B2, SHOT2_B3, SHOT2_B4, SHOT2_B5, SHOT2_B6):
        p = parse_observation(text)
        assert not any(a['state'] == 'in_progress' for a in p['actions']), text[:30]
        assert not any(a['kind'] == 'open_packaging' for a in p['actions']), text[:30]
    # merged-window aggregation may never conjure what no single observation attests
    lib = build_real_library()
    joined = next(c for c in lib['candidates'] if c['source_sha256'] == SHA2 and c['in_tick'] == 0)
    assert joined.get('supported_actions') in (None, [])


# ---- N2: splitting fruit is not opening its packaging ----

def test_n2_split_fruit_run_does_not_serve_open_packaging_sentence():
    lib = build_real_library()
    d = plan('打开火龙果包装盒。', {'s001': (0, 96000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'NO_RELATED_CANDIDATE_FOR_REQUIRED_USE'


# ---- N3: sparse 0s/8s/18s observations cannot bless 4-6s or claim validation ----

def sparse_library():
    from scheduler.shot_library import build_shot_library
    sha = hashlib.sha256(b'mixed-source').hexdigest()
    asset = {'asset_id': 'mixed-source', 'sha256': sha, 'role': 'product', 'mode': 'broll',
             'analysis': {'mode': 'single_shot'}}
    ref = {'asset_id': 'mixed-source', 'source_sha256': sha, 'usable': True,
           'duration_ticks': 960000, 'detect': None, 'product_identity_verified': False,
           'analysis_evidence_sha256': 'e' * 64,
           'coverage': [batch(0, 960000,
                              obs('火龙果包装盒正面摆放展示。', 0),
                              obs('双手正在打开火龙果包装盒。', 384000),
                              obs('火龙果包装盒已经打开，静态摆放。', 864000))]}
    return build_shot_library([asset], [ref], {'job_id': 'j', 'revision': 1})


def test_n3_sparse_action_tick_is_insufficient_not_a_validated_window():
    lib = sparse_library()
    action = [c for c in lib['candidates'] if any(
        a['kind'] == 'open_packaging' for a in c.get('supported_actions', []))]
    assert action, 'the action must be visible in supported_actions metadata'
    for c in action:
        for a in c['supported_actions']:
            if a['kind'] == 'open_packaging':
                assert a['evidence'] == 'attested_tick'   # lone tick: never a validated span
    d = plan('打开火龙果包装。', {'s001': (0, 96000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'
    # the lone tick is surfaced as a PROPOSAL for confirmation, never a window
    assert d['missing'][0].get('proposed_windows'), 'proposal metadata must be honest and visible'
    assert all(p['evidence'] in ('attested_tick', 'inferred_run')
               for p in d['missing'][0]['proposed_windows'])
    # explicitly: no auto window may sit at the old midpoint guess [4s,6s)
    for c in d['clips']:
        assert not (c.get('source_in_tick') == 192000 and c.get('source_out_tick') == 288000)


def test_n3b_confirmed_window_resolves_insufficient_evidence_with_binding():
    lib = sparse_library()
    conf = [{'sentence_id': 's001', 'source_asset_id': 'mixed-source', 'source_sha256': lib['sources']['mixed-source']['sha256'],
             'in_tick': 336000, 'out_tick': 432000, 'action_kind': 'open_packaging',
             'action_object': 'packaging', 'basis': '操作者逐帧核看 7-9s 为连续开盒动作',
             'operator_id': 'op', 'allow_source_window_overlap': False}]
    d = plan('打开火龙果包装。', {'s001': (0, 96000)}, lib, confirmed=conf)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert len(pool) == 1 and pool[0]['source_in_tick'] == 336000 and pool[0]['source_out_tick'] == 432000
    assert 'operator_confirmed' in json.dumps(pool[0]['match_evidence'])


def test_n3c_confirmed_window_rejects_mismatched_action_object_and_bounds():
    lib = sparse_library()
    sha = lib['sources']['mixed-source']['sha256']
    # wrong object: the confirmation is bound to the sentence need (open_packaging);
    # a split-fruit attestation can never unlock it
    wrong = [{'sentence_id': 's001', 'source_asset_id': 'mixed-source', 'source_sha256': sha,
              'in_tick': 336000, 'out_tick': 432000, 'action_kind': 'split_fruit',
              'action_object': 'fruit', 'basis': 'x', 'operator_id': 'op',
              'allow_source_window_overlap': False}]
    d = plan('打开火龙果包装。', {'s001': (0, 96000)}, lib, confirmed=wrong)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'
    # out of source bounds is refused outright
    beyond = [{'sentence_id': 's001', 'source_asset_id': 'mixed-source', 'source_sha256': sha,
               'in_tick': 900000, 'out_tick': 1000000, 'action_kind': 'open_packaging',
               'action_object': 'packaging', 'basis': 'x', 'operator_id': 'op',
               'allow_source_window_overlap': False}]
    with pytest.raises(ValueError):
        plan('打开火龙果包装。', {'s001': (0, 96000)}, lib, confirmed=beyond)


# ---- N4: one observation never blesses the whole batch ----

def test_n4_single_observation_batch_is_not_an_action_window():
    from scheduler.shot_library import build_shot_library
    sha = hashlib.sha256(b'wide-batch').hexdigest()
    asset = {'asset_id': 'wide', 'sha256': sha, 'role': 'product', 'mode': 'broll',
             'analysis': {'mode': 'single_shot'}}
    ref = {'asset_id': 'wide', 'source_sha256': sha, 'usable': True, 'duration_ticks': 960000,
           'detect': None, 'product_identity_verified': False, 'analysis_evidence_sha256': '0' * 64,
           'coverage': [batch(0, 960000, obs('双手正在打开火龙果包装盒。', 384000))]}
    lib = build_shot_library([asset], [ref], {'job_id': 'j', 'revision': 1})
    cands = [c for c in lib['candidates'] if c.get('supported_actions')]
    assert cands, 'metadata must surface the attested tick honestly'
    for c in cands:
        for a in c['supported_actions']:
            assert a['evidence'] == 'attested_tick'
            assert a['ticks'] == [384000]
    d = plan('打开火龙果包装。', {'s001': (0, 48000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'


# ---- N5: completion-state drift text ("继续小幅变动…保持一致") stays a display ----

def test_n5_completion_drift_with_continue_marker_is_no_action():
    from scheduler.shot_semantics import parse_observation
    p = parse_observation(SHOT1_DRIFT)
    assert not any(a['state'] == 'in_progress' for a in p['actions'])
    lib = build_real_library()
    drift = next(c for c in lib['candidates'] if c['source_sha256'] == SHA1 and c['in_tick'] == 264000)
    assert drift.get('supported_actions') in (None, [])


# ---- N6: unresolved sentence semantics stop instead of guessing ----

def test_n6_unresolved_sentence_reports_missing_not_a_guess():
    lib = build_real_library()
    d = plan('处理一下。', {'s001': (0, 96000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SENTENCE_SEMANTICS_UNRESOLVED'


# ---- N7 (M1 clarification): two same-action ticks with a long static gap, and
# ---- adjacent 1fps ticks — both stay inference, never auto-selected ----

def test_n7_two_action_ticks_across_static_gap_stay_inference():
    from scheduler.shot_library import build_shot_library
    from scheduler.shot_semantics import parse_observation, action_runs
    # 20s static source; action blips observed at 8s and 18s; static elsewhere
    sha = hashlib.sha256(b'gap-source').hexdigest()
    asset = {'asset_id': 'gap-source', 'sha256': sha, 'role': 'product', 'mode': 'broll',
             'analysis': {'mode': 'single_shot'}}
    ref = {'asset_id': 'gap-source', 'source_sha256': sha, 'usable': True, 'duration_ticks': 960000,
           'detect': None, 'product_identity_verified': False, 'analysis_evidence_sha256': '0' * 64,
           'coverage': [batch(0, 960000,
                              obs('台面静态，无变化。', 0),
                              obs('双手正在打开火龙果包装盒。', 384000),
                              obs('台面静态，无变化。', 624000),
                              obs('双手正在打开火龙果包装盒。', 864000))]}
    lib = build_shot_library([asset], [ref], {'job_id': 'j', 'revision': 1})
    runs = lib['action_runs'][sha]
    open_runs = [r for r in runs if r['kind'] == 'open_packaging']
    assert open_runs, 'both ticks must surface as honest metadata'
    for r in open_runs:
        assert r['evidence'] != 'inferred_run' or all(g['verified'] is False for g in r['gaps'])
        assert r['span'][1] - r['span'][0] < 480000, 'the 10s unobserved gap must not become one run span'
    d = plan('打开火龙果包装。', {'s001': (0, 96000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'


def test_n7b_adjacent_1fps_run_is_inference_with_gaps_never_auto():
    lib = build_real_library()
    runs = lib['action_runs'][SHA1]
    split = next(r for r in runs if r['kind'] == 'split_fruit')
    assert split['evidence'] == 'inferred_run'
    assert split['gaps'] == [{'from': 48000, 'to': 96000, 'ticks': 48000, 'verified': False}]
    # without a confirmed interval the real split sentence is NOT auto-selected
    d = plan('把火龙果掰开。', {'s001': (0, 96000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'
    proposal = next(p for p in d['missing'][0]['proposed_windows'] if p['source_sha256'] == SHA1)
    assert proposal['evidence'] == 'inferred_run' and proposal['gaps']


# ---- P1/P2/P3: real positives stay selectable (not everything missing) ----

def test_p1a_confirmed_interval_serves_split_sentence():
    """REAL positive with actual time-segment basis: an operator-REVIEWED interval
    for shot-1's splitting sequence (frames verified in zcode-r2b-media). The
    fixture simulates the post-review state; the parser never self-certifies."""
    lib = build_real_library()
    reusable = [confirmed_interval(SHA1, 'split_fruit', 'fruit', 48000, 144000,
                                   basis='已审核：帧证据 zcode-r2b-media/shot1-full-2fps.png（1-3s 双手掰开，3s 完成摆放）')]
    d = plan('把火龙果掰开。', {'s001': (0, 96000)}, lib, reusable=reusable)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert len(pool) == 1
    clip = pool[0]
    assert clip['source_sha256'] == SHA1
    assert clip['source_in_tick'] == 48000 and clip['source_out_tick'] == 144000
    ev = clip['match_evidence']
    assert ev['action_evidence'] == 'confirmed_interval'
    assert ev['confirmed']['operator_id'] == 'operator-reviewed'
    assert d['missing'] == []


def test_p1b_without_confirmed_interval_no_auto_pick_but_honest_proposal():
    lib = build_real_library()
    d = plan('把火龙果掰开。', {'s001': (0, 96000)}, lib)
    assert [c for c in d['clips'] if c['source'] == 'pool'] == []
    assert d['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'
    assert any(p['source_sha256'] == SHA1 and p['evidence'] == 'inferred_run'
               for p in d['missing'][0]['proposed_windows'])


def test_p2_detail_sentence_still_selects_real_display_window():
    lib = build_real_library()
    d = plan('果肉白色，籽粒黑色。', {'s001': (0, 96000)}, lib)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert len(pool) == 1 and d['missing'] == []
    assert 'detail' in json.dumps(pool[0]['match_evidence'])


def test_p3_completed_display_sentence_matches_static_candidates():
    lib = build_real_library()
    d = plan('看看已经掰开的火龙果切面。', {'s001': (0, 96000)}, lib)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert len(pool) == 1 and d['missing'] == []


# ---- M5: legacy libraries never pass the weak gate on new paths ----

def test_m5_legacy_library_is_refused_not_fallback():
    from scheduler.shot_planner import plan_shots, candidate_eligibility
    legacy_cand = {'candidate_id': 'cand-legacy', 'source_asset_id': 'a', 'source_sha256': 'f' * 64,
                   'in_tick': 0, 'out_tick': 240000, 'use': 'open_box', 'action_text': '打开包装取出果实',
                   'product_description': '打开包装取出火龙果果实', 'product_identity_verified': False,
                   'occlusion': 'unknown', 'clarity': 'unknown', 'speech_locked': False,
                   'evidence': {'analysis_evidence_sha256': 'e' * 64, 'frame_count': 1,
                                'observation_count': 1, 'observation_texts': ['打开包装取出火龙果果实']},
                   'fixed_role': None, 'usable': True, 'unusable_reason': None}
    legacy_lib = {'schema_version': 'shot-library/v1', 'library_sha256': '0' * 64,
                  'config': {'use_rule_version': 'use-keywords-v3', 'min_candidate_ticks': 24000},
                  'candidates': [legacy_cand], 'failures': []}
    from scheduler.script_plan import classify_visual
    sentence = {'sentence_id': 's001', 'text': '打开包装，看看火龙果。', 'claim_ids': [],
                'visual': classify_visual('打开包装，看看火龙果。')}
    ok, reason = candidate_eligibility(legacy_cand, sentence, ['火龙果'])
    assert ok is False and reason == 'LEGACY_LIBRARY_RULE_VERSION'
    np_ = {'schema_version': 'narration-plan/v1',
           'source': {'audio_sha256': 'a' * 64, 'actual_duration_ticks': 115200},
           'sentences': [{'sentence_id': 's001', 'in_tick': 0, 'out_tick': 96000, 'status': 'aligned',
                          'alignment_evidence_sha256': 'b' * 64}]}
    sp = {'schema_version': 'script-plan/v1', 'job_id': 'j', 'revision': 1, 'script_text': '打开包装，看看火龙果。',
          'script_sha256': '0' * 64, 'purpose': 'p', 'product_terms': ['火龙果'],
          'sentences': [sentence], 'library_sha256': None}
    with pytest.raises(ValueError, match='LIBRARY_RULE_VERSION_STALE'):
        plan_shots(sp, np_, legacy_lib, {'grid_ticks': 1920, 'max_tail_pause_ticks': 19200,
                                         'min_clip_ticks': 1920})


# ---- three entries share one verdict (confirmed evidence basis) ----

def test_three_entries_share_the_same_verdict():
    from scheduler.shot_planner import candidate_eligibility, apply_replacements
    lib = build_real_library()
    reusable = [confirmed_interval(SHA1, 'split_fruit', 'fruit', 48000, 144000)]
    from scheduler.script_plan import classify_visual
    sentence = {'sentence_id': 's001', 'text': '把火龙果掰开。', 'claim_ids': [],
                'visual': classify_visual('把火龙果掰开。')}
    split_cand = next(c for c in lib['candidates']
                      if any(a['kind'] == 'split_fruit' for a in c.get('supported_actions', [])))
    display_cand = next(c for c in lib['candidates'] if c['source_sha256'] == SHA2 and c['in_tick'] == 192000)
    # entry 1: direct constraint — confirmed evidence passes, absent evidence does not
    ok, _ = candidate_eligibility(split_cand, sentence, ['火龙果'], need_ticks=48000, confirmed=reusable)
    ok2, reason2 = candidate_eligibility(split_cand, sentence, ['火龙果'], need_ticks=48000, confirmed=[])
    assert ok is True and ok2 is False and reason2 == 'ACTION_EVIDENCE_INSUFFICIENT'
    # entry 2: planning serves the sentence from the confirmed interval
    d = plan('把火龙果掰开。', {'s001': (0, 48000)}, lib, reusable=reusable)
    pool = [c for c in d['clips'] if c['source'] == 'pool']
    assert len(pool) == 1
    picked = pool[0]
    assert picked['source_sha256'] == SHA1
    assert picked['source_in_tick'] == 48000 and picked['source_out_tick'] == 96000
    assert picked['match_evidence']['action_evidence'] == 'confirmed_interval'
    # entry 3: replacement passes with confirmed evidence, is refused without it
    new = apply_replacements(d, lib, [{'clip_id': picked['clip_id'],
                                       'candidate_id': picked['candidate_id'], 'reason': '重新选用同一确认区间来源'}],
                             script_plan={'sentences': [sentence], 'product_terms': ['火龙果']},
                             reusable_intervals=reusable)
    assert next(c for c in new['clips'] if c['clip_id'] == picked['clip_id'])['candidate_id'] == picked['candidate_id']
    with pytest.raises(ValueError, match='REPLACEMENT_'):
        apply_replacements(d, lib, [{'clip_id': picked['clip_id'],
                                     'candidate_id': picked['candidate_id'], 'reason': '无确认证据'}],
                           script_plan={'sentences': [sentence], 'product_terms': ['火龙果']},
                           reusable_intervals=[])
    with pytest.raises(ValueError):
        apply_replacements(d, lib, [{'clip_id': picked['clip_id'],
                                     'candidate_id': display_cand['candidate_id'], 'reason': '试图换成静态展示'}],
                           script_plan={'sentences': [sentence], 'product_terms': ['火龙果']},
                           reusable_intervals=reusable)
