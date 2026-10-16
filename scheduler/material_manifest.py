"""material_manifest (consumer) — optional import/filter adapter for reviewed material
manifests (material-manifest/v1) produced by laox_director_lab.

Converts PASS manifest entries bound to this job's own product assets into NATIVE shot
candidates via an explicit material branch — they never masquerade as Narrato analysis:
frame_count is 0 (no frame detection), candidate ids carry the 'mcat-' prefix, the
evidence hash is the manifest file hash, and observation texts are upstream
review-window semantics (human/upstream metadata, disclosed as such in the import
report consumed by machines).

Fail-closed gates (both modes unless noted): manifest file hash, WHOLE-MANIFEST ROOT
contract validation (required root fields + unknown root fields rejected via the
delivered Draft 2020-12 schema — non-object entries and non-hash field values are
refused per-entry without crashing valid siblings, and a refused entry's hash-shaped
declared SHA stays bound so its media cannot bypass the gate via Narrato analysis,
EK-14/EK-17), structural entry validation (all contract fields
present/enumerable), real media file hash, real media probe duration (declared
durations and windows are bound to the actual file, never trusted from metadata),
review PASS + content-hash binding (stale = refuse), entry-level product_id binding
(never matched through category or generic Chinese character intersection),
must-preserve audio (must not silently become silent free-pool B-roll), restrictions
(explicitly unsupported rather than silently ignored), poor-quality flags, grid-aligned
in-range windows, unverified durations, test-flag/eligibility consistency.

Production additionally refuses TEST-flagged manifests, applicable-but-not-run quality
metrics, and parses QUALITY EVIDENCE CONTENT (repair-1, EK-12/13): a vbench pass must
be backed by an evidence file whose parsed JSON binds to the entry's CURRENT media
hash, declared metrics and a pass verdict (another video's report is refused); every
production-eligible entry additionally needs a parsed visual-review evidence file
bound to the current media/material/version/window with verdict pass, plus verified
product identity — a not-applicable VBench declaration or self-declared clean flags
are NOT visual review. Test mode (isolated acceptance only) admits TEST manifests.

Use classification runs on the DELIVERED v1 keyword vocabulary snapshot below. The
live scheduler.script_plan has been rewritten to r3 action semantics (open_packaging/
split_fruit) by a concurrent external task and is NOT consumed here; see director_lab
ERROR_KNOWLEDGE EK-16 — the snapshot stays until the coordinator reconciles the drift.
"""
import hashlib
import json
import re
from pathlib import Path

from scheduler.contracts import canonical_hash

TIMESCALE = 48000
GRID = 1920
POOR_QUALITY_FLAGS = {'obscured_product', 'cropped_product'}
DEFAULT_MIN_WINDOW_TICKS = 24000
VBENCH_STATUSES = {'not_run', 'blocked', 'pass'}
ORIGINS = {'existing_h3_generated_demo', 'real_world_recording', 'generated_for_case'}
GENERATED_STATUSES = {'existing_footage_demo', 'to_be_generated', 'not_generated_test_only', 'real_world_recording'}
DURATION_SOURCES = {'probe_evidence', 'fixture_inventory', 'unverified', 'unknown'}
AUDIO_POLICIES = {'discard_allowed', 'must_preserve_original', 'silent_broll_only'}
# Shape of a declared media hash (schema $defs.hash). Untrusted entry values are checked
# against this BEFORE any set/hash operation (EK-17): a list/dict/null/numeric
# source_sha256 must be refused per-entry by the validation gates, never crash
# bound_shas.add — and a hash-shaped declared SHA stays bound even when its entry is
# refused, so that media cannot re-enter the pool through Narrato analysis.
_SHA256_HEX = re.compile(r'^[a-f0-9]{64}$')
# Full JSON Schema of the manifest lives with its producer module; the bridge validates
# against it (Draft 2020-12 + check_schema) and fails closed when it cannot be loaded.
DEFAULT_SCHEMA_PATH = Path('F:/codex/laox_director_lab/schemas/material-manifest-v1.schema.json')

