"""Read the user-approved library without rewriting its wording or medical scope."""
import copy
import hashlib
import json
import re
from pathlib import Path

from scheduler.contracts import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 'script-expression-catalog/v1'
_APPROVED = 'industry_reference_approved'


def _read(root, relative, expected_sha=None):
    path = Path(relative)
    if path.is_absolute() or ':' in str(relative) or '..' in path.parts:
        raise ValueError('EXPRESSION_PATH_OUTSIDE_ROOT')
    path = (root / path).resolve()
    if not path.is_relative_to(root):
        raise ValueError('EXPRESSION_PATH_OUTSIDE_ROOT')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha is not None and digest != expected_sha:
        raise ValueError('EXPRESSION_APPROVED_FILE_CHANGED:' + str(relative))
    return raw.decode('utf-8-sig'), digest


def _context(raw, text_key):
    # Keep qualifiers, clinical context, provenance and null cause/effect assertions.
    metadata = {'id', text_key, 'status', 'scope_status', 'user_approval', 'user_review', 'product_bindings'}
    return {k: copy.deepcopy(v) for k, v in raw.items() if k not in metadata}


def _catalog_digest(catalog):
    return canonical_hash({k: v for k, v in catalog.items() if k != 'catalog_sha256'})


def validate_catalog(catalog):
    """Reject edited snapshots; callers must originate snapshots from load_catalog."""
    if not isinstance(catalog, dict) or catalog.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('EXPRESSION_CATALOG_INVALID')
    if catalog.get('catalog_sha256') != _catalog_digest(catalog):
        raise ValueError('EXPRESSION_CATALOG_SNAPSHOT_CHANGED')
    ids = set()
    for entry in catalog.get('entries', []):
        eid = entry.get('id')
        approval = entry.get('approval', {})
        if not eid or eid in ids or approval.get('status') != 'approved':
            raise ValueError('EXPRESSION_ENTRY_NOT_APPROVED_OR_DUPLICATE')
        ids.add(eid)
    if not ids:
        raise ValueError('EXPRESSION_CATALOG_EMPTY')
    return catalog


