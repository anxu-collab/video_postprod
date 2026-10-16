"""Counterexamples for single-shot replacement and incremental reuse (S5/S6)."""
import copy, hashlib, json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

from scheduler.shot_planner import plan_shots, compile_edit_decision  # noqa: E402


def sp(*sents):
    text = ''.join(sents)
    return {'schema_version': 'script-plan/v1', 'job_id': 'j', 'revision': 1, 'script_text': text,
            'script_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'sentences': [{'sentence_id': 's%03d' % (i + 1), 'text': s, 'claim_ids': [],
                           'visual': __import__('scheduler.script_plan', fromlist=['classify_visual']).classify_visual(s)}
                          for i, s in enumerate(sents)]}


def np_(windows):
    end = max((w[1] for w in windows.values()), default=48000)
    return {'schema_version': 'narration-plan/v1', 'source': {'audio_sha256': 'a' * 64, 'actual_duration_ticks': end + 19200},
            'sentences': [{'sentence_id': k, 'in_tick': v[0], 'out_tick': v[1], 'status': 'aligned',
                           'alignment_evidence_sha256': 'b' * 64} for k, v in windows.items()]}


def cand(aid, use, in_tick, out_tick, text):
    sha = hashlib.sha256(aid.encode()).hexdigest()
    from scheduler.shot_semantics import supported_actions_for
    return {'candidate_id': 'cand-' + hashlib.sha256(('%s:%d:%d' % (sha, in_tick, out_tick)).encode()).hexdigest()[:32],
            'source_asset_id': aid, 'source_sha256': sha, 'in_tick': in_tick, 'out_tick': out_tick,
            'use': use, 'supported_actions': supported_actions_for([in_tick], [text], in_tick, out_tick),
            'action_text': text, 'product_description': text, 'product_identity_verified': False,
            'occlusion': 'unknown', 'clarity': 'unknown', 'speech_locked': False,
            'evidence': {'analysis_evidence_sha256': 'e' * 64, 'frame_count': 1, 'observation_count': 1,
                         'observation_texts': [text], 'observation_ticks': [in_tick],
                         'sampling_interval_ticks': 48000, 'per_observation': []},
            'fixed_role': None, 'usable': True, 'unusable_reason': None}


def reusable(cand_or_sha, kind, obj, in_tick, out_tick):
    sha = cand_or_sha['source_sha256'] if isinstance(cand_or_sha, dict) else cand_or_sha
    return {'source_sha256': sha, 'action_kind': kind, 'action_object': obj,
            'in_tick': in_tick, 'out_tick': out_tick, 'operator_id': 'fixture-reviewed',
            'basis': 'fixture simulates the post-review state', 'origin': {'kind': 'fixture'}}


CONST = {'grid_ticks': 1920, 'max_tail_pause_ticks': 19200, 'min_clip_ticks': 1920}


def decision_two_shots():
    from scheduler.contracts import canonical_hash
    from scheduler.shot_semantics import RULE_VERSION
    c1 = cand('a', 'open_box', 0, 240000, '打开包装取出火龙果果实')
    c2 = cand('b', 'detail', 0, 240000, '火龙果果实特写新鲜')
    lib = {'schema_version': 'shot-library/v1',
           'library_sha256': canonical_hash({'candidates': [c['candidate_id'] for c in (c1, c2)]}),
           'config': {'use_rule_version': RULE_VERSION, 'min_candidate_ticks': 24000},
           'candidates': sorted((c1, c2), key=lambda c: (c['source_sha256'], c['in_tick'])), 'failures': []}
    reusable_list = [reusable(c1, 'open_packaging', 'packaging', 0, 96000)]
    d = plan_shots(sp('打开包装，看看里面。', '果肉很新鲜。'), np_({'s001': (0, 96000), 's002': (96000, 192000)}),
                   lib, CONST, reusable_intervals=reusable_list)
    return d, lib, (c1, c2), reusable_list


def test_replacement_keeps_duration_and_narration_untouched():
    from scheduler.shot_planner import apply_replacements
    d, lib, (c1, c2), reusable_list = decision_two_shots()
    original_narration = copy.deepcopy(d['narration'])
    target = next(c for c in d['clips'] if c['source'] == 'pool')
    fresh = cand('c3', 'open_box', 0, 240000, '另一个角度的火龙果开盒画面')
    lib3 = dict(lib, candidates=lib['candidates'] + [fresh])
    new = apply_replacements(d, lib3, [{'clip_id': target['clip_id'], 'candidate_id': fresh['candidate_id'],
                                        'reason': '画面更清晰'}],
                             script_plan={'sentences': [{'sentence_id': 's001', 'text': '打开包装，看看里面。',
                                                         'claim_ids': [],
                                                         'visual': sp('打开包装，看看里面。')['sentences'][0]['visual']}],
                                          'product_terms': ['火龙果']},
                             reusable_intervals=reusable_list + [reusable(fresh, 'open_packaging', 'packaging', 0, 96000)])
    replaced = next(c for c in new['clips'] if c['clip_id'] == target['clip_id'])
    assert replaced['duration_tick'] == target['duration_tick']       # equal-length swap only
    assert replaced['timeline_start_tick'] == target['timeline_start_tick']
    assert replaced['candidate_id'] == fresh['candidate_id']
    assert '画面更清晰' in replaced['match_reason']
    assert new['narration'] == original_narration                    # voice untouched by a picture swap


