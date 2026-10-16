import copy
import hashlib
import json

import pytest


@pytest.fixture
def skill_registry(tmp_path, monkeypatch):
    from scheduler import script_skill as sk
    root = tmp_path / 'project'
    folder = root / '.agents/skills/demo'
    folder.mkdir(parents=True)
    rules = folder / 'rules.txt'
    rules.write_text('按使用动作介绍商品，不添加健康功效。', encoding='utf-8')
    registry = root / 'config/script_skills.json'
    registry.parent.mkdir()
    data = {'schema_version': 'script-skill-registry/v1', 'skills': [
        {'id': 'demo', 'version': '1.0.0', 'name': '演示规则', 'description': '隔离测试用途',
         'rules_path': '.agents/skills/demo/rules.txt'}]}
    registry.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(sk, 'ROOT', root)
    monkeypatch.setattr(sk, 'REGISTRY', registry)
    monkeypatch.setattr(sk, 'SNAPSHOTS', root / 'state/script-skills')
    return sk, data, rules


def test_snapshot_is_immutable_and_public_catalog_is_path_free(skill_registry):
    sk, data, rules = skill_registry
    binding = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    snapshot = sk.load_skill(binding)
    assert snapshot['rules'] == rules.read_text(encoding='utf-8')
    rules.write_text('修改后的规则', encoding='utf-8')
    assert sk.load_skill(binding) == snapshot
    assert sk.pin_skill(binding) == binding
    public = sk.list_skills()
    assert public[0]['id'] == 'demo'
    assert 'rules_path' not in public[0] and 'rules' not in public[0]


@pytest.mark.parametrize('selection,code', [
    ({'id': 'missing', 'version': '1.0.0'}, 'SCRIPT_SKILL_UNKNOWN'),
    ({'id': 'demo', 'version': '2.0.0'}, 'SCRIPT_SKILL_UNKNOWN'),
    ({'id': '../demo', 'version': '1.0.0'}, 'SCRIPT_SKILL_INVALID'),
    ({'id': 'demo', 'version': '1.0.0', 'rules': 'override'}, 'SCRIPT_SKILL_INVALID'),
    ({'id': 'demo', 'version': '1.0.0', 'snapshot_sha256': '0'*64}, 'SCRIPT_SKILL_SNAPSHOT_MISSING'),
])
def test_invalid_selection_fails(skill_registry, selection, code):
    sk, _, _ = skill_registry
    with pytest.raises(ValueError, match=code): sk.pin_skill(selection)


def test_snapshot_tamper_and_identity_mismatch_fail(skill_registry):
    sk, _, _ = skill_registry
    binding = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    with pytest.raises(ValueError, match='SCRIPT_SKILL_SNAPSHOT'):
        sk.load_skill(dict(binding, id='another'))
    path = sk.SNAPSHOTS / (binding['snapshot_sha256'] + '.json')
    data = json.loads(path.read_text(encoding='utf-8')); data['rules'] = '伪造'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='SCRIPT_SKILL_SNAPSHOT_CORRUPTED'): sk.load_skill(binding)


@pytest.mark.parametrize('change', ['duplicate', 'escape', 'empty'])
def test_bad_registry_is_rejected(skill_registry, change):
    sk, data, rules = skill_registry
    if change == 'duplicate': data['skills'].append(copy.deepcopy(data['skills'][0]))
    elif change == 'escape': data['skills'][0]['rules_path'] = '../../outside.txt'
    else: rules.write_text(' ', encoding='utf-8')
    sk.REGISTRY.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='SCRIPT_SKILL'): sk.list_skills()


def test_requirement_only_applies_to_generation(skill_registry):
    sk, _, _ = skill_registry
    sk.validate_script_skill_requirement({'script_input': {'mode': 'pasted'}})
    with pytest.raises(ValueError, match='SCRIPT_SKILL_REQUIRED'):
        sk.validate_script_skill_requirement({'script_input': {'mode': 'generated'}})


