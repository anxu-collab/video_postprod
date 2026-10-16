"""Explicit local writing skills. A task consumes a verified immutable snapshot."""
import hashlib
import json
import re
from pathlib import Path

from scheduler.contracts import canonical_hash
from workers.common import atomic_json

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'config/script_skills.json'
SNAPSHOTS = ROOT / 'state/script-skills'
_ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$')
_VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')
_SHA = re.compile(r'^[a-f0-9]{64}$')
_REGISTRY_FIELDS = {'id', 'version', 'name', 'description', 'rules_path'}
_SNAPSHOT_FIELDS = {
    'id', 'version', 'name', 'description', 'rules', 'schema_version',
    'source_sha256', 'rules_sha256',
}


def _reject_constant(_value):
    raise ValueError('non-finite JSON number')


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _read_json(path, *, encoding='utf-8'):
    return json.loads(path.read_text(encoding=encoding), object_pairs_hook=_object_no_duplicates,
                      parse_constant=_reject_constant)


def _snapshot_path(sha):
    try:
        root = ROOT.resolve()
        folder = SNAPSHOTS.resolve(strict=False)
        path = (SNAPSHOTS / (sha + '.json')).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError('SCRIPT_SKILL_SNAPSHOT_PATH_INVALID') from exc
    if not folder.is_relative_to(root) or not path.is_relative_to(folder):
        raise ValueError('SCRIPT_SKILL_SNAPSHOT_PATH_INVALID')
    return SNAPSHOTS / (sha + '.json')


def _selection(value):
    if (not isinstance(value, dict) or set(value) - {'id', 'version', 'snapshot_sha256'}
            or not isinstance(value.get('id'), str) or not _ID.fullmatch(value['id'])
            or not isinstance(value.get('version'), str) or not _VERSION.fullmatch(value['version'])
            or ('snapshot_sha256' in value and
                (not isinstance(value['snapshot_sha256'], str)
                 or not _SHA.fullmatch(value['snapshot_sha256'])))):
        raise ValueError('SCRIPT_SKILL_INVALID')
    return value


def _entries():
    try:
        registry_path = REGISTRY.resolve(strict=True)
        if not registry_path.is_relative_to(ROOT.resolve()) or not registry_path.is_file():
            raise ValueError('SCRIPT_SKILL_REGISTRY_INVALID')
        data = _read_json(REGISTRY, encoding='utf-8-sig')
    except (OSError, RuntimeError, ValueError, RecursionError) as exc:
        if str(exc) == 'SCRIPT_SKILL_REGISTRY_INVALID':
            raise
        raise ValueError('SCRIPT_SKILL_REGISTRY_INVALID') from exc
    if (not isinstance(data, dict) or data.get('schema_version') != 'script-skill-registry/v1'
            or not isinstance(data.get('skills'), list)):
        raise ValueError('SCRIPT_SKILL_REGISTRY_INVALID')

    root = ROOT.resolve()
    try:
        skills_root = (ROOT / '.agents/skills').resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError('SCRIPT_SKILL_PATH_INVALID') from exc
    if not skills_root.is_relative_to(root) or not skills_root.is_dir():
        raise ValueError('SCRIPT_SKILL_PATH_INVALID')

    seen, entries = set(), []
    for entry in data['skills']:
        if not isinstance(entry, dict) or set(entry) != _REGISTRY_FIELDS:
            raise ValueError('SCRIPT_SKILL_REGISTRY_INVALID')
        try:
            _selection({'id': entry.get('id'), 'version': entry.get('version')})
        except ValueError as exc:
            raise ValueError('SCRIPT_SKILL_REGISTRY_INVALID') from exc
        key = (entry['id'], entry['version'])
        if key in seen:
            raise ValueError('SCRIPT_SKILL_DUPLICATE')
        seen.add(key)
        if any(not isinstance(entry.get(k), str) or not entry[k].strip()
               for k in ('name', 'description', 'rules_path')):
            raise ValueError('SCRIPT_SKILL_REGISTRY_INVALID')

        try:
            relative = Path(entry['rules_path'])
            if relative.is_absolute():
                raise ValueError('SCRIPT_SKILL_PATH_INVALID')
            path = (ROOT / relative).resolve(strict=True)
        except ValueError as exc:
            if str(exc) == 'SCRIPT_SKILL_PATH_INVALID':
                raise
            raise ValueError('SCRIPT_SKILL_PATH_INVALID') from exc
        except (OSError, RuntimeError) as exc:
            raise ValueError('SCRIPT_SKILL_RULES_MISSING') from exc
        if not path.is_relative_to(skills_root) or not path.is_file():
            raise ValueError('SCRIPT_SKILL_PATH_INVALID')
        try:
            raw_rules = path.read_bytes()
            # Match text-file newline normalization while retaining a separate
            # hash of the exact bytes on disk (including BOM and CRLF).
            rules = raw_rules.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError('SCRIPT_SKILL_RULES_MISSING') from exc
        if not rules.strip():
            raise ValueError('SCRIPT_SKILL_RULES_EMPTY')
        entries.append(dict(entry, rules=rules,
                            source_sha256=hashlib.sha256(raw_rules).hexdigest()))
    return entries


