"""Counterexamples for edit-job/v2 validation; written before implementation."""
import copy, json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def v2_job():
    base = json.loads((ROOT / 'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8'))
    base['schema_version'] = 'edit-job/v2'
    base['script_input'] = {'mode': 'pasted', 'script_text': '大家好，我是老X。今天看一款火龙果。打开包装，里面果实完整。',
                            'script_sha256': __import__('hashlib').sha256('大家好，我是老X。今天看一款火龙果。打开包装，里面果实完整。'.encode()).hexdigest()}
    base['narration_input'] = {'mode': 'auto', 'preset': 'laox_warming_compact', 'uploaded': None, 'history': None}
    for a in base['assets']:
        a['role'] = 'fixed_closing' if a['mode'] == 'spoken_locked' else 'product'
        a['analysis'] = {'mode': 'single_shot'}
    base['v2_overrides'] = {'clip_replacements': [], 'sentence_redubs': []}
    return base


def test_v2_schema_exists_and_accepts_minimal_job():
    schema = json.loads((ROOT / 'contracts/edit-job-v2.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    from scheduler.contracts import validate_job
    validate_job(v2_job())


def test_v1_contract_unchanged_for_v1_payloads():
    from scheduler.contracts import validate_job
    job = json.loads((ROOT / 'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8'))
    assert validate_job(job)['schema_version'] == 'edit-job/v1'
    with pytest.raises(ValueError):
        validate_job({**job, 'script_input': {}})  # v1 rejects unknown fields


def test_pasted_script_requires_text_and_matching_hash():
    from scheduler.contracts import validate_job
    job = v2_job(); job['script_input']['script_text'] = None
    with pytest.raises(ValueError): validate_job(job)
    job = v2_job(); job['script_input']['script_sha256'] = '0' * 64
    with pytest.raises(ValueError): validate_job(job)


def test_narration_mode_must_match_payload_no_silent_fallback():
    from scheduler.contracts import validate_job
    job = v2_job(); job['narration_input']['mode'] = 'uploaded'  # no uploaded ref
    with pytest.raises(ValueError): validate_job(job)
    job = v2_job(); job['narration_input'].update(mode='history', history={'job_id': 'x', 'revision': 1, 'narration_sha256': 'a' * 64}, uploaded={'path': 'p', 'sha256': 'b' * 64})
    with pytest.raises(ValueError): validate_job(job)
    job = v2_job(); job['narration_input']['mode'] = 'generated'
    with pytest.raises(ValueError): validate_job(job)


def test_fixed_roles_require_locked_mode_and_product_requires_broll():
    from scheduler.contracts import validate_job
    job = v2_job()
    job['assets'][1].update(role='fixed_closing', mode='broll', locked=None)
    with pytest.raises(ValueError): validate_job(job)
    job = v2_job(); job['assets'][0].update(role='fixed_opening', mode='broll', locked=None)
    with pytest.raises(ValueError): validate_job(job)
    job = v2_job(); job['assets'][0].update(role='locked_body')
    with pytest.raises(ValueError): validate_job(job)
    job = v2_job(); job['assets'][0]['analysis']['mode'] = 'both'
    with pytest.raises(ValueError): validate_job(job)


def test_narration_plan_schema_guards():
    schema = json.loads((ROOT / 'contracts/narration-plan-v1.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    good = {
        'schema_version': 'narration-plan/v1', 'job_id': 'j', 'revision': 1,
        'source': {'mode': 'uploaded', 'preset': None, 'voice': 'laox', 'audio_path': 'x.wav',
                   'audio_sha256': 'a' * 64, 'actual_duration_ticks': 48000,
                   'origin': {'kind': 'user_imported_audio', 'ref': None}, 'config_hash': None},
        'script': {'script_sha256': 'b' * 64, 'sentence_count': 2},
        'sentences': [{'sentence_id': 's001', 'in_tick': 0, 'out_tick': 24000,
                       'alignment_evidence_sha256': 'c' * 64, 'status': 'aligned'},
                      {'sentence_id': 's002', 'in_tick': None, 'out_tick': None,
                       'alignment_evidence_sha256': None, 'status': 'unknown'}],
        'confirmation': None,
    }
    Draft202012Validator(schema).validate(good)
    bad = copy.deepcopy(good); bad['source']['mode'] = 'cloud'
    assert list(Draft202012Validator(schema).iter_errors(bad))
    bad = copy.deepcopy(good); bad['sentences'][0]['status'] = 'guessed'
    assert list(Draft202012Validator(schema).iter_errors(bad))