def test_replacement_rejects_unusable_short_window_and_silent_overlap():
    from scheduler.shot_planner import apply_replacements
    from scheduler.contracts import canonical_hash
    from scheduler.shot_semantics import RULE_VERSION
    # local fixture: two pool clips share one source; replacing the first with a
    # candidate whose CONFIRMED interval lands on the second clip's window needs
    # an explicit overlap allowance — and still requires confirmed evidence.
    c1 = cand('a', 'open_box', 0, 240000, '打开包装取出火龙果果实')
    c2 = cand('a', 'detail', 96000, 240000, '火龙果果实特写新鲜')   # same source, later window
    c4 = cand('a', 'open_box', 96000, 264000, '同源另一段已确认的火龙果开盒画面')
    lib = {'schema_version': 'shot-library/v1',
           'library_sha256': canonical_hash({'candidates': [c['candidate_id'] for c in (c1, c2, c4)]}),
           'config': {'use_rule_version': RULE_VERSION, 'min_candidate_ticks': 24000},
           'candidates': [c1, c2, c4], 'failures': []}
    d = {'schema_version': 'edit-decision/v1', 'job_id': 'j', 'revision': 1, 'inputs': {},
         'clips': [
             {'clip_id': 'clip-001', 'source': 'pool', 'candidate_id': c1['candidate_id'], 'asset_id': 'a',
              'source_sha256': c1['source_sha256'], 'source_in_tick': 0, 'source_out_tick': 96000,
              'timeline_start_tick': 0, 'duration_tick': 96000, 'sentence_ids': ['s001'],
              'match_reason': 'x', 'match_evidence': {}, 'fixed_role': None, 'overlap_exception': None},
             {'clip_id': 'clip-002', 'source': 'pool', 'candidate_id': c2['candidate_id'], 'asset_id': 'a',
              'source_sha256': c2['source_sha256'], 'source_in_tick': 96000, 'source_out_tick': 192000,
              'timeline_start_tick': 96000, 'duration_tick': 96000, 'sentence_ids': ['s002'],
              'match_reason': 'x', 'match_evidence': {}, 'fixed_role': None, 'overlap_exception': None}],
         'missing': [], 'tail_pause_ticks': 0,
         'narration': {'audio_sha256': 'a' * 64, 'body_start_tick': 0, 'sentences': []}}
    spn = {'sentences': [{'sentence_id': 's001', 'text': '打开包装，看看里面。', 'claim_ids': [],
                          'visual': sp('打开包装，看看里面。')['sentences'][0]['visual']}],
           'product_terms': ['火龙果']}
    confirmed = [reusable(c1['source_sha256'], 'open_packaging', 'packaging', 96000, 192000)]
    # no confirmed evidence for the candidate: refused (M1 — inference never swaps in)
    with pytest.raises(ValueError, match='ACTION_EVIDENCE_INSUFFICIENT'):
        apply_replacements(d, lib, [{'clip_id': 'clip-001', 'candidate_id': c1['candidate_id'], 'reason': '无证据'}],
                           script_plan=spn, reusable_intervals=[])
    short = cand('short', 'detail', 0, 2400, '太短')
    lib2 = dict(lib, candidates=lib['candidates'] + [short])
    with pytest.raises(ValueError):
        apply_replacements(d, lib2, [{'clip_id': 'clip-001', 'candidate_id': short['candidate_id'], 'reason': 'x'}],
                           script_plan=spn, reusable_intervals=confirmed)
    # confirmed interval lands on clip-002's used window: needs explicit allowance
    with pytest.raises(ValueError, match='OVERLAP_FORBIDDEN'):
        apply_replacements(d, lib, [{'clip_id': 'clip-001', 'candidate_id': c4['candidate_id'], 'reason': '重叠'}],
                           script_plan=spn, reusable_intervals=confirmed)
    new = apply_replacements(d, lib, [{'clip_id': 'clip-001', 'candidate_id': c4['candidate_id'],
                                       'reason': '用户指定复用同一窗口', 'allow_source_window_overlap': True}],
                             script_plan=spn, reusable_intervals=confirmed)
    replaced = next(c for c in new['clips'] if c['clip_id'] == 'clip-001')
    assert replaced['overlap_exception'] and replaced['overlap_exception']['operator_choice'] is True


