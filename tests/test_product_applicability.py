import copy
import json

import pytest

from scheduler.clef_adapter import ClefDecisionAdapter


PRODUCT = {'product_id': 'p1', 'name': '测试产品', 'documents': [{'document_id': 'doc1', 'text': '这是本产品完整资料，分类为食品。'}]}


def select_reply(payload):
    answers = {}
    for qid, question in payload['questions'].items():
        keys = list(question['criteria'])
        choice = next((k for k in keys if k.startswith('applicable__')), keys[0])
        if qid.endswith('_evidence'): choice = 'doc1'
        answers[qid] = {'type': 'choice', 'choice': choice, 'scores': {}}
    return {'model': 'injected-test', 'answers': answers}


def test_approved_missing_verbatim_phrase_is_model_selected_and_auditable(tmp_path):
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    result = select_applicable(PRODUCT, load_catalog(), adapter=ClefDecisionAdapter(transport=select_reply), entry_ids=['A06'], work=tmp_path)
    assert result['status'] == 'completed' and result['selected_ids'] == ['A06']
    decision = result['decisions'][0]
    assert decision['status'] == 'applicable' and decision['evidence_refs'] == ['doc1']
    assert decision['reason_code'] == 'product_context_fit'
    assert decision['medical_verification_claimed'] is False
    assert result['requests'][0]['response']['model'] == 'injected-test'
    assert result['coverage']['not_evaluated_ids'] == []
    assert list(tmp_path.rglob('*.json'))


def test_oversize_documents_are_not_silently_cut_and_never_infer():
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    product = copy.deepcopy(PRODUCT); product['documents'][0]['text'] = '中文资料' * 1500
    adapter = ClefDecisionAdapter(transport=lambda payload: pytest.fail('oversize must not infer'))
    result = select_applicable(product, load_catalog(), adapter=adapter, entry_ids=['A01'])
    assert result['selected_ids'] == []
    assert result['decisions'][0]['status'] == 'insufficient_information'
    assert result['decisions'][0]['reason_code'] == 'context_budget_exceeded'
    assert result['input_snapshot']['product'] == product


def test_budget_exhaustion_preserves_not_evaluated_and_batches_fit():
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    adapter = ClefDecisionAdapter({'max_requests': 1}, transport=select_reply)
    result = select_applicable(PRODUCT, load_catalog(), adapter=adapter)
    assert len(result['requests']) >= 1
    assert result['coverage']['not_evaluated_ids']
    assert all(len(json.dumps(r['request'], ensure_ascii=False)) <= 4500 for r in result['requests'] if r['response'])
    assert all(d['status'] == 'not_evaluated' for d in result['decisions'] if d['entry_id'] in result['coverage']['not_evaluated_ids'])


def test_cache_replay_never_infers_and_changes_invalidate(tmp_path):
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    catalog = load_catalog()
    first = select_applicable(PRODUCT, catalog, adapter=ClefDecisionAdapter(transport=select_reply), entry_ids=['A01'], work=tmp_path)
    def no_send(payload): pytest.fail('cache replay must not infer')
    second = select_applicable(PRODUCT, catalog, adapter=ClefDecisionAdapter(transport=no_send), entry_ids=['A01'], work=tmp_path)
    assert second['selected_ids'] == first['selected_ids'] and second['requests'][0]['cached']
    product = copy.deepcopy(PRODUCT);product['documents'][0]['text'] += ' changed'
    third = select_applicable(product, catalog, adapter=ClefDecisionAdapter(transport=select_reply), entry_ids=['A01'], work=tmp_path)
    assert third['product_sha256'] != first['product_sha256'] and not third['requests'][0]['cached']


def test_unknown_ids_and_unapproved_or_changed_catalog_rejected_before_inference():
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    adapter = ClefDecisionAdapter(transport=lambda payload: pytest.fail('invalid request must not infer'))
    catalog = load_catalog()
    with pytest.raises(ValueError): select_applicable(PRODUCT, catalog, adapter=adapter, entry_ids=['UNKNOWN'])
    catalog['entries'][0]['approval']['status'] = 'pending'
    with pytest.raises(ValueError): select_applicable(PRODUCT, catalog, adapter=adapter)


def test_model_failure_and_same_category_do_not_auto_select():
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    adapter = ClefDecisionAdapter(transport=lambda payload: {'answers': {}})
    result = select_applicable(dict(PRODUCT, category='blood'), load_catalog(), adapter=adapter, entry_ids=['A01'])
    assert result['selected_ids'] == [] and result['status'] == 'service_error'
    assert result['decisions'][0]['status'] == 'insufficient_information'


def test_session_exit_failure_invalidates_choices_and_records_exit_error(monkeypatch):
    import contextlib
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    @contextlib.contextmanager
    def broken(self):
        yield select_reply
        raise RuntimeError('test session exit failed')
    monkeypatch.setattr(ClefDecisionAdapter, '_session', broken)
    result = select_applicable(PRODUCT, load_catalog(), adapter=ClefDecisionAdapter(transport=select_reply), entry_ids=['A01'])
    assert result['status'] == 'service_error' and result['selected_ids'] == []
    assert result['decisions'][0]['status'] == 'insufficient_information'
    assert 'test session exit failed' in result['lifecycle']['session_error']


def test_applicable_choice_requires_explicit_finite_evidence_choice():
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    def no_evidence(payload):
        answer = select_reply(payload)
        for qid, row in answer['answers'].items():
            if qid.endswith('_evidence'): row['choice'] = 'NONE'
        return answer
    result = select_applicable(PRODUCT, load_catalog(), adapter=ClefDecisionAdapter(transport=no_evidence), entry_ids=['A01'])
    assert result['selected_ids'] == []
    assert result['decisions'][0]['reason_code'] == 'missing_evidence_selection'


def test_omitted_candidates_are_not_claimed_as_model_context():
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    result = select_applicable(PRODUCT, load_catalog(), adapter=ClefDecisionAdapter(transport=select_reply), entry_ids=['A01'])
    assert result['coverage']['context_only_ids'] == []
    assert 'A02' in result['coverage']['excluded_ids']


def test_later_invalid_response_invalidates_earlier_applicable_choices():
    from scheduler.expression_catalog import load_catalog
    from scheduler.product_applicability import select_applicable
    calls = []
    def first_only(payload):
        calls.append(payload)
        return select_reply(payload) if len(calls) == 1 else {'answers': {}}
    result = select_applicable(PRODUCT, load_catalog(), adapter=ClefDecisionAdapter(transport=first_only), entry_ids=['SHORT-GUT-001-1', 'SHORT-GUT-001-2'])
    assert len(result['requests']) == 2 and result['status'] == 'service_error'
    assert result['selected_ids'] == [] and all(d['status'] != 'applicable' for d in result['decisions'])