# DELIVERED v1 consumer vocabulary snapshot (equal to director_lab
# agents.product_storyboards.USE_KEYWORDS; tests lock the two copies together). The
# live script_plan.py USE_KEYWORDS/USE_PRIORITY drifted to r3 semantics externally
# (EK-16): candidates must stay inside the delivered shot-library use enum, so the
# bridge classifies with this snapshot instead of importing the drifted module.
DELIVERED_USE_KEYWORDS = {
    'open_box': ['打开', '开盒', '拆开', '撕开', '开封', '揭开', '开箱', '掰开', '切开', '剖开', '对半切'],
    'packaging': ['包装', '盒子', '袋子', '封口', '标签', '外观', '正面'],
    'detail': ['特写', '细节', '颗粒', '切面', '里面', '内部', '果肉', '纹理', '断面', '仔细看'],
    'takeout': ['取出', '拿出来', '舀', '倒出', '夹起'],
    'wrapup': ['盖上', '收起', '合上', '放回', '收尾'],
}
DELIVERED_USE_PRIORITY = ['open_box', 'takeout', 'wrapup', 'detail', 'packaging', 'other']

_ENTRY_REQUIRED = {
    'material_id': str, 'shot_id': str, 'product_id': str, 'version': int,
    'path': str, 'source_sha256': str, 'review_status': str,
    'review': dict, 'semantics': dict, 'features': dict, 'narration': dict,
    'keywords': list, 'usable_window': dict, 'audio': dict, 'references': list,
    'restrictions': list, 'technical': dict, 'visual': dict, 'vbench': dict,
    'applicability': dict, 'evidence': dict, 'test_flag': bool,
    'generated': dict, 'auto_edit_eligible': bool,
}

_SCHEMA_CACHE = {}
_PROBE_CACHE = {}   # media duration ticks keyed by file SHA256 (path-bound caches go stale)


def _load_schema(schema_path):
    """Load the full manifest schema (check_schema'd) and return an entry validator
    bound to the entries[] item subschema."""
    from jsonschema import Draft202012Validator
    key = str(schema_path)
    if key not in _SCHEMA_CACHE:
        p = Path(schema_path)
        if not p.is_file():
            raise ValueError('MANIFEST_SCHEMA_FILE_MISSING:' + str(p))
        try:
            schema = json.loads(p.read_text(encoding='utf-8'))
            Draft202012Validator.check_schema(schema)
            # validate each ENTRY against the entries[] item schema, keeping the root
            # $defs so internal refs (#/$defs/id, #/$defs/hash) still resolve
            item_schema = schema['properties']['entries']['items']
            wrapped = {'$schema': schema.get('$schema'), '$defs': schema.get('$defs', {})}
            wrapped.update(item_schema)
            _SCHEMA_CACHE[key] = Draft202012Validator(wrapped)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — malformed schema must be fail-closed
            raise ValueError('MANIFEST_SCHEMA_INVALID:' + str(exc)[:200])
    return _SCHEMA_CACHE[key]


def _load_root_validator(schema_path):
    """Root-contract validator: the delivered schema with entries[] constrained only to
    a non-empty array. Entry CONTENT stays per-entry (failure isolation for bad
    entries); the root check enforces required root fields (created_utc/source_case/…)
    and rejects unknown root fields (additionalProperties:false)."""
    from jsonschema import Draft202012Validator
    key = 'root:' + str(schema_path)
    if key not in _SCHEMA_CACHE:
        p = Path(schema_path)
        if not p.is_file():
            raise ValueError('MANIFEST_SCHEMA_FILE_MISSING:' + str(p))
        try:
            schema = json.loads(p.read_text(encoding='utf-8'))
            Draft202012Validator.check_schema(schema)
            root = json.loads(json.dumps(schema))
            root['properties']['entries'] = {'type': 'array', 'minItems': 1}
            _SCHEMA_CACHE[key] = Draft202012Validator(root)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — malformed schema must be fail-closed
            raise ValueError('MANIFEST_SCHEMA_INVALID:' + str(exc)[:200])
    return _SCHEMA_CACHE[key]


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _probe_ticks(path, sha):
    """Real media duration via the pipeline's ffprobe, cached per file SHA256 (a
    path-keyed cache would go stale the moment the file at that path changes)."""
    from renderer.ffmpeg import probe
    if sha not in _PROBE_CACHE:
        _PROBE_CACHE[sha] = probe(Path(path))['duration_ticks']
    return _PROBE_CACHE[sha]


