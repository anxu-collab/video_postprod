"""Bounded Clef selection of approved expressions for one complete product input."""
import copy
import contextlib
import json
import os
import re
import uuid
from pathlib import Path

from scheduler.clef_adapter import ClefDecisionAdapter, RULE_VERSION as ADAPTER_RULE_VERSION, safe
from scheduler.contracts import canonical_hash
from scheduler.expression_catalog import validate_catalog

RULE_VERSION = 'product-expression-applicability/v1'
DEFAULT_KINDS = {'expression', 'pattern', 'painpoint', 'summary'}
_DECISIONS = {
    'applicable__product_context_fit': '产品完整语境支持采用该已审批表达，不要求资料有同句原文',
    'applicable__expression_pattern_fit': '连接句式适用于本期表达，填入的具体事实仍需另行核对',
    'not_applicable__product_context_conflict': '词卡完整语境与本产品资料冲突或属于不同产品、作用对象',
    'insufficient_information__information_missing': '信息不足以判断产品适用',
}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _FileCache:
    """Task-local cache: only validated classifier responses enter it."""
    def __init__(self, path): self.path = path

    def cache_get(self, key):
        path = self.path / (key + '.json')
        if not path.exists(): return None
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('key') != key or data.get('response_sha256') != canonical_hash(data.get('response')):
            raise ValueError('CLEF_CACHE_CORRUPT')
        return data['response']

    def cache_put(self, key, value):
        _write_json(self.path / (key + '.json'), {'key': key, 'response': value, 'response_sha256': canonical_hash(value)})