def load_catalog(root=ROOT):
    root = Path(root).resolve()
    config_text, _ = _read(root, 'config/script_expression_catalog.json')
    config = json.loads(config_text)
    if config.get('schema_version') != 'script-expression-registry/v1':
        raise ValueError('EXPRESSION_REGISTRY_INVALID')
    manifest_text, manifest_sha = _read(root, config['approval_manifest'], config['approval_manifest_sha256'])
    manifest = json.loads(manifest_text)
    if manifest.get('schema_version') != 'health-library-approval/v1':
        raise ValueError('EXPRESSION_APPROVAL_MANIFEST_INVALID')
    entries, file_records, seen_paths = [], [], set()

    def make(raw, kind, text_key, path, digest, sources=None, context=None):
        if raw.get('status', raw.get('scope_status')) != _APPROVED:
            raise ValueError('EXPRESSION_ENTRY_NOT_APPROVED:' + str(raw.get('id')))
        source_id = raw.get('source_id')
        if source_id and source_id not in (sources or {}):
            raise ValueError('EXPRESSION_SOURCE_UNKNOWN:' + source_id)
        source = copy.deepcopy((sources or {}).get(source_id))
        return {'id': raw['id'], 'kind': kind, 'category': raw.get('category', 'unclassified' if kind != 'pattern' else 'general'),
                'text': raw[text_key], 'context': context if context is not None else {
                    'original': _context(raw, text_key), 'source': source},
                'approval': {'status': 'approved', 'scope': 'industry_reference_only',
                             'record_id': manifest['approval_id'], 'manifest_sha256': manifest_sha,
                             'source_file_sha256': digest, 'medical_verification_claimed': False},
                'source_refs': [{'path': path, 'sha256': digest, 'source_id': source_id, 'source': source}]}

    for spec in manifest['approved_sets']:
        path = spec['path']
        if path in seen_paths:
            raise ValueError('EXPRESSION_SOURCE_DUPLICATE')
        seen_paths.add(path)
        text, digest = _read(root, path, spec['approved_file_sha256'])
        file_records.append({'path': path, 'sha256': digest})
        if path.endswith('.md'):
            rows = {}
            for line in text.splitlines():
                cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
                if len(cells) == 4 and re.fullmatch(r'[ABC]\d{2}', cells[0]):
                    if cells[0] in rows:
                        raise ValueError('EXPRESSION_ID_DUPLICATE')
                    rows[cells[0]] = cells
            wanted = spec['groups']['additional_display_ids']
            if len(set(wanted)) != len(wanted) or not set(wanted).issubset(rows):
                raise ValueError('EXPRESSION_MARKDOWN_IDS_INVALID')
            for eid in wanted:
                cells = rows[eid]
                if cells[3] != '已认可：行业参考':
                    raise ValueError('EXPRESSION_MARKDOWN_NOT_APPROVED')
                raw = {'id': eid, 'phrase': cells[1], 'status': _APPROVED,
                       'source_context': cells[2], 'capture': 'draft_pattern' if eid.startswith('C') else 'source_phrase'}
                entries.append(make(raw, 'pattern' if eid.startswith('C') else 'expression', 'phrase', path, digest))
            continue
        data = json.loads(text)
        for group, approved_ids in spec['groups'].items():
            rows = data.get(group)
            if not isinstance(rows, list):
                raise ValueError('EXPRESSION_APPROVED_GROUP_MISSING')
            ids = [r.get('id') for r in rows]
            if len(ids) != len(set(ids)) or len(approved_ids) != len(set(approved_ids)) or set(ids) != set(approved_ids):
                raise ValueError('EXPRESSION_UNKNOWN_DUPLICATE_OR_MISSING_ID')
            for raw in rows:
                if group == 'observations':
                    kind, key = 'observation', 'spoken_candidate'
                elif group == 'categories':
                    kind, key = 'usage', 'scene_candidate'
                elif group == 'cards':
                    kind, key = 'painpoint', 'spoken_candidate'
                elif 'painpoint_ref' in raw:
                    kind, key = 'summary', 'summary'
                else:
                    kind, key = 'expression', 'phrase'
                entry = make(raw, kind, key, path, digest, data.get('sources'))
                if kind == 'usage':
                    entry['id'] = 'USAGE-' + raw['id']
                    entry['category'] = raw['id']
                entries.append(entry)

    by_id = {}
    for entry in entries:
        if entry['id'] in by_id:
            raise ValueError('EXPRESSION_ID_DUPLICATE')
        by_id[entry['id']] = entry

    def observations(original):
        result = []
        for oid in original.get('observation_refs', []):
            observation = by_id.get(oid)
            if not observation or observation['kind'] != 'observation':
                raise ValueError('EXPRESSION_OBSERVATION_MISSING:' + oid)
            result.append({'id': oid, 'text': observation['text'], 'context': copy.deepcopy(observation['context'])})
        return result

    for entry in entries:
        original = entry['context']['original']
        if entry['kind'] in ('painpoint', 'usage'):
            entry['context']['observations'] = observations(original)
            for ref in original.get('effect_refs', []):
                if ref not in by_id:
                    raise ValueError('EXPRESSION_EFFECT_MISSING:' + ref)
    for entry in entries:
        if entry['kind'] != 'summary':
            continue
        parent_id = entry['context']['original']['painpoint_ref']
        parent = by_id.get(parent_id)
        if not parent or parent['kind'] != 'painpoint':
            raise ValueError('EXPRESSION_PARENT_MISSING:' + parent_id)
        entry['category'] = parent['category']
        entry['context']['parent'] = {'id': parent_id, 'text': parent['text'], 'original': copy.deepcopy(parent['context']['original'])}
        entry['context']['observations'] = copy.deepcopy(parent['context']['observations'])
        entry['source_refs'].extend(copy.deepcopy(parent['source_refs']))
    catalog = {'schema_version': SCHEMA_VERSION, 'approval_manifest_sha256': manifest_sha,
               'sources': file_records, 'entries': entries}
    catalog['catalog_sha256'] = _catalog_digest(catalog)
    return validate_catalog(catalog)