def entry_content_sha256(entry):
    """Content the review verdict binds to: the entry without its review block.
    Must stay byte-compatible with the exporter (director_lab adapters.material_manifest
    .entry_content_hash) — same canonical JSON algorithm."""
    return canonical_hash({k: v for k, v in entry.items() if k != 'review'})


def load_manifest(manifest_ref, schema_path=None):
    path = Path(manifest_ref['path'])
    if not path.is_file():
        raise ValueError('MANIFEST_FILE_MISSING:' + str(path))
    actual = _sha256_file(path)
    declared = manifest_ref.get('sha256')
    if declared and declared != actual:
        raise ValueError('MANIFEST_HASH_MISMATCH')
    try:
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as exc:
        raise ValueError('MANIFEST_STRUCTURE_INVALID:root_not_json:' + str(exc)[:120])
    if not isinstance(manifest, dict):
        # EK-14 / coordinator non-object-root negative: an explicit refusal, never an
        # accidental AttributeError from calling .get on a list/string root
        raise ValueError('MANIFEST_STRUCTURE_INVALID:root_not_object:' + type(manifest).__name__)
    if manifest.get('schema_version') != 'material-manifest/v1':
        raise ValueError('UNSUPPORTED_MANIFEST_SCHEMA_VERSION:' + str(manifest.get('schema_version')))
    for field, kind in (('manifest_id', str), ('policy_version', str), ('test_flag', bool), ('entries', list)):
        if not isinstance(manifest.get(field), kind):
            raise ValueError('MANIFEST_STRUCTURE_INVALID:' + field)
    # whole-manifest ROOT contract (EK-14): required root fields + unknown root fields
    # per the delivered Draft 2020-12 schema. Entry content is validated per-entry in
    # import_material_manifest so one bad entry cannot sink valid siblings.
    root_errors = sorted(_load_root_validator(schema_path or DEFAULT_SCHEMA_PATH)
                         .iter_errors(manifest), key=lambda e: list(e.absolute_path))
    if root_errors:
        first = root_errors[0]
        loc = '/'.join(str(p) for p in first.absolute_path) or '<root>'
        raise ValueError('MANIFEST_STRUCTURE_INVALID:%s:%s' % (loc, str(first.message)[:160]))
    return manifest, actual


def _structural_problems(entry):
    """Fail-closed structural validation of one entry (schema-equivalent, no schema file
    dependency on the consumer side)."""
    problems = []
    for field, kind in _ENTRY_REQUIRED.items():
        if field not in entry or not isinstance(entry[field], kind):
            problems.append('FIELD_INVALID:' + field)
            return problems
    review = entry['review']
    for field in ('verdict', 'content_sha256', 'reviewer', 'problems'):
        if field not in review:
            problems.append('FIELD_INVALID:review.' + field)
    semantics = entry['semantics']
    for field in ('summary', 'action'):
        if field not in semantics:
            problems.append('FIELD_INVALID:semantics.' + field)
    for field in ('framing', 'camera', 'movement'):
        if field not in entry['features']:
            problems.append('FIELD_INVALID:features.' + field)
    window = entry['usable_window']
    for field in ('start_sec', 'end_sec'):
        if field not in window or not isinstance(window[field], (int, float)):
            problems.append('FIELD_INVALID:usable_window.' + field)
    audio = entry['audio']
    for field in ('has_original', 'must_preserve', 'policy'):
        if field not in audio:
            problems.append('FIELD_INVALID:audio.' + field)
    if audio.get('policy') not in AUDIO_POLICIES:
        problems.append('FIELD_INVALID:audio.policy')
    technical = entry['technical']
    for field in ('duration_sec', 'duration_source'):
        if field not in technical:
            problems.append('FIELD_INVALID:technical.' + field)
    if technical.get('duration_source') not in DURATION_SOURCES:
        problems.append('FIELD_INVALID:technical.duration_source')
    visual = entry['visual']
    for field in ('quality_flags', 'occlusion'):
        if field not in visual:
            problems.append('FIELD_INVALID:visual.' + field)
    vbench = entry['vbench']
    for field in ('applicable', 'status'):
        if field not in vbench:
            problems.append('FIELD_INVALID:vbench.' + field)
    if vbench.get('status') not in VBENCH_STATUSES:
        problems.append('FIELD_INVALID:vbench.status')
    if vbench.get('status') == 'pass' and (not vbench.get('metrics')
                                           or not vbench.get('metrics_evidence_sha256')):
        problems.append('FIELD_INVALID:vbench.pass_without_bound_evidence')
    if vbench.get('status') == 'pass' and not ((entry.get('evidence') or {})
                                               .get('metrics_evidence_path')):
        problems.append('FIELD_INVALID:evidence.metrics_evidence_path')
    provenance = (entry.get('evidence') or {}).get('provenance') or {}
    if provenance.get('origin') not in ORIGINS:
        problems.append('FIELD_INVALID:evidence.provenance.origin')
    generated = entry['generated']
    if generated.get('status') not in GENERATED_STATUSES:
        problems.append('FIELD_INVALID:generated.status')
    if entry['test_flag'] and entry['auto_edit_eligible']:
        problems.append('FIELD_INVALID:test_entry_cannot_be_production_eligible')
    if entry['review_status'] != 'PASS' or review.get('verdict') != 'PASS':
        problems.append('FIELD_INVALID:review_status')
    if review.get('problems'):
        problems.append('FIELD_INVALID:review_problems_nonempty')
    return problems


