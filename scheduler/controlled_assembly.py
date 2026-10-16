"""Deterministic assembly with versioned finite-variant or open-middle polishing guards."""
import hashlib
import itertools
import json
import re
from pathlib import Path

from scheduler.contracts import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
RECIPE_ID = 'tianyiqing-r07-v1'
RECIPES = {'1.2.0': RECIPE_ID, '1.3.0': 'tianyiqing-r07-v2', '1.4.0':'tianyiqing-r07-v3'}


def controlled(skill):
    return skill.get('id') == 'health-supplement-selling' and skill.get('version') in RECIPES


def _read(root, path):
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('ASSEMBLY_SOURCE_OUTSIDE_ROOT')
    raw = resolved.read_bytes()
    return raw.decode('utf-8-sig'), hashlib.sha256(raw).hexdigest()


def _edit(text, edits):
    for old, new in edits:
        if not old or text.count(old) != 1:
            raise ValueError('ASSEMBLY_SOURCE_EDIT_AMBIGUOUS')
        text = text.replace(old, new, 1)
    return text


def assemble(payload, root=ROOT):
    """Resolve only server-owned sources and transformations, before any paid call."""
    recipe_id = RECIPES.get(payload['skill'].get('version'))
    if not controlled(payload['skill']) or payload.get('assembly_recipe') != recipe_id:
        raise ValueError('ASSEMBLY_RECIPE_REQUIRED')
    root = Path(root)
    path = 'config/script_assembly_recipes/' + recipe_id + '.json'
    raw, digest = _read(root, path); recipe = json.loads(raw)
    if (payload['product']['product_id'] != recipe['product_id'] or
            payload['product']['name'] not in recipe['product_names']):
        raise ValueError('ASSEMBLY_PRODUCT_MISMATCH')
    if payload['account'] != recipe['account']:
        raise ValueError('ASSEMBLY_ACCOUNT_MISMATCH')
    if payload.get('entry_ids'):
        raise ValueError('ASSEMBLY_USES_RECIPE_NOT_FREE_ENTRY_SELECTION')
    sources = {}
    files = [{'path':path, 'sha256':digest}]
    for key, source_path in recipe['sources'].items():
        raw, digest = _read(root, source_path)
        sources[key] = json.loads(raw)
        files.append({'path':source_path, 'sha256':digest})
    segments = []
    for spec in recipe['segments']:
        original_parts, choices, refs = [], [], []
        for part in spec['parts']:
            source = sources[part['source']]
            if part['source'] == 'openings':
                text = next(x['corrected'] for x in source['items'] if x['source_id'] == part['id'])
            elif part['source'] == 'cards':
                text = next(x['text'] for x in source['cards'] if x['id'] == part['id'])
            else:
                text = source['ending']['current_tianyiqing_text']
            base = _edit(text, part['edits'])
            original_parts.append(base)
            choices.append(list(dict.fromkeys([base] + [_edit(base, v) for v in part['variants']])))
            refs.append({'id':part['id'], 'path':recipe['sources'][part['source']],
                         'original':text, 'assembly_edits':part['edits'], 'polish_edits':part['variants']})
        segments.append({'id':spec['id'], 'role':spec['role'], 'label':spec['label'], 'mode':spec['mode'],
                         'text':''.join(original_parts), 'sources':refs,
                         'allowed_texts':[''.join(v) for v in itertools.product(*choices)]})
    evidence = []
    for source_path in recipe['evidence_documents']:
        raw, digest = _read(root, source_path)
        evidence.append({'path':source_path, 'sha256':digest, 'text':raw})
    review, digest = _read(root, recipe['review_prompt'])
    files.append({'path':recipe['review_prompt'], 'sha256':digest})
    bundle = {k:recipe[k] for k in ('recipe_id','version','narrative_type','authorization','selection_method',
                                   'required_effects','forbidden_before_cta','evidence_issues')}
    bundle.update(segments=segments, source_files=files, evidence_documents=evidence,
                  review_prompt=review, script_text='\n'.join(s['text'] for s in segments))
    if recipe.get('polish_mode') == 'open_middle':
        bundle.update({k:recipe[k] for k in ('polish_mode','required_middle_terms','effect_subjects')})
        for segment in segments[1:-1]:
            segment.pop('allowed_texts',None)
    if 'repair_policy' in recipe:
        bundle['repair_policy']=recipe['repair_policy']
    bundle['sha256'] = canonical_hash(bundle)
    validate_bundle(bundle)
    check = check_polish(bundle, output_from_text(bundle,bundle['script_text']))
    if not check['passed']:
        raise ValueError('ASSEMBLY_BASE_INVALID:' + ';'.join(check['issues']))
    return bundle


