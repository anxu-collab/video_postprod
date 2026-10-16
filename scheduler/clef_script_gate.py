"""Selling-script structure gate. Clef reads the script text before any narration
and returns a probability for each allowed option. The gate keeps those
probabilities. It does not ask for, store, or follow a rewrite.

An efficacy mark is not a fact verdict. It stops narration until a person
records EFFICACY_REVIEW for this exact script.
"""
import json

RULE_VERSION = 'selling-script-structure/v2'

# First option is the one that allows the next stage. Mock mode uses that order.
QUESTIONS = {
    'opening': {
        'type': 'choice',
        'instructions': '看 opening_text，这是按口播语速折算的前3秒，不是成片。它有没有具体问题、矛盾、使用场景或明确吸引点？只在选项里给概率，不要写修改意见。',
        'criteria': {
            'holds': '前3秒有具体问题、矛盾、使用场景或明确吸引点',
            'name_or_look_only': '前3秒只是在报产品名、念包装或描述外观',
        },
    },
    'need': {
        'type': 'choice',
        'instructions': '目标消费者为什么要关注这个产品，有没有说清？定位若是纯开箱展示，把包装看清楚可以成立；定位若是完整销售口播，只讲产品本身算没说清。只在选项里给概率，不要写修改意见。',
        'criteria': {
            'clear': '说清了目标消费者为什么要关注它',
            'unclear': '讲了产品，却没说清目标消费者为什么要关注它',
        },
    },
    'value': {
        'type': 'choice',
        'instructions': '全片有没有讲清产品满足什么合理需求，或资料里写明的功能？主要在介绍包装、颜色、原料名称，没有实质产品价值，算功能表达不足。只在选项里给概率，不要写修改意见。',
        'criteria': {
            'stated': '讲清了合理需求或可核实的特点',
            'list_only': '只罗列原料、包装或使用方式',
        },
    },
    'logic': {
        'type': 'choice',
        'instructions': '前面提出的问题、矛盾或场景，和后面的产品卖点有没有实际关系？出现了某类生活场景不等于关系已经建立。只在选项里给概率，不要写修改意见。',
        'criteria': {
            'connected': '前面的问题与后面的卖点有实际关系',
            'broken': '问题与卖点没有实际关系，强行转到产品',
        },
    },
    'substance': {
        'type': 'choice',
        'instructions': '后一句有没有比前一句增加信息？定位若是纯开箱展示，持续描述外观可以成立；定位若是完整销售口播，连续换词重复同一信息或大量空话算空转。只在选项里给概率，不要写修改意见。',
        'criteria': {
            'advances': '句子在往前增加信息',
            'spins': '连续几句换词重复同一信息，或大量没有实质信息的空话',
        },
    },
    'ending': {
        'type': 'choice',
        'instructions': '说到最后，有没有把一个完整意思说完？只在选项里给概率，不要写修改意见。',
        'criteria': {
            'complete': '结尾形成了完整意思',
            'abrupt': '结尾直接报产品名、喊口号或生硬要求互动，完整意思没有说完',
        },
    },
    'efficacy_risk': {
        'type': 'choice',
        'instructions': '口播有没有用血管、河道冲开或其他画面，暗示未经证实的功效？只标记有或没有。不判断暗示是否真实，不代替事实核查，不要写修改意见。',
        'criteria': {
            'absent': '没有这类功效暗示',
            'marked': '有血管、河道冲开或其他未证实功效暗示',
        },
    },
}

def _opening(script_text, chars_per_second):
    if chars_per_second:
        return script_text[:max(1, int(3 * chars_per_second))]
    return script_text.split('。')[0]


def build_state(script_plan, job, chars_per_second=None, fact_map=None):
    text = script_plan['script_text']
    rows = (fact_map or {}).get('sentences') or [{'sentence_id': s['sentence_id'], 'text': s['text'], 'ingredients': [], 'facts': []}
                                                 for s in script_plan['sentences']]
    return {
        'positioning': (job.get('intent') or {}).get('brief') or '',
        'product_terms': list((job.get('intent') or {}).get('product_terms') or []),
        'opening_text': _opening(text, chars_per_second),
        'script_text': text,
        'sentences': rows,
    }


def _health_script(script_rules, facts):
    return bool(facts.get('ingredients')) or any('保健品带货口播' in r for r in (script_rules or []))


def sentence_questions(fact_map):
    """One probability question per sentence. A pass on another sentence is not an option here."""
    questions = {}
    for row in fact_map.get('sentences') or []:
        ingredients = '、'.join(row['ingredients']) if row['ingredients'] else '无'
        facts = '、'.join(row['facts']) if row['facts'] else '无'
        questions['sent_%s_info' % row['sentence_id']] = {
            'type': 'choice',
            'instructions': '只判断这一句。程序标注成分：%s。资料已匹配的成分作用：%s。句子：%s。装饰、空泛、重复，或只写外观、声音、手感，算无效。只在选项里给概率，不要写修改意见。'
                            % (ingredients, facts, row['text']),
            'criteria': {'informative': '提供了必要的新信息', 'empty': '没有提供必要信息'},
        }
    return questions