def _fail(failures, entry, reason, detail=None):
    record = {'material_id': entry.get('material_id', '?') if isinstance(entry, dict) else '?',
              'product_id': entry.get('product_id', '?') if isinstance(entry, dict) else '?',
              'reason': reason}
    if detail:
        record['detail'] = detail[:200]
    failures.append(record)


def _load_evidence_validators(schema_path):
    """Validators for the normalized side-evidence file contracts, built from the
    delivered schema's $defs.metrics_evidence / $defs.visual_review_evidence (EK-12/13).
    Parsed evidence is validated against the REAL contract (required fields + types),
    not a hand-picked subset — a doc missing checked_utc/product_identity is invalid."""
    from jsonschema import Draft202012Validator
    key = 'defs:' + str(schema_path)
    if key not in _SCHEMA_CACHE:
        p = Path(schema_path)
        if not p.is_file():
            raise ValueError('MANIFEST_SCHEMA_FILE_MISSING:' + str(p))
        try:
            schema = json.loads(p.read_text(encoding='utf-8'))
            Draft202012Validator.check_schema(schema)
            out = {}
            for def_name in ('metrics_evidence', 'visual_review_evidence'):
                d = json.loads(json.dumps(schema['$defs'][def_name]))
                d.setdefault('$defs', {}).update(schema.get('$defs', {}))
                Draft202012Validator.check_schema(d)
                out[def_name] = Draft202012Validator(d)
            _SCHEMA_CACHE[key] = out
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — malformed schema must be fail-closed
            raise ValueError('MANIFEST_SCHEMA_INVALID:' + str(exc)[:200])
    return _SCHEMA_CACHE[key]


def _evidence_schema_detail(validator, doc):
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = '/'.join(str(p) for p in first.absolute_path) or '<root>'
        return 'evidence contract invalid at %s: %s' % (loc, str(first.message)[:100])
    return None


def _load_evidence_json(path):
    """Parse a side evidence file. Returns the object dict, or raises ValueError with a
    precise cause (missing/unreadable/non-JSON/non-object). File-hash equality is
    checked by the CALLER against the manifest-declared SHA before parsing."""
    p = Path(path)
    if not p.is_file():
        raise ValueError('EVIDENCE_FILE_MISSING:' + str(p))
    try:
        data = json.loads(p.read_text(encoding='utf-8-sig'))
    except Exception as exc:  # noqa: BLE001 — arbitrary bytes must fail closed
        raise ValueError('EVIDENCE_NOT_JSON:' + str(exc)[:120])
    if not isinstance(data, dict):
        raise ValueError('EVIDENCE_NOT_OBJECT:' + type(data).__name__)
    return data


def _numbers_close(a, b):
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) < 1e-9


