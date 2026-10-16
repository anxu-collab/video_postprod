"""Counterexamples for full-script planning (S4); written before implementation."""
import hashlib, json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def build(script_input, evidence=None, library=None, generate=None):
    from scheduler.script_plan import build_script_plan
    return build_script_plan(script_input,
                             evidence or {'brief': '火龙果画面介绍', 'claims': [], 'documents': {}},
                             library or {'schema_version': 'shot-library/v1', 'candidates': []},
                             generate=generate)


def pasted(text):
    return {'mode': 'pasted', 'script_text': text, 'script_sha256': hashlib.sha256(text.encode()).hexdigest()}


def test_schema_file_valid_and_sentence_fields_required():
    schema = json.loads((ROOT / 'contracts/script-plan-v1.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)


def test_pasted_order_is_script_order_not_upload_order():
    text = '大家好，我是老X。今天看一款火龙果。打开包装，看看里面。果肉是白心的。'
    plan = build(pasted(text))
    texts = [s['text'] for s in plan['sentences']]
    assert texts == ['大家好，我是老X。', '今天看一款火龙果。', '打开包装，看看里面。', '果肉是白心的。']
    assert [s['sentence_id'] for s in plan['sentences']] == ['s001', 's002', 's003', 's004']
    assert plan['script_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    # stable across calls and independent of library order
    again = build(pasted(text), library={'schema_version': 'shot-library/v1', 'candidates': [{'candidate_id': 'x'}, {'candidate_id': 'a'}]})
    assert again['sentences'] == plan['sentences']


def test_open_box_requirement_is_not_packaging_front():
    plan = build(pasted('打开包装，看看里面。'))
    need = plan['sentences'][0]['visual']
    # r3: opening PACKAGING is its own kind; a packaging-front display cannot serve it
    assert 'open_packaging' in need['required'] and need['required'][0] == 'open_packaging'
    assert plan['sentences'][0]['semantics']['action_object'] == 'packaging'


def test_generated_mode_needs_generate_and_pasted_never_calls():
    calls = []
    def generate(brief, prompt):
        calls.append(1)
        return '这是生成的整稿。一共两句。'
    plan = build({'mode': 'generated', 'script_text': None, 'script_sha256': None}, generate=generate)
    assert calls == [1] and plan['script_text'] == '这是生成的整稿。一共两句。'
    assert plan['source_mode'] == 'generated'
    with pytest.raises(ValueError):
        build({'mode': 'generated', 'script_text': None, 'script_sha256': None})  # no binding: refuse, no silent fallback
    n = []
    build(pasted('粘贴稿一句。'), generate=lambda *a: n.append(1) or 'x')
    assert n == []  # pasted never invokes generation


def test_claim_binding_by_substring():
    evidence = {'brief': 'b', 'documents': {'doc1': '产地为云南，单果约350克。'},
                'claims': [{'claim_id': 'c1', 'document_id': 'doc1', 'text': '单果约350克', 'source_span': '单果约350克'}]}
    plan = build(pasted('单果约350克，重量实在。'), evidence=evidence)
    assert plan['sentences'][0]['claim_ids'] == ['c1']


def test_sentence_ids_stable_when_text_prefix_changes():
    a = build(pasted('第一句。第二句。第三句。'))
    b = build(pasted('换掉的第一句。第二句。第三句。'))
    assert [s['text'] for s in b['sentences']] == ['换掉的第一句。', '第二句。', '第三句。']
    assert [s['sentence_id'] for s in a['sentences']] == [s['sentence_id'] for s in b['sentences']]