def test_revision_plan_reuses_voice_for_subtitle_and_shot_edits():
    from scheduler.revision_plan import plan_revision
    def job(script='A。', narration='auto', preset='p1', subtitles=None, clips=None, bgm=None, assets=1):
        return {'schema_version': 'edit-job/v2', 'script_input': {'mode': 'pasted', 'script_text': script,
                                                                  'script_sha256': hashlib.sha256(script.encode()).hexdigest()},
                'narration_input': {'mode': narration, 'preset': preset if narration == 'auto' else None,
                                    'uploaded': None, 'history': None},
                'assets': [{'asset_id': 'x%d' % i} for i in range(assets)],
                'overrides': {'subtitle_edits': subtitles or [], 'bgm_override': bgm, 'shot_edits': [], 'speech_edits': []},
                'v2_overrides': {'clip_replacements': clips or [], 'sentence_redubs': []}}
    old = job()
    new = job(subtitles=[{'cue_id': 'cue-1', 'text': '改字'}])
    plan = plan_revision(old, new, {})
    assert 'speech.synthesize' in plan['reuse'] and 'speech.transcribe' in plan['reuse']
    assert 'video.analyze' in plan['reuse']
    assert {'subtitle.layout', 'picture.caption', 'mux.final'} <= set(plan['invalidated'])
    new = job(clips=[{'clip_id': 'clip-001', 'candidate_id': 'cand-x', 'reason': 'r'}])
    plan = plan_revision(old, new, {})
    assert 'speech.synthesize' in plan['reuse'] and 'speech.transcribe' in plan['reuse']
    assert 'video.analyze' in plan['reuse']
    assert 'edit.plan' in plan['invalidated'] and 'video.chunk' in plan['invalidated']
    new = job(bgm={'path': 'p', 'sha256': '0' * 64, 'provenance_ref': 'q', 'gain_db': -30})
    plan = plan_revision(old, new, {})
    assert 'speech.synthesize' in plan['reuse']
    assert {'mix.raw', 'mix.loudnorm', 'mux.final'} <= set(plan['invalidated'])


def test_revision_plan_invalidates_downstream_on_script_or_audio_change():
    from scheduler.revision_plan import plan_revision
    def job(script, uploaded=None, preset='p1'):
        return {'schema_version': 'edit-job/v2',
                'script_input': {'mode': 'pasted', 'script_text': script, 'script_sha256': hashlib.sha256(script.encode()).hexdigest()},
                'narration_input': {'mode': 'uploaded' if uploaded else 'auto', 'preset': None if uploaded else preset,
                                    'uploaded': uploaded, 'history': None},
                'assets': [], 'overrides': {'subtitle_edits': [], 'bgm_override': None, 'shot_edits': [], 'speech_edits': []},
                'v2_overrides': {'clip_replacements': [], 'sentence_redubs': []}}
    plan = plan_revision(job('旧稿。'), job('新稿。'), {})
    for node in ['script.build', 'speech.synthesize', 'speech.transcribe', 'subtitle.review', 'edit.plan', 'video.chunk', 'mux.final']:
        assert node in plan['invalidated'], node
    # uploaded audio change keeps TTS nodes (there are none to redo) but invalidates alignment/subtitles/planning
    plan = plan_revision(job('稿。', uploaded={'path': 'a', 'sha256': '0' * 64}),
                         job('稿。', uploaded={'path': 'b', 'sha256': '1' * 64}), {})
    assert 'speech.synthesize' not in plan['invalidated']
    for node in ['speech.transcribe', 'subtitle.review', 'edit.plan']:
        assert node in plan['invalidated'], node


def test_v2_revision_diff_in_dag():
    from scheduler.dag import revision_diff
    def job(**kw):
        base = {'schema_version': 'edit-job/v2',
                'script_input': {'mode': 'pasted', 'script_text': kw.get('script', 's'), 'script_sha256': hashlib.sha256(kw.get('script', 's').encode()).hexdigest()},
                'narration_input': {'mode': 'auto', 'preset': kw.get('preset', 'p'), 'uploaded': None, 'history': None},
                'assets': kw.get('assets', [{'a': 1}]),
                'templates': {'bgm': kw.get('bgm', None)},
                'overrides': {'subtitle_edits': kw.get('subs', []), 'bgm_override': None, 'shot_edits': [], 'speech_edits': []},
                'v2_overrides': {'clip_replacements': kw.get('clips', []), 'sentence_redubs': kw.get('redubs', [])}}
        return base
    dirty = revision_diff(job(), job(subs=[{'cue_id': 'c', 'text': 't'}]))
    assert {'subtitle.layout', 'picture.caption', 'mux.final'} <= dirty
    dirty = revision_diff(job(), job(clips=[{'clip_id': 'c1', 'candidate_id': 'x', 'reason': 'r'}]))
    assert 'edit.plan' in dirty and 'speech.synthesize' not in dirty