@pytest.mark.parametrize('raw', [
    '[]', 'null', '{"schema_version":"script-skill-registry/v1","skills":[null]}',
    '{"schema_version":"script-skill-registry/v1","skills":[[]]}',
    '{"schema_version":"script-skill-registry/v1","skills":[{"id":[],"version":"1.0.0",'
    '"name":"n","description":"d","rules_path":".agents/skills/demo/rules.txt"}]}',
    '{"schema_version":"script-skill-registry/v1","skills":[],"skills":[]}',
])
def test_registry_json_types_and_duplicate_json_keys_are_controlled(skill_registry, raw):
    sk, _, _ = skill_registry
    sk.REGISTRY.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError, match='SCRIPT_SKILL_REGISTRY_INVALID'):
        sk.list_skills()


def test_registry_rejects_symlink_resolved_outside_skills(skill_registry, tmp_path):
    sk, data, _ = skill_registry
    outside = tmp_path / 'outside.txt'
    outside.write_text('外部规则', encoding='utf-8')
    link = sk.ROOT / '.agents/skills/demo/external.txt'
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation is unavailable')
    data['skills'][0]['rules_path'] = '.agents/skills/demo/external.txt'
    sk.REGISTRY.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='SCRIPT_SKILL_PATH_INVALID'):
        sk.list_skills()


def test_registry_rejects_skills_root_symlink_outside_project(skill_registry, tmp_path):
    sk, _, _ = skill_registry
    outside = tmp_path / 'external-skills'
    outside.mkdir()
    (outside / 'rules.txt').write_text('外部规则', encoding='utf-8')
    old_skills = sk.ROOT / '.agents/skills'
    old_skills.rename(sk.ROOT / '.agents/skills-original')
    try:
        old_skills.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        (sk.ROOT / '.agents/skills-original').rename(old_skills)
        pytest.skip('directory symlink creation is unavailable')
    try:
        with pytest.raises(ValueError, match='SCRIPT_SKILL_PATH_INVALID'):
            sk.list_skills()
    finally:
        old_skills.unlink()
        (sk.ROOT / '.agents/skills-original').rename(old_skills)


@pytest.mark.parametrize('raw', ['[]', 'null', '{"rules":NaN}', '{"id":"demo","id":"demo"}'])
def test_snapshot_malformed_json_types_are_controlled(skill_registry, raw):
    sk, _, _ = skill_registry
    binding = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    path = sk.SNAPSHOTS / (binding['snapshot_sha256'] + '.json')
    path.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError, match='SCRIPT_SKILL_SNAPSHOT_CORRUPTED'):
        sk.load_skill(binding)


def test_snapshot_identity_and_version_must_match_binding(skill_registry):
    sk, _, _ = skill_registry
    binding = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    wrong_version = dict(binding, version='1.0.1')
    with pytest.raises(ValueError, match='SCRIPT_SKILL_SNAPSHOT_CORRUPTED'):
        sk.load_skill(wrong_version)


def test_old_snapshot_survives_current_registry_and_source_changes(skill_registry):
    sk, data, rules = skill_registry
    binding = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    old = sk.load_skill(binding)
    rules.write_text('更新规则', encoding='utf-8')
    data['skills'].clear()
    sk.REGISTRY.write_text(json.dumps(data), encoding='utf-8')
    assert sk.load_skill(binding) == old


def test_snapshot_records_raw_source_hash_and_normalized_rules_hash(skill_registry):
    sk, _, rules = skill_registry
    raw = b'\xef\xbb\xbf\xe7\xac\xac\xe4\xb8\x80\xe8\xa1\x8c\r\n\xe7\xac\xac\xe4\xba\x8c\xe8\xa1\x8c\r\n'
    rules.write_bytes(raw)
    binding = sk.pin_skill({'id': 'demo', 'version': '1.0.0'})
    snapshot = sk.load_skill(binding)
    assert snapshot['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert snapshot['rules'] == '第一行\n第二行\n'
    assert snapshot['rules_sha256'] == hashlib.sha256(snapshot['rules'].encode('utf-8')).hexdigest()


@pytest.mark.parametrize('job', [None, [], {}, {'script_input': []}, {'script_input': {'mode': 'generated', 'skill': []}}])
def test_malformed_requirement_inputs_raise_controlled_value_error(skill_registry, job):
    sk, _, _ = skill_registry
    with pytest.raises(ValueError, match='SCRIPT_SKILL'):
        sk.validate_script_skill_requirement(job)
