"""Counterexamples for script↔audio subtitle review (S2); written before implementation."""
import hashlib, json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def words_of(pairs):
    # pairs: (text, start_s, end_s)
    return [{'text': t, 'start_tick': round(s * 48000), 'end_tick': round(e * 48000)} for t, s, e in pairs]


def script_plan_of(*sentences):
    text = ''.join(sentences)
    return {'schema_version': 'script-plan/v1', 'script_text': text,
            'script_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'sentences': [{'sentence_id': 's%03d' % (i + 1), 'text': s, 'claim_ids': [],
                           'visual': {'required': [], 'preferred': ['other'], 'alternatives_ok': True}}
                          for i, s in enumerate(sentences)]}


def narration_plan_of(audio_sha, sentences):
    return {'schema_version': 'narration-plan/v1', 'source': {'audio_sha256': audio_sha, 'actual_duration_ticks': 480000},
            'sentences': sentences}


def compare(script_plan, word_pairs, audio_sha='a' * 64):
    from scheduler.subtitle_review import compare_script_audio
    alignment = {'audio_sha256': audio_sha, 'words': words_of(word_pairs)}
    return compare_script_audio(script_plan, alignment, narration_plan_of(audio_sha, []))


def test_schema_file_valid():
    schema = json.loads((ROOT / 'contracts/subtitle-review-v1.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)


def test_perfect_match_is_matched_with_word_window():
    review = compare(script_plan_of('今天看一款火龙果。', '果肉是白心的。'),
                     [('今', 0.0, 0.1), ('天', 0.1, 0.2), ('看', 0.2, 0.3), ('一', 0.3, 0.4), ('款', 0.4, 0.5),
                      ('火龙果', 0.5, 0.9), ('。', 0.9, 0.95),
                      ('果肉', 1.2, 1.5), ('是', 1.5, 1.6), ('白心的', 1.6, 2.0), ('。', 2.0, 2.05)])
    assert [i['status'] for i in review['items']] == ['matched', 'matched']
    assert review['items'][0]['window'] == {'in_tick': 0, 'out_tick': round(0.95 * 48000)}
    assert review['items'][1]['window']['in_tick'] == round(1.2 * 48000)


def test_homophone_and_number_errors_are_uncertain_not_matched():
    review = compare(script_plan_of('里面是黑籽。', '价格九点九元。'),
                     [('里面', 0.0, 0.3), ('是', 0.3, 0.4), ('黑紫', 0.4, 0.7), ('。', 0.7, 0.75),
                      ('价格', 1.0, 1.3), ('9.9', 1.3, 1.7), ('元', 1.7, 1.8), ('。', 1.8, 1.85)])
    assert [i['status'] for i in review['items']] == ['uncertain', 'uncertain']
    assert review['items'][0]['diff_type'] != 'none'


def test_missing_and_extra_words_detected():
    review = compare(script_plan_of('打开包装取出果盘。', '很新鲜。'),
                     [('打开', 0.0, 0.3), ('包装', 0.3, 0.6), ('取出', 0.6, 0.9), ('。', 0.9, 0.95),
                      ('很', 1.2, 1.3), ('新鲜', 1.3, 1.6), ('啊', 1.6, 1.8), ('。', 1.8, 1.85)])
    assert review['items'][0]['status'] == 'uncertain'      # 漏读"果盘"
    assert review['items'][1]['diff_type'] == 'extra'       # 多读"啊"


def test_total_omission_and_silence_marked_not_guessed():
    review = compare(script_plan_of('第一句在这里。', '第二句完全没读。'),
                     [('第一句', 0.0, 0.5), ('在这里', 0.5, 0.9), ('。', 0.9, 0.95)])
    assert review['items'][1]['status'] == 'omission'
    assert review['items'][1]['window'] is None             # unknown stays unknown, no split/average


def test_stale_alignment_hash_rejected():
    from scheduler.subtitle_review import compare_script_audio
    with pytest.raises(ValueError):
        compare_script_audio(script_plan_of('一句。'),
                             {'audio_sha256': 'b' * 64, 'words': words_of([('一句', 0.0, 0.4)])},
                             narration_plan_of('a' * 64, []))


def test_decisions_and_gate():
    from scheduler.subtitle_review import apply_decision, unresolved_items, build_cues, REDUB
    review = compare(script_plan_of('里面是黑籽。'),
                     [('里面', 0.0, 0.3), ('是', 0.3, 0.4), ('黑紫', 0.4, 0.7), ('。', 0.7, 0.75)])
    assert unresolved_items(review) == ['s001']
    apply_decision(review, 's001', {'choice': 'asr_error_confirmed', 'operator_id': 'op', 'notes': '试听声音正确'})
    assert review['items'][0]['status'] == 'asr_error_confirmed'
    assert review['items'][0]['decision']['operator_id'] == 'op'
    assert unresolved_items(review) == []
    cues = build_cues(review)
    assert cues[0]['text'] == '里面是黑籽。'                  # subtitle uses script after operator confirms
    assert cues[0]['start_tick'] == 0 and cues[0]['end_tick'] == round(0.75 * 48000)
    # speech error stops the pipeline instead of passing
    review2 = compare(script_plan_of('里面是黑籽。'),
                      [('里面', 0.0, 0.3), ('是', 0.3, 0.4), ('黑紫', 0.4, 0.7), ('。', 0.7, 0.75)])
    apply_decision(review2, 's001', {'choice': 'speech_error_confirmed', 'operator_id': 'op', 'notes': '听错了'})
    assert REDUB in json.dumps(unresolved_items(review2, stop_on_speech_error=True))


def test_long_sentence_cues_split_within_asr_window():
    from scheduler.subtitle_review import apply_decision, build_cues
    long_text = '这一款火龙果的果肉颜色和籽粒分布都和普通品种不一样吃起来口感也很特别。'
    review = compare(script_plan_of(long_text),
                     [('这一款', 0.0, 0.4), ('火龙果', 0.4, 0.8), ('的', 0.8, 0.9), ('果肉', 0.9, 1.2),
                      ('颜色', 1.2, 1.5), ('和', 1.5, 1.6), ('籽粒', 1.6, 1.9), ('分布', 1.9, 2.2),
                      ('都', 2.2, 2.3), ('和', 2.3, 2.4), ('普通', 2.4, 2.7), ('品种', 2.7, 3.0),
                      ('不一样', 3.0, 3.4), ('吃起来', 3.4, 3.7), ('口感', 3.7, 4.0), ('也', 4.0, 4.1),
                      ('很', 4.1, 4.2), ('特别', 4.2, 4.5), ('。', 4.5, 4.55)])
    apply_decision(review, 's001', {'choice': 'asr_error_confirmed', 'operator_id': 'op', 'notes': 'ok'})
    cues = build_cues(review)
    assert all(len(c['text']) <= 16 for c in cues) and len(cues) >= 2
    assert cues[0]['start_tick'] == 0 and cues[-1]['end_tick'] == round(4.55 * 48000)
    assert all(cues[i]['end_tick'] <= cues[i + 1]['start_tick'] + 1 for i in range(len(cues) - 1))