def link_questions(fact_map, positioning):
    """Asked only when the document itself states that ingredient's function."""
    questions = {}
    seen = []
    for row in fact_map.get('ingredients') or []:
        if not row.get('function') or row.get('scope') == '研究':
            continue
        seen.append(row)
    for index, row in enumerate(seen):
        questions['ing_%d_link' % index] = {
            'type': 'choice',
            'instructions': '成分%s。资料写明的作用是「%s」，这不是医学判断。本条需求：%s。这个作用说明与该需求有没有实际联系？只在选项里给概率，不要写修改意见。'
                            % (row['name'], row['function'], positioning or '未写'),
            'criteria': {'linked': '作用说明与本条消费需求有联系', 'detached': '有作用说明，但没建立与本条需求的联系'},
        }
    return questions


def gate_questions(fact_map, script_rules, positioning):
    facts = {'ingredients': (fact_map or {}).get('ingredients') or []}
    if not _health_script(script_rules, facts):
        return dict(QUESTIONS)
    merged = dict(QUESTIONS)
    merged.update(sentence_questions(fact_map))
    merged.update(link_questions(fact_map, positioning))
    return merged


def _block_option(question):
    for key in ('name_or_look_only', 'unclear', 'list_only', 'broken', 'spins', 'abrupt', 'empty', 'detached'):
        if key in question['criteria']:
            return key
    return None


_HANDLING = {'empty': '删除或替换', 'detached': '返修', 'list_only': '整稿返修'}


def _probabilities(answer):
    if not isinstance(answer, dict):
        return {}
    raw = answer.get('probabilities')
    if not isinstance(raw, dict):
        raw = answer.get('scores')
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {}
        out[str(key)] = float(value)
    return out


def interpret(response, script_sha256, questions=None):
    """Decide from option probabilities only. A tie or a missing probability does not pass.
    Each question is judged alone; a high score on another sentence does not clear it."""
    questions = questions or QUESTIONS
    answers = (response or {}).get('answers') if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        answers = {}
    kept = {}
    hits = []
    unread = []
    for qid, question in questions.items():
        probs = _probabilities(answers.get(qid))
        if set(probs) != set(question['criteria']):
            unread.append(qid)
            continue
        kept[qid] = {'probabilities': probs}
        block = _block_option(question)
        if block is None:
            continue
        allow = next(iter(question['criteria']))
        if probs[block] >= probs[allow]:
            hit = {'question_id': qid, 'option': block, 'probabilities': probs}
            if block in _HANDLING:
                hit['handling'] = _HANDLING[block]
            hits.append(hit)
    efficacy = kept.get('efficacy_risk', {}).get('probabilities', {})
    marked = bool(efficacy) and efficacy.get('marked', 0) >= efficacy.get('absent', 1)
    if unread:
        status = 'NEEDS_REVIEW'
    elif hits:
        status = 'REPAIR'
    elif marked:
        status = 'EFFICACY_HOLD'
    else:
        status = 'PASS'
    return {
        'schema_version': 'script-structure/v1',
        'rule_version': RULE_VERSION,
        'script_sha256': script_sha256,
        'status': status,
        'hits': hits,
        'unread': unread,
        'hard_blocks': [],
        'efficacy_risk': marked,
        'answers': kept,
    }


def _apply_blocks(verdict, blocks):
    """Keep both judgments. A fact block does not erase Clef, and a Clef pass does not clear facts."""
    verdict['clef_status'] = verdict['status']
    verdict['fact_status'] = 'HARD_BLOCK' if blocks else 'PASS'
    verdict['hard_blocks'] = blocks
    if blocks:
        verdict['status'] = 'HARD_BLOCK'
    return verdict


def review_selling_script(adapter, script_plan, job, repository=None, chars_per_second=None, documents=None, script_rules=()):
    from scheduler.selling_script_facts import parse_product_facts, sentence_fact_map
    facts = {'ingredients': [], 'allowed_functions': [], 'product_names': []}
    for text in (documents or {}).values():
        parsed = parse_product_facts(text)
        facts['ingredients'].extend(parsed['ingredients'])
        facts['allowed_functions'].extend(parsed['allowed_functions'])
        facts['product_names'].extend(parsed['product_names'])
    fact_map = sentence_fact_map(script_plan['sentences'], facts)
    positioning = (job.get('intent') or {}).get('brief') or ''
    questions = gate_questions(fact_map, script_rules, positioning)
    state = build_state(script_plan, job, chars_per_second, fact_map)
    try:
        response = adapter.classify(state, questions, repository)
    except Exception as exc:
        verdict = interpret({}, script_plan['script_sha256'], questions)
        verdict.update(error=type(exc).__name__ + ':' + str(exc), state=state, fact_map=fact_map)
        return _apply_blocks(verdict, fact_map['hard_blocks'])
    verdict = interpret(response, script_plan['script_sha256'], questions)
    verdict.update(state=state, fact_map=fact_map)
    return _apply_blocks(verdict, fact_map['hard_blocks'])


def block_message(verdict):
    payload = {'status': verdict['status'], 'clef_status': verdict.get('clef_status'),
               'fact_status': verdict.get('fact_status'), 'hits': verdict['hits'], 'unread': verdict['unread'],
               'hard_blocks': verdict.get('hard_blocks') or [], 'efficacy_risk': verdict['efficacy_risk']}
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