def list_skills():
    return [{k: e[k] for k in ('id', 'version', 'name', 'description')} for e in _entries()]


def pin_skill(selection):
    selection = _selection(selection)
    if 'snapshot_sha256' in selection:
        load_skill(selection)
        return dict(selection)
    entry = next((e for e in _entries() if (e['id'], e['version']) ==
                  (selection['id'], selection['version'])), None)
    if entry is None:
        raise ValueError('SCRIPT_SKILL_UNKNOWN')
    snapshot = {k: entry[k] for k in ('id', 'version', 'name', 'description', 'rules')}
    snapshot.update(schema_version='script-skill-snapshot/v1',
                    source_sha256=entry['source_sha256'],
                    rules_sha256=hashlib.sha256(snapshot['rules'].encode('utf-8')).hexdigest())
    try:
        sha = canonical_hash(snapshot)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('SCRIPT_SKILL_REGISTRY_INVALID') from exc
    path = _snapshot_path(sha)
    binding = dict(selection, snapshot_sha256=sha)
    if not path.exists():
        try:
            atomic_json(path, snapshot)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError('SCRIPT_SKILL_SNAPSHOT_WRITE_FAILED') from exc
    load_skill(binding)
    return binding


def load_skill(binding):
    binding = _selection(binding)
    sha = binding.get('snapshot_sha256')
    if not sha:
        raise ValueError('SCRIPT_SKILL_SNAPSHOT_MISSING')
    try:
        data = _read_json(_snapshot_path(sha))
    except FileNotFoundError as exc:
        raise ValueError('SCRIPT_SKILL_SNAPSHOT_MISSING') from exc
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('SCRIPT_SKILL_SNAPSHOT_CORRUPTED') from exc

    if (not isinstance(data, dict) or set(data) != _SNAPSHOT_FIELDS
            or not isinstance(data.get('id'), str) or not _ID.fullmatch(data['id'])
            or not isinstance(data.get('version'), str) or not _VERSION.fullmatch(data['version'])
            or not isinstance(data.get('name'), str) or not data['name'].strip()
            or not isinstance(data.get('description'), str) or not data['description'].strip()
            or not isinstance(data.get('rules'), str) or not data['rules'].strip()
            or not isinstance(data.get('source_sha256'), str)
            or not _SHA.fullmatch(data['source_sha256'])
            or not isinstance(data.get('rules_sha256'), str)
            or not _SHA.fullmatch(data['rules_sha256'])
            or data.get('schema_version') != 'script-skill-snapshot/v1'
            or any(data.get(k) != binding[k] for k in ('id', 'version'))):
        raise ValueError('SCRIPT_SKILL_SNAPSHOT_CORRUPTED')
    try:
        valid_hash = canonical_hash(data) == sha
    except (TypeError, ValueError, OverflowError):
        valid_hash = False
    if (not valid_hash
            or hashlib.sha256(data['rules'].encode('utf-8')).hexdigest() != data['rules_sha256']):
        raise ValueError('SCRIPT_SKILL_SNAPSHOT_CORRUPTED')
    return data


def validate_script_skill_requirement(job):
    if not isinstance(job, dict) or not isinstance(job.get('script_input'), dict):
        raise ValueError('SCRIPT_SKILL_INVALID')
    si = job['script_input']
    if si.get('mode') == 'generated':
        if not si.get('skill'):
            raise ValueError('SCRIPT_SKILL_REQUIRED:请在本地指定本次口播 Skill')
        _selection(si['skill'])
        from scheduler.controlled_assembly import controlled
        if controlled(si['skill']):
            raise ValueError('CONTROLLED_ASSEMBLY_REQUIRED:请在独立口播中按库组装、审核并确认后转入视频')
