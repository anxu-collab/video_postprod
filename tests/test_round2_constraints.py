"""Round-2 counterexamples from independent-after-r1.py — written first, must fail
before the fixes: product lock is job-level, completion state dominates, action
windows need temporal evidence, speech-preservation metadata must surface."""
import hashlib, json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
import runpy
_h = runpy.run_path(str(ROOT / 'tests/test_shot_planner.py'))
script_plan = _h['script_plan']
narration_plan = _h['narration_plan']
cand = _h['cand']
library = _h['library']
CONSTRAINTS = _h['CONSTRAINTS']


def test_product_lock_applies_even_when_sentence_omits_the_product():
    from scheduler.shot_planner import plan_shots
    for sentence, desc, use in [
        ('打开包装。', '双手打开包装盒中的蓝牙耳机。', 'open_box'),
        ('果肉里布满黑色籽粒。', '西瓜果肉里布满黑色籽粒特写。', 'detail'),
    ]:
        sp = script_plan(sentence)
        sp['product_terms'] = ['火龙果']
        decision = plan_shots(sp, narration_plan({'s001': (0, 96000)}),
                              library(cand('src-x', use, 0, 480000, desc)), CONSTRAINTS)
        assert decision['clips'] == [], sentence
        assert decision['missing'] and 's001' in json.dumps(decision['missing']), sentence


def test_explicit_completed_state_is_not_action_in_progress():
    from scheduler.script_plan import action_in_progress
    assert action_in_progress('已经掰开的火龙果切面静态展示，没有变化。') is False
    assert action_in_progress('火龙果包装盒已经打开，静态摆放。') is False
    assert action_in_progress('双手向两侧用力掰开火龙果。') is True
    assert action_in_progress('双手正在打开火龙果包装盒。') is True
    assert action_in_progress('双手打开包装盒取出火龙果') is True


def test_action_window_requires_temporal_action_evidence():
    """20s batch with observations only at 0s/8s/18s: the lone 8s action tick
    proves no span (M1). Nothing auto-selects — not the midpoint [4s,6s) window,
    not the 8s tick dressed up as validated. The sentence reports insufficient
    evidence with the tick as an explicit PROPOSAL for operator confirmation."""
    from scheduler.shot_library import build_shot_library
    from scheduler.shot_planner import plan_shots
    sha = hashlib.sha256(b'mixed-source').hexdigest()
    asset = {'asset_id': 'mixed-source', 'role': 'product', 'sha256': sha,
             'analysis': {'mode': 'single_shot'}}
    ref = {'asset_id': 'mixed-source', 'source_sha256': sha, 'duration_ticks': 960000,
           'usable': True, 'analysis_evidence_sha256': 'e' * 64, 'path': None,
           'coverage': [{'status': 'success', 'in_tick': 0, 'out_tick': 960000,
                         'observations': [
                             {'frame_id': 'f0', 'source_tick': 0, 'text': '火龙果包装盒正面摆放展示。'},
                             {'frame_id': 'f8', 'source_tick': 384000, 'text': '双手正在打开火龙果包装盒。'},
                             {'frame_id': 'f18', 'source_tick': 864000, 'text': '火龙果包装盒已经打开，静态摆放。'}]}]}
    lib = build_shot_library([asset], [ref], {'job_id': 'j', 'revision': 1})
    # the lone action tick surfaces as attested_tick metadata — never a validated span
    for c in lib['candidates']:
        for a in c.get('supported_actions', []):
            if a['kind'] == 'open_packaging':
                assert a['evidence'] == 'attested_tick', a
    sp = script_plan('打开火龙果包装。')
    sp['product_terms'] = ['火龙果']
    decision = plan_shots(sp, narration_plan({'s001': (0, 96000)}), lib, CONSTRAINTS)
    chosen = [c for c in decision['clips'] if c['source'] == 'pool']
    assert chosen == [], 'a lone tick must not auto-select any window'
    assert decision['missing'][0]['reason'] == 'SHOT_EVIDENCE_INSUFFICIENT'
    assert decision['missing'][0]['proposed_windows'][0]['evidence'] == 'attested_tick'
    # explicitly: the old midpoint guess [4s,6s) is never a pickable window
    for c in decision['clips']:
        assert not (c.get('source_in_tick') == 192000 and c.get('source_out_tick') == 288000)


def test_speech_preservation_metadata_is_not_silently_unlocked():
    from scheduler.shot_library import build_shot_library
    sha = hashlib.sha256(b'speech-src').hexdigest()
    asset = {'asset_id': 'speech-src', 'role': 'product', 'sha256': sha,
             'analysis': {'mode': 'single_shot'}}
    ref = {'asset_id': 'speech-src', 'source_sha256': sha, 'duration_ticks': 960000,
           'usable': True, 'analysis_evidence_sha256': 'e' * 64,
           'coverage': [{'status': 'success', 'in_tick': 0, 'out_tick': 960000,
                         'observations': [{'frame_id': 'host', 'source_tick': 0,
                                           'text': '老X正在讲话介绍火龙果，必须保留原声和嘴型。'}]}]}
    lib = build_shot_library([asset], [ref], {'job_id': 'j', 'revision': 1})
    c = lib['candidates'][0]
    assert c['speech_locked'] is True
    assert c['usable'] is False and c['unusable_reason'] == 'SPEECH_PRESERVATION_REQUIRED'
