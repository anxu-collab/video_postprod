import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def copy_library(tmp_path):
    manifest = json.loads((ROOT / 'assets/script_expressions/health-supplement/approval-20261014-06.json').read_text(encoding='utf-8'))
    paths = [item['path'] for item in manifest['approved_sets']]
    paths += ['assets/script_expressions/health-supplement/approval-20261014-06.json', 'config/script_expression_catalog.json']
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    return tmp_path


def reseal_test_approval(root, relative, data):
    """A test-only new trusted manifest exercises checks beneath file integrity."""
    path = root / relative
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    manifest_path = root / 'assets/script_expressions/health-supplement/approval-20261014-06.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for spec in manifest['approved_sets']:
        if spec['path'] == relative:
            spec['approved_file_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    config_path = root / 'config/script_expression_catalog.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['approval_manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown', 'pending', 'parent_reference'])
def test_manifest_membership_and_parent_references_checked_after_hashes(tmp_path, mutation):
    from scheduler.expression_catalog import load_catalog
    root = copy_library(tmp_path)
    relative = 'assets/script_expressions/health-supplement/painpoint-summaries-20261014-05.json'
    data = json.loads((root / relative).read_text(encoding='utf-8'))
    if mutation == 'duplicate': data['entries'][1]['id'] = data['entries'][0]['id']
    if mutation == 'unknown': data['entries'][0]['id'] = 'UNKNOWN-APPROVED'
    if mutation == 'pending': data['entries'][0]['status'] = 'pending'
    if mutation == 'parent_reference': data['entries'][0]['painpoint_ref'] = 'PAIN-MISSING'
    reseal_test_approval(root, relative, data)
    with pytest.raises(ValueError): load_catalog(root)


def test_full_approved_catalog_and_inherited_clinical_context():
    from scheduler.expression_catalog import load_catalog
    catalog = load_catalog()
    assert Counter(e['kind'] for e in catalog['entries']) == {
        'expression': 40, 'pattern': 4, 'observation': 22, 'usage': 7, 'painpoint': 14, 'summary': 42}
    by_id = {e['id']: e for e in catalog['entries']}
    assert len(by_id) == 129
    assert by_id['B05']['text'] == '为细胞产生能量'
    assert by_id['B05']['category'] == 'unclassified'
    assert by_id['C04']['kind'] == 'pattern'
    assert by_id['SHORT-KIDNEY-001-1']['context']['parent']['id'] == 'PAIN-KIDNEY-001'
    assert by_id['SHORT-KIDNEY-001-1']['context']['observations']
    assert '早期通常没有症状' in by_id['SHORT-KIDNEY-001-1']['context']['observations'][0]['context']['source']['context']
    assert all(e['approval']['status'] == 'approved' for e in by_id.values())
    assert all(e['approval']['medical_verification_claimed'] is False for e in by_id.values())


@pytest.mark.parametrize('mutation', ['text', 'new_approved_id', 'pending', 'missing_parent', 'manifest', 'traversal'])
def test_modified_or_unapproved_library_is_rejected(tmp_path, mutation):
    from scheduler.expression_catalog import load_catalog
    root = copy_library(tmp_path)
    path = root / 'assets/script_expressions/health-supplement/painpoints-20261014-04.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    if mutation == 'text': data['cards'][0]['spoken_candidate'] += '篡改'
    if mutation == 'new_approved_id': data['cards'].append(dict(data['cards'][0], id='NEW-UNAPPROVED'))
    if mutation == 'pending': data['cards'][0]['status'] = 'pending'
    if mutation == 'missing_parent': data['cards'].pop(0)
    if mutation in ('manifest', 'traversal'):
        path = root / 'assets/script_expressions/health-supplement/approval-20261014-06.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['approved_sets'][0]['path'] = '../outside.json' if mutation == 'traversal' else 'unknown.json'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError): load_catalog(root)