def validate_bundle(bundle):
    if bundle.get('sha256') != canonical_hash({k:v for k,v in bundle.items() if k != 'sha256'}):
        raise ValueError('ASSEMBLY_SNAPSHOT_CHANGED')
    return bundle


def check_polish(bundle, output):
    """Fail closed on changed subjects/qualifiers, even if key phrases still occur."""
    if bundle.get('polish_mode') == 'open_middle':
        return _check_open_middle(bundle,output)
    issues = []
    if not isinstance(output, dict) or set(output) != {'segments'} or not isinstance(output['segments'], list):
        return {'passed':False, 'issues':['润色输出格式错误'], 'facts_verified':False}
    rows = output['segments']; expected = bundle['segments']
    if len(rows) != len(expected): issues.append('段数改变')
    for i, row in enumerate(rows):
        if (i >= len(expected) or not isinstance(row, dict) or set(row) != {'id','text'} or
                row['id'] != expected[i]['id'] or not isinstance(row['text'], str) or
                row['text'] not in expected[i]['allowed_texts']):
            issues.append(f'第{i+1}段超出允许改写范围（包括固定句、作用对象与限定词）')
    text = '\n'.join(r.get('text','') for r in rows if isinstance(r,dict) and isinstance(r.get('text'),str))
    for effect in bundle['required_effects']:
        if effect not in text: issues.append('效果词缺失：'+effect)
    before_cta = '\n'.join(r.get('text','') for r in rows[:-1] if isinstance(r,dict) and isinstance(r.get('text'),str))
    for word in bundle['forbidden_before_cta']:
        if word in before_cta: issues.append('重新添加已删除的人称或情绪：'+word)
    return {'passed':not issues, 'issues':issues, 'facts_verified':False,
            'checks':['段序及职责','固定开头','效果词及限定词','成分与作用对应','固定结尾','仅允许衔接变体']}


def output_from_text(bundle,text):
    lines=text.splitlines()
    if bundle.get('polish_mode') == 'open_middle':
        ids=['hook']+[f'middle-{i}' for i in range(1,len(lines)-1)]+['cta']
    else:
        ids=[s['id'] for s in bundle['segments']]
    # Do not silently truncate surplus manual lines.
    return {'segments':[{'id':ids[i] if i<len(ids) else 'extra','text':line} for i,line in enumerate(lines)]}


def _check_open_middle(bundle,output):
    issues=[]
    result={'passed':False,'issues':issues,'facts_verified':False,'semantic_review_required':True,
            'checks':['固定开头','固定结尾','必要效果词及限定词','产品和成分保留','成分作用字面对应（需语义复核）','中段开放改写']}
    if not isinstance(output,dict) or set(output)!={'segments'} or not isinstance(output['segments'],list):
        issues.append('润色输出格式错误');return result
    rows=output['segments']
    if not 3<=len(rows)<=8:
        issues.append('需保留开头、1至6段中间及结尾');return result
    ids=['hook']+[f'middle-{i}' for i in range(1,len(rows)-1)]+['cta']
    for row,id in zip(rows,ids):
        if (not isinstance(row,dict) or set(row)!={'id','text'} or row['id']!=id or
                not isinstance(row['text'],str) or not row['text'].strip() or
                row['text'].splitlines()!=[row['text']] or len(row['text'])>2000):
            issues.append('段落ID、正文或格式错误');return result
    if rows[0]['text']!=bundle['segments'][0]['text']:issues.append('固定开头改变')
    if rows[-1]['text']!=bundle['segments'][-1]['text']:issues.append('固定结尾改变')
    middle='\n'.join(r['text'] for r in rows[1:-1])
    for word in bundle['required_effects']+bundle['required_middle_terms']:
        if word not in middle:issues.append('中段必要词缺失：'+word)
    for word in bundle['forbidden_before_cta']:
        if word in '\n'.join(r['text'] for r in rows[:-1]):issues.append('重新添加已删除的人称或情绪：'+word)
    # This is a lexical tripwire, not proof of the sentence's semantic claims.
    subjects=[r['subject'] for r in bundle['effect_subjects']]
    for rule in bundle['effect_subjects']:
        for clause in re.split(r'[。！？；;\n]',middle):
            if rule['effect'] not in clause:continue
            for match in re.finditer(re.escape(rule['effect']),clause):
                preceding=[(m.start(),s) for s in subjects for m in re.finditer(re.escape(s),clause[:match.start()])]
                following=[(m.start(),s) for s in subjects for m in re.finditer(re.escape(s),clause[match.end():])]
                nearest=max(preceding)[1] if preceding else min(following)[1] if following else None
                if nearest!=rule['subject']:issues.append('效果词缺少对应成分或作用对象改变：'+rule['effect'])
    result['passed']=not issues
    return result