def _window_matches(declared, entry_window):
    if not isinstance(declared, dict):
        return False
    return _numbers_close(declared.get('start_sec'), entry_window.get('start_sec')) and \
        _numbers_close(declared.get('end_sec'), entry_window.get('end_sec'))


def _metrics_evidence_detail(report, entry, vbench, validator):
    """Normalized metrics-evidence contract (schema $defs.metrics_evidence, EK-12).
    Returns None when the report validly backs this entry's vbench pass, else a short
    refusal detail. The doc is first validated against the delivered evidence schema
    (required fields/types), then CONTENT-bound to THIS entry: the report must be FOR
    THIS VIDEO (source_sha256), carry a pass status, and repeat the declared metrics
    exactly; optional material/version/window bindings must match the entry."""
    detail = _evidence_schema_detail(validator, report)
    if detail:
        return detail
    if report.get('source_sha256') != entry['source_sha256']:
        return 'metrics report bound to another/zero video hash'
    if report.get('status') != 'pass':
        return 'metrics report status=%r cannot back a pass claim' % (report.get('status'),)
    if report['metrics'] != vbench.get('metrics'):
        return 'declared metrics differ from reported metrics'
    if report.get('material_id') is not None and report['material_id'] != entry['material_id']:
        return 'metrics report bound to another material'
    if report.get('version') is not None and report['version'] != entry.get('version'):
        return 'metrics report bound to a stale version'
    if report.get('window') is not None and not _window_matches(report['window'], entry.get('usable_window') or {}):
        return 'metrics report bound to a different window'
    return None


def _visual_review_detail(review, entry, validator):
    """Normalized visual-review-evidence contract (schema $defs.visual_review_evidence,
    EK-13). Returns None when the file validly backs THIS entry's production
    eligibility, else a short refusal detail. The doc is validated against the
    delivered evidence schema first (checked_utc and product_identity are REQUIRED —
    a hand-picked subset check would accept docs missing them), then content-bound:
    media hash, material, CURRENT version, window, pass verdict, non-empty reviewer,
    and an explicit verified identity check bound to this entry's product (the
    manifest's self-declared provenance bool alone never substitutes for it)."""
    detail = _evidence_schema_detail(validator, review)
    if detail:
        return detail
    if review.get('source_sha256') != entry['source_sha256']:
        return 'visual review bound to another video hash'
    if review.get('material_id') != entry['material_id']:
        return 'visual review bound to another material'
    if review.get('version') != entry.get('version'):
        return 'visual review bound to a stale version'
    if not _window_matches(review.get('window'), entry.get('usable_window') or {}):
        return 'visual review bound to a different window'
    if review.get('verdict') != 'pass':
        return 'visual review verdict=%r is not pass' % (review.get('verdict'),)
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        return 'visual review has no reviewer'
    identity = review.get('product_identity') or {}
    if identity.get('verified') is not True:
        return 'visual review identity check not verified'
    if identity.get('product_id') != entry['product_id']:
        return 'visual review identity bound to another product'
    ref_sha = identity.get('reference_sha256')
    if ref_sha is not None and ref_sha != entry['source_sha256']:
        return 'visual review identity bound to another reference'
    return None


def _classify_use(entry):
    """Delivered-v1 keyword classification of a manifest entry (snapshot tables above;
    the live r3 script_plan classifier is deliberately not consumed — EK-16). The
    result always stays inside the delivered shot-library use enum."""
    texts = list((entry.get('semantics') or {}).get('observation_texts') or [])
    texts.append((entry.get('semantics') or {}).get('summary') or '')
    texts.append((entry.get('narration') or {}).get('text') or '')
    joined = ' '.join(t for t in texts if t)
    for use in DELIVERED_USE_PRIORITY:
        keywords = DELIVERED_USE_KEYWORDS.get(use)
        if keywords and any(k in joined for k in keywords):
            return use
    return 'other'