def _validate_product(product):
    if not isinstance(product, dict) or any(not isinstance(product.get(k), str) or not product[k].strip() for k in ('product_id', 'name')):
        raise ValueError('PRODUCT_ID_AND_NAME_REQUIRED')
    docs = product.get('documents')
    if not isinstance(docs, list) or not docs:
        raise ValueError('PRODUCT_DOCUMENTS_REQUIRED')
    seen = set()
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get('document_id'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', doc['document_id']):
            raise ValueError('PRODUCT_DOCUMENT_ID_INVALID')
        if doc['document_id'] in seen or doc['document_id'] == 'NONE':
            raise ValueError('PRODUCT_DOCUMENT_ID_DUPLICATE_OR_RESERVED')
        seen.add(doc['document_id'])
        if not isinstance(doc.get('text'), str) or not doc['text'].strip():
            raise ValueError('PRODUCT_DOCUMENT_TEXT_REQUIRED')
    try:
        json.dumps(product, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError('PRODUCT_NOT_FINITE_JSON')


def _payload(product, entries, catalog_sha):
    questions = {}
    for index, entry in enumerate(entries):
        prefix = 'e' + str(index)
        questions[prefix + '_decision'] = {
            'type': 'choice', 'instructions': '判断词卡 ' + entry['id'] + ' 对该产品是否适用，并选择明确理由。',
            'criteria': _DECISIONS}
        questions[prefix + '_evidence'] = {
            'type': 'choice', 'instructions': '为词卡 ' + entry['id'] + ' 的判断选择最相关的产品资料ID；这是模型选择的依据定位，不是事实验证。',
            'criteria': {**{d['document_id']: '产品资料 ' + d['document_id'] for d in product['documents']}, 'NONE': '没有可定位的产品资料'}}
    return {'state': {'task': RULE_VERSION, 'catalog_sha256': catalog_sha,
                      'product': copy.deepcopy(product), 'entries': copy.deepcopy(entries),
                      'instructions': '只判产品适用。行业参考均已审批；资料没有同句不能自动否决。分类相同不能自动通过。'
                                      '保留词卡的临床、来源产品、假设场景及传统用途语境；不虚构病因、疗效、真实体验。'
                                      '表达审批和本次概率均不等于医学事实核实。'},
            'questions': questions}


def _choice(response, qid, questions):
    answers = response.get('answers') or {}
    answer = answers[qid]
    if answer.get('choice') is not None:
        return answer['choice']
    probabilities = answer.get('probabilities') or answer.get('scores') or {}
    best = max(probabilities.values())
    choices = [key for key, value in probabilities.items() if value == best]
    if len(choices) != 1:
        raise ValueError('CLEF_TIED_CHOICE')
    choice = choices[0]
    if choice not in questions[qid]['criteria']:
        raise ValueError('CLEF_UNKNOWN_CHOICE')
    return choice


def select_applicable(product, catalog, *, adapter=None, entry_ids=None, work=None):
    """Return approved choices plus complete coverage, requests and input snapshot.

    work optionally stores content-addressed input/result snapshots and persistent
    classifier cache. No calls to the video pipeline, cloud writer or user approval.
    """
    _validate_product(product)
    validate_catalog(catalog)
    product, catalog = copy.deepcopy(product), copy.deepcopy(catalog)
    by_id = {entry['id']: entry for entry in catalog['entries']}
    if entry_ids is None:
        candidates = [entry for entry in catalog['entries'] if entry['kind'] in DEFAULT_KINDS]
    else:
        if not isinstance(entry_ids, (list, tuple)) or any(not isinstance(eid, str) for eid in entry_ids):
            raise ValueError('EXPRESSION_ENTRY_IDS_INVALID')
        if len(set(entry_ids)) != len(entry_ids) or any(eid not in by_id for eid in entry_ids):
            raise ValueError('EXPRESSION_ENTRY_ID_UNKNOWN_OR_DUPLICATE')
        candidates = [by_id[eid] for eid in entry_ids]
    adapter = adapter or ClefDecisionAdapter()
    if adapter.max_retries != 0:
        raise ValueError('PRODUCT_APPLICABILITY_REQUIRES_ZERO_RETRIES')
    product_sha = canonical_hash(product)
    snapshot = {'product': product, 'catalog_sha256': catalog['catalog_sha256'],
                'entries': candidates, 'rule_version': RULE_VERSION, 'adapter_rule_version': ADAPTER_RULE_VERSION,
                'model_version': adapter.model_version, 'model_fingerprint': adapter.fingerprint}
    input_hash = canonical_hash(snapshot)
    candidate_ids = {entry['id'] for entry in candidates}
    context_ids = set()
    for entry in candidates:
        context = entry['context']
        if context.get('parent'):
            context_ids.add(context['parent']['id'])
        context_ids.update(observation['id'] for observation in context.get('observations', []))
    context_ids -= candidate_ids
    result = {'schema_version': 'product-expression-applicability/v1', 'status': 'completed',
              'product_sha256': product_sha, 'catalog_sha256': catalog['catalog_sha256'],
              'input_sha256': input_hash, 'rule_version': RULE_VERSION,
              'model_version': adapter.model_version, 'model_fingerprint': adapter.fingerprint,
              'selected_ids': [], 'decisions': [], 'requests': [], 'error': None,
              'input_snapshot': snapshot,
              'interpretation': 'Clef product applicability and model-selected document references; not medical verification',
              'coverage': {'candidate_ids': [e['id'] for e in candidates],
                           'context_only_ids': sorted(context_ids),
                           'excluded_ids': [e['id'] for e in catalog['entries'] if e['id'] not in candidate_ids | context_ids],
                           'evaluated_ids': [], 'not_evaluated_ids': [], 'context_budget_exceeded_ids': []}}
    decisions = {}
    for entry in candidates:
        row = {'entry_id': entry['id'], 'status': 'not_evaluated', 'reason_code': 'not_evaluated',
               'evidence_refs': [], 'request_index': None, 'medical_verification_claimed': False}
        decisions[entry['id']] = row
        result['decisions'].append(row)

    # Greedy packing measures the actual serialized request including all questions.
    batches, pending = [], []
    for entry in candidates:
        if not adapter._fits(_payload(product, [entry], catalog['catalog_sha256'])):
            decisions[entry['id']].update(status='insufficient_information', reason_code='context_budget_exceeded')
            result['coverage']['context_budget_exceeded_ids'].append(entry['id'])
            continue
        if pending and not adapter._fits(_payload(product, pending + [entry], catalog['catalog_sha256'])):
            batches.append(pending)
            pending = []
        pending.append(entry)
    if pending:
        batches.append(pending)
    repository = _FileCache(Path(work) / 'clef-cache') if work is not None else None
    if work is not None:
        _write_json(Path(work) / 'inputs' / (input_hash + '.json'), snapshot)
    stream = adapter.classify_many((_payload(product, batch, catalog['catalog_sha256']) for batch in batches), repository)
    try:
        with contextlib.closing(stream):
            for batch, request in zip(batches, stream):
                index = len(result['requests'])
                result['requests'].append(request)
                error = request['error']
                if error:
                    result['error'] = error
                    if 'BUDGET_EXHAUSTED' in error:
                        result['status'] = 'partial'
                        break
                    result['status'] = 'service_error'
                    for entry in batch:
                        decisions[entry['id']].update(status='insufficient_information', reason_code='model_error', request_index=index)
                    break
                for position, entry in enumerate(batch):
                    row = decisions[entry['id']]
                    qid = 'e' + str(position)
                    choice = _choice(request['response'], qid + '_decision', request['request']['questions'])
                    evidence = _choice(request['response'], qid + '_evidence', request['request']['questions'])
                    status, reason = choice.split('__', 1)
                    if status == 'applicable' and evidence == 'NONE':
                        status, reason = 'insufficient_information', 'missing_evidence_selection'
                    row.update(status=status, reason_code=reason, evidence_refs=[] if evidence == 'NONE' else [evidence], request_index=index)
                    result['coverage']['evaluated_ids'].append(entry['id'])
    except Exception as exc:
        result['status'] = 'service_error'
        result['error'] = safe(type(exc).__name__ + ':' + str(exc))
        # Failed cleanup or decoding cannot remain production advice.
        for row in result['decisions']:
            if row['status'] != 'not_evaluated':
                row['status_before_failure'] = row['status']
                row.update(status='insufficient_information', reason_code='session_or_response_error')
    result['lifecycle'] = copy.deepcopy(adapter.last_lifecycle)
    result['model_sha256'] = adapter.model_sha256
    result['coverage']['not_evaluated_ids'] = [r['entry_id'] for r in result['decisions'] if r['status'] == 'not_evaluated']
    result['selected_ids'] = [r['entry_id'] for r in result['decisions'] if r['status'] == 'applicable']
    if result['status'] == 'completed' and (result['coverage']['context_budget_exceeded_ids'] or result['coverage']['not_evaluated_ids']):
        result['status'] = 'partial'
    if result['status'] == 'service_error':
        result['selected_ids'] = []
        for row in result['decisions']:
            if row['status'] == 'applicable':
                row['status_before_failure'] = row['status']
                row.update(status='insufficient_information', reason_code='model_batch_error')
    if work is not None:
        result_hash = canonical_hash(result)
        _write_json(Path(work) / 'results' / (input_hash + '-' + result_hash + '.json'), result)
    return result
