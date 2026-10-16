"""Program-side map for a health-product selling script.

Sentences are split by the script plan. This module tags ingredient names and
matches them to the product document so Clef can review content and structure.
Naming an ingredient that has no written function, or saying both documented
product functions, is not a block. The person who reads the narration decides
whether it passes. A sentence that turns research wording into a human effect
is still blocked. This module does not decide whether an effect is medically true.
"""
import re

_UNGROUNDED = ('研究', '临床', '实验', '证明', '治疗', '治愈', '降幅')


def parse_product_facts(text):
    """Read only lines the product document actually contains."""
    ingredients = []
    functions = []
    product_names = []
    stated = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if not product_names and '（' in line and not line.startswith(('剂型', '吃法', '成分', '功效', '人群', '卖点', '包装', '收尾')):
            product_names.append(line.split('（', 1)[0].strip())
        if line.startswith('成分：') or line.startswith('成分:'):
            body = re.split(r'[:：]', line, maxsplit=1)[1].split('。', 1)[0]
            ingredients = [part.strip() for part in re.split(r'[、,，]', body) if part.strip()]
        elif line.startswith('功效：') or line.startswith('功效:'):
            body = re.split(r'[:：]', line, maxsplit=1)[1].split('。', 1)[0]
            functions = [part.strip() for part in re.split(r'[、,，]', body) if part.strip()]
        elif line.startswith('成分作用：') or line.startswith('成分作用:'):
            body = re.split(r'[:：]', line, maxsplit=1)[1]
            name, _, rest = body.partition('＝')
            function, _, scope = rest.partition('｜')
            if name.strip() and function.strip():
                stated[name.strip()] = {'function': function.strip(), 'scope': scope.strip() or 'product'}
    rows = []
    for name in ingredients:
        extra = stated.get(name) or {}
        rows.append({'name': name, 'function': extra.get('function'), 'scope': extra.get('scope') or 'undocumented',
                     'evidence': 'product_document' if extra.get('function') else None})
    return {'ingredients': rows, 'allowed_functions': functions, 'product_names': [n for n in product_names if n]}


def _mask(text, names):
    masked = text
    for name in sorted(names, key=len, reverse=True):
        masked = masked.replace(name, ' ')
    return masked


def sentence_fact_map(sentences, facts):
    names = [row['name'] for row in facts.get('ingredients') or []]
    product_names = facts.get('product_names') or []
    by_name = {row['name']: row for row in facts.get('ingredients') or []}
    tagged = []
    blocks = []
    for sentence in sentences:
        visible = _mask(sentence['text'], product_names)
        found = [name for name in sorted(names, key=len, reverse=True) if name in visible]
        tagged.append({'sentence_id': sentence['sentence_id'], 'text': sentence['text'],
                       'ingredients': found, 'facts': []})
        for name in found:
            row = by_name[name]
            if row['function'] and row['function'] in sentence['text'] and row['scope'] != '研究':
                tagged[-1]['facts'].append(name)
            elif any(mark in sentence['text'] for mark in _UNGROUNDED):
                blocks.append({'code': 'CLAIM_WITHOUT_EVIDENCE', 'handling': '硬拦截', 'ingredient': name,
                               'sentence_id': sentence['sentence_id'],
                               'reason': '句子把研究、临床、实验或治疗说法和成分放在一起'})
    return {'sentences': tagged, 'hard_blocks': blocks, 'ingredients': facts.get('ingredients') or []}
