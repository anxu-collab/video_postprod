import hashlib,json
from pathlib import Path
from jsonschema import Draft202012Validator
ROOT=Path(__file__).resolve().parents[1]

def _load(name):
    return json.loads((ROOT/'contracts'/f'{name}.schema.json').read_text(encoding='utf-8'))

def validate(data,kind):
    schema=_load(f'{kind}-v1')
    errors=list(Draft202012Validator(schema).iter_errors(data))
    if errors:raise ValueError('CONTRACT_INVALID:'+str(errors[0].message))
    return data

def validate_named(data,name):
    """Validate against a fully named schema file, e.g. 'edit-job-v2' or 'narration-plan-v1'."""
    schema=_load(name)
    errors=list(Draft202012Validator(schema).iter_errors(data))
    if errors:raise ValueError('CONTRACT_INVALID:'+str(errors[0].message))
    return data

def canonical_hash(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def _common_job_checks(data):
    if any(a['mode']=='avatar_redub' for a in data['assets']):raise ValueError('CAPABILITY_UNAVAILABLE_LIPSYNC')
    ids=[a['asset_id'] for a in data['assets']]
    if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_ASSET_ID')
    docs={d['document_id'] for d in data['product_documents']}
    if len(docs)!=len(data['product_documents']):raise ValueError('DUPLICATE_DOCUMENT_ID')
    claims=[c['claim_id'] for c in data['claims']]
    if len(claims)!=len(set(claims)) or any(c['document_id'] not in docs for c in data['claims']):raise ValueError('CLAIM_DOCUMENT_INVALID')

def _v2_job_checks(data):
    si=data['script_input']
    if si['mode']=='pasted':
        if hashlib.sha256(si['script_text'].encode('utf-8')).hexdigest()!=si['script_sha256']:raise ValueError('SCRIPT_HASH_MISMATCH')
    roles=[a['role'] for a in data['assets']]
    for fixed in ['fixed_opening','fixed_closing']:
        if roles.count(fixed)>1:raise ValueError('DUPLICATE_FIXED_ROLE:'+fixed)
    cr=data['v2_overrides']['clip_replacements']
    if len({c['clip_id'] for c in cr})!=len(cr):raise ValueError('DUPLICATE_CLIP_REPLACEMENT')
    sr=data['v2_overrides']['sentence_redubs']
    if len({s['sentence_id'] for s in sr})!=len(sr):raise ValueError('DUPLICATE_SENTENCE_REDUB')

def validate_job(data):
    version=data.get('schema_version')
    if version=='edit-job/v1':
        validate(data,'edit-job')
        _common_job_checks(data)
        o=data['overrides']
        if any(o[k] for k in ['subtitle_edits','shot_edits','speech_edits']) and not o['base_timeline_sha256']:raise ValueError('BASE_TIMELINE_HASH_REQUIRED')
        if data['templates']['effects'] or data['templates']['sfx'] or data['templates']['product_badge']['enabled'] or o['template_overrides']:raise ValueError('TEMPLATE_CAPABILITY_UNAVAILABLE')
        return data
    if version=='edit-job/v2':
        validate_named(data,'edit-job-v2')
        _common_job_checks(data);_v2_job_checks(data)
        o=data['overrides']
        if o['subtitle_edits'] and not o['base_timeline_sha256']:raise ValueError('BASE_TIMELINE_HASH_REQUIRED')
        if data['templates']['effects'] or data['templates']['sfx'] or data['templates']['product_badge']['enabled'] or o['template_overrides']:raise ValueError('TEMPLATE_CAPABILITY_UNAVAILABLE')
        if o['shot_edits'] or o['speech_edits']:raise ValueError('V1_SHOT_SPEECH_OVERRIDES_USE_V2')
        return data
    raise ValueError('UNSUPPORTED_JOB_SCHEMA_VERSION:'+str(version))
