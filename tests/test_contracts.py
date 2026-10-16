import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT.parent / 'docs' / 'contracts'

def load(name):
    return json.loads((ROOT / 'contracts' / name).read_text(encoding='utf-8-sig'))

@pytest.mark.parametrize('kind', ['edit-job', 'timeline'])
def test_original_contract_preserved(kind):
    name = kind + '-v1.schema.json'
    assert (ROOT / 'contracts' / name).read_bytes() == (DOCS / name).read_bytes()
    Draft202012Validator.check_schema(load(name))

@pytest.mark.parametrize('name', sorted(p.name for p in (DOCS / 'examples').glob('*.json')))
def test_delivered_examples(name):
    assert (ROOT / 'contracts/examples' / name).read_bytes() == (DOCS / 'examples' / name).read_bytes()
    data = load('examples/' + name)
    Draft202012Validator(load(data['schema_version'].split('/')[0] + '-v1.schema.json')).validate(data)

@pytest.mark.parametrize('field,value', [('speed', 1.1), ('boundary_evidence_ref', None), ('lip_result_ref', 'fake')])
def test_locked_video_refuses_invalid_fields(field, value):
    data = load('examples/timeline-ab.example.json')
    data['video_clips'][1][field] = value
    assert list(Draft202012Validator(load('timeline-v1.schema.json')).iter_errors(data))

@pytest.mark.parametrize('case', ['unknown', 'locked_missing', 'paid_zero', 'badge', 'parent', 'output'])
def test_job_refusals(case):
    data = copy.deepcopy(load('examples/edit-job-ab.example.json'))
    if case == 'unknown': data['extra'] = True
    if case == 'locked_missing': data['assets'][1]['locked'] = None
    if case == 'paid_zero': data['cost']['paid_enabled'] = True
    if case == 'badge': data['templates']['product_badge']['enabled'] = True
    if case == 'parent': data['parent_revision'] = 1
    if case == 'output': data['output']['width'] = 1376
    assert list(Draft202012Validator(load('edit-job-v1.schema.json')).iter_errors(data))