def _candidate(asset, entry, in_tick, out_tick, manifest_sha):
    """Native shot-library candidate built directly from a reviewed manifest entry.
    frame_count=0: this is upstream review metadata, not a frame detection."""
    config_hash = canonical_hash({'use_rule_version': 'use-keywords-v1',
                                  'material_manifest_sha256': manifest_sha})
    candidate_id = 'mcat-' + canonical_hash({'src': entry['source_sha256'], 'in': in_tick, 'out': out_tick,
                                             'mat': entry['material_id'], 'ver': entry.get('version', 1),
                                             'cfg': config_hash})[:32]
    texts = [t for t in ((entry.get('semantics') or {}).get('observation_texts') or [])[:8] if t]
    summary = (entry.get('semantics') or {}).get('summary') or ''
    narration = (entry.get('narration') or {}).get('text') or ''
    return {
        'candidate_id': candidate_id,
        'source_asset_id': asset['asset_id'],
        'source_sha256': entry['source_sha256'],
        'in_tick': in_tick, 'out_tick': out_tick,
        'use': _classify_use(entry),
        'action_text': texts[0] if texts else None,
        'product_description': max(texts + [summary, narration], key=len) if (texts or summary or narration) else None,
        'product_identity_verified': bool((((entry.get('evidence') or {}).get('provenance') or {})
                                           .get('product_identity_verified', False))),
        'occlusion': ((entry.get('visual') or {}).get('occlusion') if (entry.get('visual') or {})
                      .get('occlusion') in ('unknown', 'none', 'partial', 'severe') else 'unknown'),
        'clarity': 'unknown',
        'speech_locked': False,
        'evidence': {'analysis_evidence_sha256': manifest_sha,
                     'frame_count': 0,
                     'observation_count': len(texts),
                     'observation_texts': texts},
        'fixed_role': None,
        'usable': True, 'unusable_reason': None,
    }


def import_material_manifest(assets, manifest_ref, *, mode='production',
                             min_window_ticks=DEFAULT_MIN_WINDOW_TICKS, verify_media=True,
                             schema_path=None):
    """Filter + convert manifest entries into native candidates.

    Returns {'candidates': [shot-library candidate...], 'failures': [...],
             'bound_shas': {entry source_sha256...},   # assets whose pool candidacy is
             governed by the manifest gate (their Narrato analysis must not bypass it)
             'report': {...truthful provenance...}}.
    Production mode forces full media verification regardless of verify_media, and every
    entry is validated against the full material-manifest/v1 JSON Schema (check_schema'd).
    """
    if mode not in ('production', 'test'):
        raise ValueError('UNSUPPORTED_MANIFEST_MODE:' + str(mode))
    if mode == 'production':
        verify_media = True  # media hash + probe gates are not skippable in production
    manifest, manifest_sha = load_manifest(manifest_ref, schema_path)
    validator = _load_schema(schema_path or DEFAULT_SCHEMA_PATH)
    evidence_validators = _load_evidence_validators(schema_path or DEFAULT_SCHEMA_PATH)
    if manifest.get('test_flag') and mode != 'test':
        raise ValueError('TEST_FLAG_REFUSED_IN_PRODUCTION')
    by_sha = {}
    for asset in assets:
        if asset.get('role') == 'product':
            by_sha[asset['sha256']] = asset
    product_filter = manifest_ref.get('product_id')
    candidates, failures, imported = [], [], []
    bound_shas = set()
    for entry in manifest.get('entries', []):
        if not isinstance(entry, dict):
            # EK-14: malformed non-object entry — explicit per-entry refusal, never an
            # accidental AttributeError; valid siblings keep processing
            _fail(failures, entry, 'MANIFEST_ENTRY_INVALID',
                  detail='entry_not_object:%s' % type(entry).__name__)
            continue
        # EK-17: validate the declared SHA's shape before any set/hash operation — a
        # non-hash value (list/dict/null/number) must reach the per-entry validation
        # gates and be refused as MANIFEST_ENTRY_INVALID, never crash here. A
        # hash-shaped declared SHA stays bound EVEN when the entry is about to be
        # refused: the manifest gate governs that media's Narrato analysis too.
        declared_sha = entry.get('source_sha256')
        if isinstance(declared_sha, str) and _SHA256_HEX.fullmatch(declared_sha):
            bound_shas.add(declared_sha)
        # Full JSON-Schema validation runs BEFORE the structural walk (coordinator
        # nested counterexample: audio.policy=[] crashed in set membership): jsonschema
        # is total over arbitrary JSON values, while the structural checks perform
        # set membership / nested access on untrusted values (audio.policy,
        # vbench.status, technical.duration_source, generated.status, provenance…).
        # One systematic gate instead of per-field type patches; the structural walk
        # then only ever sees schema-conforming containers.
        schema_errors = sorted(validator.iter_errors(entry),
                               key=lambda e: list(e.absolute_path))
        if schema_errors:
            first = schema_errors[0]
            loc = '/'.join(str(p) for p in first.absolute_path) or '<entry>'
            _fail(failures, entry, 'MANIFEST_ENTRY_INVALID',
                  detail='schema:%s:%s' % (loc, str(first.message)[:100]))
            continue
        structural = _structural_problems(entry)
        if structural:
            _fail(failures, entry, 'MANIFEST_ENTRY_INVALID', detail=structural[0])
            continue
        asset = by_sha.get(entry.get('source_sha256'))
        if asset is None:
            _fail(failures, entry, 'ASSET_NOT_IN_JOB')
            continue
        if product_filter and entry.get('product_id') != product_filter:
            # entry-level refusal: a wrong product never matches through category or
            # generic Chinese character intersection later in the planner
            _fail(failures, entry, 'PRODUCT_ID_MISMATCH')
            continue
        review = entry['review']
        if review.get('content_sha256') != entry_content_sha256(entry):
            _fail(failures, entry, 'REVIEW_STALE')
            continue
        if entry['test_flag'] != manifest.get('test_flag', False):
            _fail(failures, entry, 'TEST_FLAG_INCONSISTENT')
            continue
        if entry['audio']['must_preserve'] or entry['audio']['policy'] == 'must_preserve_original':
            _fail(failures, entry, 'AUDIO_MUST_PRESERVE_EXCLUDED')
            continue
        if entry['restrictions']:
            _fail(failures, entry, 'RESTRICTION_UNSUPPORTED')
            continue
        vbench = entry['vbench']
        if vbench['status'] == 'blocked':
            _fail(failures, entry, 'VBENCH_BLOCKED')
            continue
        if vbench['status'] == 'pass':
            metrics_path = (entry.get('evidence') or {}).get('metrics_evidence_path')
            if not (vbench.get('metrics') and vbench.get('metrics_evidence_sha256')):
                _fail(failures, entry, 'QUALITY_REVIEW_INVALID_STATUS')
                continue
            if not metrics_path:
                # pass without a real evidence FILE binding is a forged/unusable claim
                _fail(failures, entry, 'QUALITY_REVIEW_INVALID_STATUS')
                continue
            mp = Path(metrics_path)
            if not mp.is_file() or _sha256_file(mp) != vbench['metrics_evidence_sha256']:
                _fail(failures, entry, 'QUALITY_REVIEW_INVALID_STATUS')
                continue
            # EK-12: the evidence file's CONTENT must back THIS entry — parse the
            # normalized metrics-evidence contract and bind media hash/status/metrics
            # (arbitrary bytes, another video's report, wrong metrics or a failed/
            # not_run report are all refused; a file hash alone proves nothing)
            try:
                report = _load_evidence_json(mp)
                detail = _metrics_evidence_detail(report, entry, vbench,
                                                  evidence_validators['metrics_evidence'])
            except ValueError as exc:
                detail = str(exc)
            if detail:
                _fail(failures, entry, 'QUALITY_REVIEW_INVALID_STATUS', detail=detail)
                continue
        if mode == 'production' and (not entry['auto_edit_eligible']
                                     or not entry['applicability'].get('auto_edit', False)):
            # an entry that declines auto-edit eligibility never enters the auto-edit
            # free pool, whatever its origin or quality claims
            _fail(failures, entry, 'PRODUCTION_ENTRY_NOT_ELIGIBLE')
            continue
        if vbench['applicable'] and vbench['status'] == 'not_run' and mode == 'production':
            # production refuses entries whose applicable quality review was skipped
            _fail(failures, entry, 'QUALITY_REVIEW_NOT_RUN')
            continue
        if set(entry['visual']['quality_flags'] or []) & POOR_QUALITY_FLAGS:
            _fail(failures, entry, 'POOR_QUALITY_FLAG')
            continue
        if mode == 'production' and entry['auto_edit_eligible']:
            # EK-13: production eligibility demands PARSED evidence, not self-declared
            # flags. Every eligible entry needs (1) an applicable quality metric pass
            # with content-bound evidence (checked above), and (2) a real visual review
            # evidence file bound to the CURRENT video/material/version/window with a
            # pass verdict — a not-applicable VBench declaration is not visual review —
            # and (3) verified product identity. Missing any of them keeps the entry
            # blocked; this round produces no such evidence, so nothing gets promoted.
            origin = entry['evidence']['provenance']['origin']
            provenance = entry['evidence']['provenance']
            if not provenance.get('product_identity_verified', False):
                _fail(failures, entry, 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING',
                      detail='product identity unverified — production stays blocked')
                continue
            vr_path = (entry.get('evidence') or {}).get('visual_review_path')
            vr_sha = (entry.get('evidence') or {}).get('visual_review_sha256')
            if not vr_path or not vr_sha:
                _fail(failures, entry, 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING',
                      detail='no visual review evidence file (vbench not-applicable is not visual review; '
                             'origin=%s)' % origin)
                continue
            vp = Path(vr_path)
            if not vp.is_file() or _sha256_file(vp) != vr_sha:
                _fail(failures, entry, 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING',
                      detail='visual review evidence file missing or hash mismatch')
                continue
            try:
                review_ev = _load_evidence_json(vp)
                detail = _visual_review_detail(review_ev, entry,
                                               evidence_validators['visual_review_evidence'])
            except ValueError as exc:
                detail = str(exc)
            if detail:
                _fail(failures, entry, 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING', detail=detail)
                continue
        media_path = Path(entry['path'])
        if verify_media:
            if not media_path.is_file():
                _fail(failures, entry, 'MEDIA_FILE_MISSING')
                continue
            if _sha256_file(media_path) != entry['source_sha256']:
                _fail(failures, entry, 'MEDIA_HASH_MISMATCH')
                continue
            try:
                actual_ticks = _probe_ticks(media_path, entry['source_sha256'])
            except Exception as exc:  # noqa: BLE001 — probe failure must be fail-closed
                _fail(failures, entry, 'MEDIA_PROBE_FAILED:' + str(exc)[:120])
                continue
            declared = entry['technical']['duration_sec']
            if declared is None or entry['technical']['duration_source'] in ('unverified', 'unknown'):
                _fail(failures, entry, 'DURATION_UNVERIFIED')
                continue
            if declared * TIMESCALE > actual_ticks + GRID:
                _fail(failures, entry, 'DURATION_MISMATCH_ACTUAL')
                continue
        else:
            actual_ticks = int(round((entry['technical']['duration_sec'] or 0) * TIMESCALE))
        window = entry['usable_window']
        in_tick, out_tick = window['start_sec'] * TIMESCALE, window['end_sec'] * TIMESCALE
        if not 0 <= in_tick < out_tick <= actual_ticks + GRID:
            _fail(failures, entry, 'WINDOW_OUT_OF_RANGE')
            continue
        if any(abs(v - round(v)) > 1e-6 or int(round(v)) % GRID for v in (in_tick, out_tick)):
            _fail(failures, entry, 'WINDOW_NOT_GRID_ALIGNED')
            continue
        in_tick, out_tick = int(round(in_tick)), int(round(out_tick))
        if out_tick - in_tick < min_window_ticks:
            _fail(failures, entry, 'WINDOW_BELOW_MIN_TICKS')
            continue
        candidates.append(_candidate(asset, entry, in_tick, out_tick, manifest_sha))
        imported.append(entry['material_id'])
    candidates.sort(key=lambda c: (c['source_sha256'], c['in_tick'], c['out_tick']))
    report = {
        'manifest_id': manifest.get('manifest_id'),
        'manifest_sha256': manifest_sha,
        'mode': mode,
        'test_flag': bool(manifest.get('test_flag')),
        'imported_materials': imported,
        'refused': failures,
        'evidence_origin': 'reviewed_material_manifest (review-window semantics, frame_count=0; '
                           'not Narrato frame detection)',
        'candidate_id_prefix': 'mcat-',
        'product_match_policy': 'entry-level product_id/sha binding; no category or generic-character matching',
    }
    return {'candidates': candidates, 'failures': failures, 'bound_shas': bound_shas, 'report': report}
