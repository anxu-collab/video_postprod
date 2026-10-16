"""Reviewed-material manifest import counterexamples (consumer side; koc-v1 bridge).

Negative/positive tests for scheduler.material_manifest and the explicit
build_shot_library material_candidates branch. Entries bind to the REAL a1/a2 fixture
files (jobs/deployment-fixtures/inventory.json) so media hash and ffprobe duration
gates are exercised against actual media. Refusal contract: director_lab
config/koc_material_policy.json → consumer_bridge.refusal_reasons.
"""
import hashlib
import json
from pathlib import Path

import pytest

from scheduler.material_manifest import import_material_manifest, entry_content_sha256
from scheduler.shot_library import build_shot_library

import hashlib as _hl


def _sha256(p):
    return _hl.sha256(Path(p).read_bytes()).hexdigest()


INVENTORY = json.loads((Path(__file__).resolve().parents[1] /
                        'jobs/deployment-fixtures/inventory.json').read_text(encoding='utf-8'))
A1 = INVENTORY['a1']          # ~6.583s real media
A2 = INVENTORY['a2']          # ~5.167s real media


def asset(aid, sha):
    return {'asset_id': aid, 'sha256': sha, 'role': 'product', 'mode': 'broll',
            'analysis': {'mode': 'single_shot'}}


def entry(sha, path, **overrides):
    base = {
        'material_id': 'mat-001', 'shot_id': 'shot-001', 'product_id': 'prod-x',
        'version': 1, 'path': path, 'source_sha256': sha,
        'review_status': 'PASS',
        'review': {'verdict': 'PASS', 'content_sha256': None, 'reviewer': 'r', 'problems': []},
        'semantics': {'summary': '把果子对半切开', 'person': None,
                      'action': {'start': 'a', 'process': ['b'], 'end': 'c'}, 'location': None,
                      'observation_texts': ['把果子对半切开。']},
        'features': {'framing': 'f', 'camera': 'c', 'movement': 'm', 'visual_keywords': []},
        'narration': {'text': '把果子对半切开。', 'language': 'zh'},
        'keywords': [],
        'usable_window': {'start_sec': 0.0, 'end_sec': 2.0, 'grid_ticks': 1920},
        'audio': {'has_original': True, 'must_preserve': False, 'policy': 'discard_allowed'},
        'references': [], 'restrictions': [],
        'technical': {'duration_sec': 6.583333, 'duration_source': 'probe_evidence'},
        'visual': {'quality_flags': [], 'occlusion': 'none'},
        'vbench': {'applicable': False, 'status': 'not_run', 'metrics': {}},
        'applicability': {'auto_edit': True, 'uses': ['open_box']},
        'evidence': {'probe_sha256': '0' * 64,
                     'provenance': {'origin': 'real_world_recording', 'product_identity_verified': True}},
        'test_flag': False,
        'generated': {'status': 'real_world_recording', 'verified_real_product_test': True},
        'auto_edit_eligible': True,
    }
    base.update(overrides)
    if 'review' not in overrides:
        base['review']['content_sha256'] = entry_content_sha256(base)
    return base


def bind(entry_dict):
    """Recompute the review content hash after any mutation."""
    entry_dict['review']['content_sha256'] = entry_content_sha256(entry_dict)
    return entry_dict


UNIT_NOTE = 'UNIT ONLY — synthetic validator fixture; not a real VBench score / human review'


def unit_visual_review_file(tmp_path, e, name='visual-review.json', **overrides):
    """Normalized visual-review evidence for a production-eligible entry (schema
    $defs.visual_review_evidence, incl. REQUIRED checked_utc + product_identity).
    UNIT ONLY fixture: validates the parser contract, never claims a real human
    review happened."""
    doc = {
        'evidence_type': 'visual-review/v1',
        'source_sha256': e['source_sha256'],
        'material_id': e['material_id'],
        'version': e['version'],
        'window': {'start_sec': e['usable_window']['start_sec'],
                   'end_sec': e['usable_window']['end_sec']},
        'verdict': 'pass',
        'reviewer': 'unit-validator',
        'checked_utc': '2026-10-11T00:00:00',
        'product_identity': {'verified': True, 'product_id': e['product_id'],
                             'reference_sha256': e['source_sha256'],
                             'method': 'UNIT ONLY synthetic identity check'},
        'checks': {'product_visible': True},
        'test_fixture': UNIT_NOTE,
    }
    doc.update(overrides)
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    e['evidence']['visual_review_path'] = str(p)
    e['evidence']['visual_review_sha256'] = _sha256(p)
    return p


def unit_metrics_report_file(tmp_path, e, metrics, name='metrics-report.json', **overrides):
    """Normalized metrics evidence (schema $defs.metrics_evidence). UNIT ONLY fixture:
    synthetic numbers for parser-contract tests, never real inference results."""
    doc = {
        'evidence_type': 'vbench-metrics/v1',
        'source_sha256': e['source_sha256'],
        'status': 'pass',
        'metrics': metrics,
        'test_fixture': UNIT_NOTE,
    }
    doc.update(overrides)
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    e['vbench']['metrics_evidence_sha256'] = _sha256(p)
    e['evidence']['metrics_evidence_path'] = str(p)
    return p


def manifest_file(tmp_path, entries, test_flag=False):
    manifest = {'schema_version': 'material-manifest/v1', 'manifest_id': 'm1',
                'created_utc': '2026-10-11T00:00:00', 'policy_version': 'koc-material-policy-v1',
                'test_flag': test_flag, 'source_case': 'c1', 'entries': entries}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def test_production_import_positive_real_media(tmp_path):
    e = entry(A1['sha256'], A1['path'])
    unit_visual_review_file(tmp_path, e)
    bind(e)
    ref = manifest_file(tmp_path, [e])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert result['failures'] == [] and len(result['candidates']) == 1
    cand = result['candidates'][0]
    assert cand['source_asset_id'] == 'src1'
    assert cand['candidate_id'].startswith('mcat-')
    assert cand['evidence']['frame_count'] == 0
    assert cand['evidence']['analysis_evidence_sha256'] == ref['sha256']
    assert cand['use'] == 'open_box'   # delivered v1 vocabulary; live r3 drift not consumed
    assert result['bound_shas'] == {A1['sha256']}


def test_test_flag_refused_in_production_admitted_in_test(tmp_path):
    ref = manifest_file(tmp_path, [entry(A1['sha256'], A1['path'], test_flag=True,
                                         auto_edit_eligible=False)], test_flag=True)
    with pytest.raises(ValueError, match='TEST_FLAG_REFUSED_IN_PRODUCTION'):
        import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    ok = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='test')
    assert len(ok['candidates']) == 1 and ok['report']['test_flag'] is True


def test_manifest_file_hash_and_version_gates(tmp_path):
    ref = manifest_file(tmp_path, [entry(A1['sha256'], A1['path'])])
    ref['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='MANIFEST_HASH_MISMATCH'):
        import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    ref2 = manifest_file(tmp_path, [entry(A1['sha256'], A1['path'])])
    data = json.loads(Path(ref2['path']).read_text(encoding='utf-8'))
    data['schema_version'] = 'material-manifest/v9'
    Path(ref2['path']).write_text(json.dumps(data), encoding='utf-8')
    ref2['sha256'] = hashlib.sha256(Path(ref2['path']).read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='UNSUPPORTED_MANIFEST_SCHEMA_VERSION'):
        import_material_manifest([asset('src1', A1['sha256'])], ref2, mode='production')


def test_stale_review_all_zero_hash_refused(tmp_path):
    stale = entry(A1['sha256'], A1['path'])
    stale['review'] = {'verdict': 'PASS', 'content_sha256': '0' * 64, 'reviewer': 'r', 'problems': []}
    ref = manifest_file(tmp_path, [stale])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert result['candidates'] == []
    assert {f['reason'] for f in result['failures']} == {'REVIEW_STALE'}


def test_missing_structural_fields_refused(tmp_path):
    missing_visual = entry(A1['sha256'], A1['path'])
    del missing_visual['visual']
    ref = manifest_file(tmp_path, [missing_visual])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert result['candidates'] == []
    assert {f['reason'] for f in result['failures']} == {'MANIFEST_ENTRY_INVALID'}


def test_declared_duration_lies_refused_against_real_media(tmp_path):
    lying = entry(A1['sha256'], A1['path'],
                  technical={'duration_sec': 1000.0, 'duration_source': 'fixture_inventory'},
                  usable_window={'start_sec': 0.0, 'end_sec': 999.2, 'grid_ticks': 1920})
    lying['review']['content_sha256'] = entry_content_sha256(lying)
    ref = manifest_file(tmp_path, [lying])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='test')
    assert result['candidates'] == []
    assert 'DURATION_MISMATCH_ACTUAL' in {f['reason'] for f in result['failures']}


def test_media_hash_mismatch_refused(tmp_path):
    swapped = entry(A1['sha256'], A2['path'])   # declares a1's hash but points at a2's file
    unit_visual_review_file(tmp_path, swapped)
    bind(swapped)
    ref = manifest_file(tmp_path, [swapped])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert result['candidates'] == []
    assert 'MEDIA_HASH_MISMATCH' in {f['reason'] for f in result['failures']}


def test_entry_level_refusals(tmp_path):
    sha, path = A1['sha256'], A1['path']

    def bad_entry(idx, **overrides):
        e = entry(sha, path, **overrides)
        # production eligibility (visual review + identity) is not what these cases
        # exercise — satisfy it so each targeted gate is reached and fires on its own
        # (unique file name: the case list is built eagerly, a shared name would be
        # overwritten before its import runs)
        unit_visual_review_file(tmp_path, e, name='vr-%02d.json' % idx)
        return bind(e)

    cases = [
        (bad_entry(1, product_id='prod-other'), 'PRODUCT_ID_MISMATCH'),
        (entry(hashlib.sha256(b'other').hexdigest(), path), 'ASSET_NOT_IN_JOB'),
        (bad_entry(3, audio={'has_original': True, 'must_preserve': True,
                             'policy': 'must_preserve_original'}), 'AUDIO_MUST_PRESERVE_EXCLUDED'),
        (bad_entry(4, restrictions=['no_zoom_crop']), 'RESTRICTION_UNSUPPORTED'),
        (bad_entry(5, vbench={'applicable': True, 'status': 'blocked', 'metrics': {}}), 'VBENCH_BLOCKED'),
        (bad_entry(6, vbench={'applicable': True, 'status': 'not_run', 'metrics': {}}), 'QUALITY_REVIEW_NOT_RUN'),
        (bad_entry(7, visual={'quality_flags': ['obscured_product'], 'occlusion': 'partial'}), 'POOR_QUALITY_FLAG'),
        (bad_entry(8, usable_window={'start_sec': 6.0, 'end_sec': 7.5, 'grid_ticks': 1920}), 'WINDOW_OUT_OF_RANGE'),
        (bad_entry(9, usable_window={'start_sec': 0.01, 'end_sec': 2.0, 'grid_ticks': 1920}),
         'WINDOW_NOT_GRID_ALIGNED'),
        (bad_entry(10, technical={'duration_sec': None, 'duration_source': 'unknown'}), 'DURATION_UNVERIFIED'),
        (bad_entry(11, technical={'duration_sec': 6.0, 'duration_source': 'unverified'}), 'DURATION_UNVERIFIED'),
        (entry(sha, path, vbench={'applicable': True, 'status': 'pass', 'metrics': {}}), 'MANIFEST_ENTRY_INVALID'),
    ]
    for bad, reason in cases:
        ref = manifest_file(tmp_path, [bad])
        if reason == 'PRODUCT_ID_MISMATCH':
            ref['product_id'] = 'prod-x'  # job declares its product; mismatched entries must be refused
        result = import_material_manifest([asset('src1', sha)], ref, mode='production')
        assert result['candidates'] == [], (bad.get('material_id'), reason)
        assert reason in {f['reason'] for f in result['failures']}, (reason, result['failures'])


def test_production_refuses_self_declared_ineligible_entries(tmp_path):
    """Coordinator counterexample (production-ineligible.json): entry flips test_flag
    False, vbench.applicable False, keeps auto_edit_eligible=False and recomputes the
    review hash. Production must refuse — an entry declining auto-edit eligibility
    never enters the auto-edit free pool."""
    ineligible = entry(A1['sha256'], A1['path'], test_flag=False, auto_edit_eligible=False,
                       vbench={'applicable': False, 'status': 'not_run', 'metrics': {}})
    ineligible['evidence']['provenance']['origin'] = 'existing_h3_generated_demo'
    ineligible['generated'] = {'status': 'existing_footage_demo',
                               'verified_real_product_test': False, 'provider': 'H3'}
    ineligible['review']['content_sha256'] = entry_content_sha256(ineligible)
    ref = manifest_file(tmp_path, [ineligible], test_flag=False)
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert result['candidates'] == []
    assert {f['reason'] for f in result['failures']} == {'PRODUCTION_ENTRY_NOT_ELIGIBLE'}
    # an applicability.auto_edit=false entry is equally refused even if eligible flag lies
    lying = dict(ineligible)
    lying['auto_edit_eligible'] = True
    lying['applicability'] = {'auto_edit': False, 'uses': ['packaging']}
    lying['review']['content_sha256'] = entry_content_sha256(lying)
    ref2 = manifest_file(tmp_path, [lying], test_flag=False)
    result2 = import_material_manifest([asset('src1', A1['sha256'])], ref2, mode='production')
    assert result2['candidates'] == [] and \
        {f['reason'] for f in result2['failures']} == {'PRODUCTION_ENTRY_NOT_ELIGIBLE'}


def test_coordinator_production_ineligible_file_reproduced():
    """Re-run the coordinator's saved adversarial file verbatim (path+hash bound)."""
    adv = Path(r'F:\codex\laox_director_lab\runtime\evidence\koc-v1\coordinator-adversarial\production-ineligible.json')
    if not adv.is_file():
        pytest.skip('coordinator adversarial file not present')
    result = import_material_manifest([asset('a1', A1['sha256']), asset('a2', A2['sha256'])],
                                      {'path': str(adv), 'sha256': _sha256(adv)}, mode='production')
    assert len(result['candidates']) == 0
    assert 'PRODUCTION_ENTRY_NOT_ELIGIBLE' in {f['reason'] for f in result['failures']}


def test_future_vbench_pass_requires_bound_evidence(tmp_path):
    # pass with metrics but no evidence hash/file at all
    forged = entry(A1['sha256'], A1['path'],
                   vbench={'applicable': True, 'status': 'pass', 'metrics': {'subject_consistency': 0.9}})
    bind(forged)
    ref = manifest_file(tmp_path, [forged])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='test')
    assert result['candidates'] == []
    assert {f['reason'] for f in result['failures']} & {'QUALITY_REVIEW_INVALID_STATUS', 'MANIFEST_ENTRY_INVALID'}
    # pass with metrics_evidence_sha256 but no evidence path is still refused
    no_file = entry(A1['sha256'], A1['path'],
                    vbench={'applicable': True, 'status': 'pass', 'metrics': {'subject_consistency': 0.9},
                            'metrics_evidence_sha256': 'a' * 64})
    bind(no_file)
    ref1 = manifest_file(tmp_path, [no_file])
    result1 = import_material_manifest([asset('src1', A1['sha256'])], ref1, mode='test')
    assert result1['candidates'] == []
    assert {f['reason'] for f in result1['failures']} & {'QUALITY_REVIEW_INVALID_STATUS', 'MANIFEST_ENTRY_INVALID'}
    # repair-1 (EK-12): the evidence FILE must parse as the normalized contract AND be
    # content-bound to THIS entry — the only accepted form
    real = entry(A1['sha256'], A1['path'],
                 vbench={'applicable': True, 'status': 'pass', 'metrics': {'subject_consistency': 0.9}})
    unit_metrics_report_file(tmp_path, real, {'subject_consistency': 0.9})
    unit_visual_review_file(tmp_path, real)
    bind(real)
    ref2 = manifest_file(tmp_path, [real])
    ok = import_material_manifest([asset('src1', A1['sha256'])], ref2, mode='production')
    assert len(ok['candidates']) == 1  # forward-compatible: content-bound evidence accepted


def test_metrics_report_content_binding_negatives(tmp_path):
    """EK-12 acceptance: wrong-video report, wrong metrics, failed/not_run report and
    arbitrary bytes are all refused; a stale optional version binding is refused."""
    sha, path = A1['sha256'], A1['path']

    def run_with(report_doc, raw_bytes=None, name='report.json'):
        e = entry(sha, path,
                  vbench={'applicable': True, 'status': 'pass', 'metrics': {'subject_consistency': 0.9}})
        p = tmp_path / name
        if raw_bytes is not None:
            p.write_bytes(raw_bytes)
        else:
            p.write_text(json.dumps(report_doc, ensure_ascii=False), encoding='utf-8')
        e['vbench']['metrics_evidence_sha256'] = _sha256(p)
        e['evidence']['metrics_evidence_path'] = str(p)
        unit_visual_review_file(tmp_path, e)
        bind(e)
        ref = manifest_file(tmp_path, [e])
        return import_material_manifest([asset('src1', sha)], ref, mode='production')

    # report for another video (all-zero source hash — the coordinator's exact shape)
    r = run_with({'source_sha256': '0' * 64, 'status': 'pass',
                  'metrics': {'subject_consistency': 0.9},
                  'test_fixture': 'NEGATIVE ONLY, NOT REAL VBENCH SCORES'})
    assert r['candidates'] == []
    assert 'QUALITY_REVIEW_INVALID_STATUS' in {f['reason'] for f in r['failures']}
    # report whose metrics differ from the declared ones
    r = run_with({'source_sha256': sha, 'status': 'pass',
                  'metrics': {'subject_consistency': 0.1}}, name='r2.json')
    assert r['candidates'] == []
    # failed / not_run report cannot back a pass claim
    r = run_with({'source_sha256': sha, 'status': 'fail',
                  'metrics': {'subject_consistency': 0.9}}, name='r3.json')
    assert r['candidates'] == []
    r = run_with({'source_sha256': sha, 'status': 'not_run', 'metrics': {}}, name='r4.json')
    assert r['candidates'] == []
    # arbitrary bytes (file hash still matches — content is not a JSON report)
    r = run_with(None, raw_bytes=b'\x00\x01not-json\xff', name='r5.bin')
    assert r['candidates'] == []
    # optional stale version binding
    r = run_with({'source_sha256': sha, 'status': 'pass',
                  'metrics': {'subject_consistency': 0.9}, 'version': 7}, name='r6.json')
    assert r['candidates'] == []
    # a correct report (UNIT ONLY labeled) passes — see positive tests above
    r = run_with({'source_sha256': sha, 'status': 'pass', 'metrics': {'subject_consistency': 0.9},
                  'material_id': 'mat-001', 'version': 1,
                  'window': {'start_sec': 0.0, 'end_sec': 2.0}, 'test_fixture': UNIT_NOTE},
                 name='r7.json')
    assert len(r['candidates']) == 1


def test_visual_review_stale_version_and_window_refused(tmp_path):
    """EK-13 acceptance: visual-review evidence bound to a stale version or a
    different window must not unlock production eligibility."""
    sha, path = A1['sha256'], A1['path']
    stale_version = entry(sha, path)
    unit_visual_review_file(tmp_path, stale_version, version=3)
    bind(stale_version)
    ref = manifest_file(tmp_path, [stale_version])
    r = import_material_manifest([asset('src1', sha)], ref, mode='production')
    assert r['candidates'] == []
    assert {f['reason'] for f in r['failures']} == {'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING'}
    other_window = entry(sha, path)
    unit_visual_review_file(tmp_path, other_window,
                            window={'start_sec': 2.0, 'end_sec': 4.0})
    bind(other_window)
    ref2 = manifest_file(tmp_path, [other_window])
    r2 = import_material_manifest([asset('src1', sha)], ref2, mode='production')
    assert r2['candidates'] == []
    assert {f['reason'] for f in r2['failures']} == {'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING'}


def test_product_mismatch_refused_even_when_text_would_bigram_match(tmp_path):
    wrong = entry(A1['sha256'], A1['path'], product_id='prod-other')
    wrong['semantics']['observation_texts'] = ['把果子对半切开。切面的特写，籽粒看得很清楚。']
    wrong['review']['content_sha256'] = entry_content_sha256(wrong)
    ref = manifest_file(tmp_path, [wrong])
    ref['product_id'] = 'prod-x'
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert result['candidates'] == []
    assert {f['reason'] for f in result['failures']} == {'PRODUCT_ID_MISMATCH'}


def test_must_preserve_never_becomes_silent_free_pool_candidate(tmp_path):
    ref = manifest_file(tmp_path, [entry(A1['sha256'], A1['path'],
                                         audio={'has_original': True, 'must_preserve': True,
                                                'policy': 'must_preserve_original'})])
    imported = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert imported['candidates'] == []
    library = build_shot_library([asset('src1', A1['sha256'])], [], {'job_id': 'j', 'revision': 1},
                                 material_candidates=imported['candidates'])
    assert library['candidates'] == []  # nothing silently entered the free pool


def test_production_forces_media_verification_despite_verify_media_false(tmp_path):
    swapped = entry(A1['sha256'], A2['path'])   # declares a1's hash but points at a2's file
    unit_visual_review_file(tmp_path, swapped)
    bind(swapped)
    ref = manifest_file(tmp_path, [swapped])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production',
                                      verify_media=False)  # production must ignore the opt-out
    assert result['candidates'] == []
    assert 'MEDIA_HASH_MISMATCH' in {f['reason'] for f in result['failures']}


def test_probe_cache_bound_to_file_hash_not_path(tmp_path):
    media = tmp_path / 'media.mp4'
    media.write_bytes(Path(A1['path']).read_bytes())
    first = entry(_sha256(media), str(media))
    ref = manifest_file(tmp_path, [first])
    ok = import_material_manifest([asset('src1', _sha256(media))], ref, mode='test')
    assert len(ok['candidates']) == 1
    # same path, different bytes: the probe must rebind to the new hash, not reuse a1's duration
    media.write_bytes(Path(A2['path']).read_bytes())
    lying = entry(_sha256(media), str(media),
                  technical={'duration_sec': 6.583333, 'duration_source': 'fixture_inventory'},
                  usable_window={'start_sec': 4.0, 'end_sec': 6.0, 'grid_ticks': 1920})
    lying['review']['content_sha256'] = entry_content_sha256(lying)
    ref2 = manifest_file(tmp_path, [lying])
    result = import_material_manifest([asset('src1', _sha256(media))], ref2, mode='test')
    # a1's stale duration would have accepted the declared 6.583s; the hash-bound probe
    # re-probed and caught a2's real ~5.167s (either refusal proves the rebinding)
    assert result['candidates'] == []
    assert {f['reason'] for f in result['failures']} & {'DURATION_MISMATCH_ACTUAL', 'WINDOW_OUT_OF_RANGE'}


def test_production_eligibility_requires_real_quality_evidence(tmp_path):
    sha, path = A1['sha256'], A1['path']
    h3_origin = entry(sha, path, vbench={'applicable': False, 'status': 'not_run', 'metrics': {}})
    h3_origin['evidence']['provenance']['origin'] = 'existing_h3_generated_demo'
    h3_origin['generated'] = {'status': 'existing_footage_demo',
                              'verified_real_product_test': False, 'provider': 'H3'}
    bind(h3_origin)
    ref = manifest_file(tmp_path, [h3_origin])
    result = import_material_manifest([asset('src1', sha)], ref, mode='production')
    assert 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING' in {f['reason'] for f in result['failures']}
    # repair-1 (EK-13): a self-declared clean real_world_recording WITHOUT a parsed
    # visual-review evidence file is refused too — not-applicable is not visual review
    real_no_review = entry(sha, path)
    bind(real_no_review)
    ref1 = manifest_file(tmp_path, [real_no_review])
    result1 = import_material_manifest([asset('src1', sha)], ref1, mode='production')
    assert result1['candidates'] == []
    assert 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING' in {f['reason'] for f in result1['failures']}
    # unverified identity stays blocked even with a visual-review file present
    unverified = entry(sha, path)
    unverified['evidence']['provenance']['product_identity_verified'] = False
    unit_visual_review_file(tmp_path, unverified)
    bind(unverified)
    ref2 = manifest_file(tmp_path, [unverified])
    result2 = import_material_manifest([asset('src1', sha)], ref2, mode='production')
    assert result2['candidates'] == []
    assert 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING' in {f['reason'] for f in result2['failures']}
    # honest real-world recording + identity + bound visual-review evidence: accepted
    real = entry(sha, path)
    unit_visual_review_file(tmp_path, real)
    bind(real)
    ref3 = manifest_file(tmp_path, [real])
    ok = import_material_manifest([asset('src1', sha)], ref3, mode='production')
    assert len(ok['candidates']) == 1


def test_full_schema_validation_applies(tmp_path):
    bad_version = entry(A1['sha256'], A1['path'], version=0)   # schema: minimum 1
    bad_version['review']['content_sha256'] = entry_content_sha256(bad_version)
    ref = manifest_file(tmp_path, [bad_version])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    assert result['candidates'] == []
    assert {f['reason'] for f in result['failures']} == {'MANIFEST_ENTRY_INVALID'}


def test_material_candidates_join_native_library_and_legacy_path_identical(tmp_path):
    e = entry(A1['sha256'], A1['path'])
    unit_visual_review_file(tmp_path, e)
    bind(e)
    ref = manifest_file(tmp_path, [e])
    imported = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='production')
    narrato = [{'asset_id': 'src1', 'source_sha256': A1['sha256'], 'usable': True, 'duration_ticks': 316000,
                'coverage': [{'in_tick': 200000, 'out_tick': 288000, 'status': 'success',
                              'observations': [{'text': '果肉特写', 'frame_id': 'f', 'source_tick': 200000}]}],
                'detect': None, 'product_identity_verified': False, 'analysis_evidence_sha256': 'd' * 64}]
    merged = build_shot_library([asset('src1', A1['sha256'])], [dict(r) for r in narrato],
                                {'job_id': 'j', 'revision': 1}, material_candidates=imported['candidates'])
    legacy = build_shot_library([asset('src1', A1['sha256'])], [dict(r) for r in narrato],
                                {'job_id': 'j', 'revision': 1})
    assert len(merged['candidates']) == len(legacy['candidates']) + 1
    windows = sorted((c['in_tick'], c['out_tick'], c['candidate_id'].split('-')[0])
                     for c in merged['candidates'])
    assert (0, 96000, 'mcat') in windows and (200000, 288000, 'cand') in windows
    material_candidate = next(c for c in merged['candidates'] if c['candidate_id'].startswith('mcat-'))
    assert material_candidate['evidence']['frame_count'] == 0
    assert material_candidate['evidence']['analysis_evidence_sha256'] == ref['sha256']
    # a material candidate without the contract prefix is refused (no fake branch)
    rogue = dict(imported['candidates'][0])
    rogue['candidate_id'] = 'cand-' + rogue['candidate_id'][5:]
    with pytest.raises(ValueError, match='MATERIAL_CANDIDATE_ID_PREFIX_MISSING'):
        build_shot_library([asset('src1', A1['sha256'])], [], {}, material_candidates=[rogue])


ADV_DIR = Path(r'F:\codex\laox_director_lab\runtime\evidence\koc-v1\coordinator-adversarial')


def _adv_ref(name):
    p = ADV_DIR / name
    return {'path': str(p), 'sha256': _sha256(p)}


def test_coordinator_wrong_video_quality_binding_file_refused():
    """Coordinator adversarial file verbatim (repair-1 finding 1): the metrics report's
    source_sha256 is all-zero (another video); production must import ZERO candidates."""
    result = import_material_manifest([asset('a1', A1['sha256']), asset('a2', A2['sha256'])],
                                      _adv_ref('wrong-video-quality-binding.json'), mode='production')
    assert len(result['candidates']) == 0
    reasons = {f['reason'] for f in result['failures']}
    assert 'QUALITY_REVIEW_INVALID_STATUS' in reasons
    detail = next(f for f in result['failures'] if f['reason'] == 'QUALITY_REVIEW_INVALID_STATUS')
    assert 'another/zero video' in detail.get('detail', '')


def test_coordinator_missing_production_visual_evidence_file_refused():
    """Coordinator adversarial file verbatim (repair-1 finding 2): no visual-review
    evidence at all + unverified identity; production must import ZERO candidates."""
    result = import_material_manifest([asset('a1', A1['sha256']), asset('a2', A2['sha256'])],
                                      _adv_ref('missing-production-visual-evidence.json'), mode='production')
    assert len(result['candidates']) == 0
    assert 'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING' in {f['reason'] for f in result['failures']}


def test_coordinator_invalid_manifest_header_file_refused():
    """Coordinator adversarial file verbatim (repair-1 finding 3): root drops
    created_utc/source_case and carries an unknown root field — whole-manifest
    refusal even in test mode."""
    with pytest.raises(ValueError, match='MANIFEST_STRUCTURE_INVALID'):
        import_material_manifest([asset('a1', A1['sha256'])],
                                 _adv_ref('invalid-manifest-header.json'), mode='test')


def test_manifest_root_contract_rejects_missing_and_unknown_root_fields(tmp_path):
    good = entry(A1['sha256'], A1['path'])
    ref = manifest_file(tmp_path, [good])
    data = json.loads(Path(ref['path']).read_text(encoding='utf-8'))
    del data['created_utc']
    del data['source_case']
    Path(ref['path']).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    ref['sha256'] = _sha256(ref['path'])
    with pytest.raises(ValueError, match='MANIFEST_STRUCTURE_INVALID'):
        import_material_manifest([asset('src1', A1['sha256'])], ref, mode='test')
    data2 = json.loads(Path(ref['path']).read_text(encoding='utf-8'))
    data2['created_utc'] = '2026-10-11T00:00:00'
    data2['source_case'] = 'c1'
    data2['unexpected_field'] = 'invalid per shipped schema'
    Path(ref['path']).write_text(json.dumps(data2, ensure_ascii=False), encoding='utf-8')
    ref['sha256'] = _sha256(ref['path'])
    with pytest.raises(ValueError, match='MANIFEST_STRUCTURE_INVALID'):
        import_material_manifest([asset('src1', A1['sha256'])], ref, mode='test')


def test_non_object_entries_fail_explicitly_without_crashing_valid_sibling(tmp_path):
    """EK-14 acceptance: malformed non-object entries are refused per-entry with an
    explicit reason; a valid sibling entry still imports."""
    good = entry(A1['sha256'], A1['path'])
    ref = manifest_file(tmp_path, ['just-a-string', 42, good])
    result = import_material_manifest([asset('src1', A1['sha256'])], ref, mode='test')
    assert len(result['candidates']) == 1
    invalid = [f for f in result['failures'] if f['reason'] == 'MANIFEST_ENTRY_INVALID']
    assert len(invalid) == 2
    assert {f.get('detail', '').startswith('entry_not_object') for f in invalid} == {True}


def test_non_object_manifest_root_refused_explicitly(tmp_path):
    """Coordinator non-object-root negative (msg_96ee3c9f760d): a JSON list (or string)
    at the manifest ROOT must fail with MANIFEST_STRUCTURE_INVALID, never an
    AttributeError from calling .get on it."""
    for raw in ('[]', '"a-string-root"', '42'):
        p = tmp_path / 'root.json'
        p.write_text(raw, encoding='utf-8')
        ref = {'path': str(p), 'sha256': _sha256(p)}
        with pytest.raises(ValueError, match='MANIFEST_STRUCTURE_INVALID'):
            import_material_manifest([asset('src1', A1['sha256'])], ref, mode='test')


def test_visual_review_contract_requires_checked_utc_and_identity(tmp_path):
    """Coordinator visual-contract negative (msg_96ee3c9f760d): the parsed visual-review
    doc is validated against the delivered evidence contract — a doc missing
    checked_utc or the explicit product-identity check must NOT unlock production,
    and neither may an unverified / wrongly-bound identity check."""
    sha, path = A1['sha256'], A1['path']

    def run_with(doc):
        e = entry(sha, path)
        p = tmp_path / 'vr.json'
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        e['evidence']['visual_review_path'] = str(p)
        e['evidence']['visual_review_sha256'] = _sha256(p)
        bind(e)
        ref = manifest_file(tmp_path, [e])
        return import_material_manifest([asset('src1', sha)], ref, mode='production')

    def good_doc():
        e = entry(sha, path)
        return {
            'evidence_type': 'visual-review/v1', 'source_sha256': sha,
            'material_id': e['material_id'], 'version': 1,
            'window': {'start_sec': 0.0, 'end_sec': 2.0}, 'verdict': 'pass',
            'reviewer': 'unit', 'checked_utc': '2026-10-11T00:00:00',
            'product_identity': {'verified': True, 'product_id': e['product_id']},
            'test_fixture': UNIT_NOTE,
        }

    missing_utc = good_doc()
    del missing_utc['checked_utc']          # coordinator: _visual_review_detail hole
    r = run_with(missing_utc)
    assert r['candidates'] == [] and \
        {f['reason'] for f in r['failures']} == {'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING'}
    missing_identity = good_doc()
    del missing_identity['product_identity']
    r = run_with(missing_identity)
    assert r['candidates'] == []
    unverified_identity = good_doc()
    unverified_identity['product_identity'] = {'verified': False, 'product_id': 'prod-x'}
    r = run_with(unverified_identity)
    assert r['candidates'] == []
    wrong_product_identity = good_doc()
    wrong_product_identity['product_identity'] = {'verified': True, 'product_id': 'prod-other'}
    r = run_with(wrong_product_identity)
    assert r['candidates'] == []
    assert {f['reason'] for f in r['failures']} == {'PRODUCTION_ELIGIBILITY_EVIDENCE_MISSING'}


def _a2_entry():
    """Valid sibling entry bound to the a2 fixture (real media, probe-backed)."""
    return entry(A2['sha256'], A2['path'],
                 technical={'duration_sec': 5.166667, 'duration_source': 'probe_evidence'},
                 usable_window={'start_sec': 0.0, 'end_sec': 2.0, 'grid_ticks': 1920})


@pytest.mark.parametrize('bad_sha', [[], {'k': 'v'}, None, 12345, 3.5], ids=['list', 'dict', 'null', 'int', 'float'])
def test_nonhashable_source_sha_refused_explicitly_without_crash(tmp_path, bad_sha):
    """EK-17 acceptance (UNIT ONLY — synthetic validator negatives, not production
    evidence): a non-hash source_sha256 (list/dict/null/numeric) must be refused as
    MANIFEST_ENTRY_INVALID by the validation gates — never a TypeError from
    bound_shas.add — and must not be admitted into bound_shas; a valid sibling entry
    on another asset keeps importing."""
    bad = entry(A1['sha256'], A1['path'], source_sha256=bad_sha)
    sibling = bind(_a2_entry())
    ref = manifest_file(tmp_path, [bad, sibling])
    result = import_material_manifest([asset('src1', A1['sha256']), asset('src2', A2['sha256'])],
                                      ref, mode='test')
    assert len(result['candidates']) == 1                      # sibling survives
    assert result['candidates'][0]['source_sha256'] == A2['sha256']
    invalid = [f for f in result['failures'] if f['reason'] == 'MANIFEST_ENTRY_INVALID']
    assert len(invalid) == 1
    assert invalid[0].get('detail', '').startswith('schema:source_sha256')
    assert result['bound_shas'] == {A2['sha256']}              # bad value not admitted


@pytest.mark.parametrize('field_path,bad_value', [
    ('audio/policy', []),
    ('technical/duration_source', {'x': 1}),
    ('vbench/status', []),
    ('generated/status', {}),
    ('evidence/provenance', []),
    ('evidence/provenance/origin', {}),
    ('usable_window/start_sec', []),
], ids=['audio-policy', 'duration-source', 'vbench-status', 'generated-status',
        'provenance', 'provenance-origin', 'window-start'])
def test_nested_nonhashable_field_values_refused_without_crash(tmp_path, field_path, bad_value):
    """EK-17 nested acceptance (UNIT ONLY — synthetic negatives; coordinator follow-up
    msg_c4748bb729f7): an unhashable nested container value (audio.policy=[] class)
    must be refused by the FULL SCHEMA gate BEFORE any unsafe type operation — never a
    TypeError in the structural set-membership walk — while the entry's still-valid
    declared source SHA stays bound and a valid sibling entry keeps importing."""
    bad = entry(A1['sha256'], A1['path'])
    target = bad
    keys = field_path.split('/')
    for k in keys[:-1]:
        target = target[k]
    target[keys[-1]] = bad_value
    bad['review']['content_sha256'] = entry_content_sha256(bad)
    sibling = bind(_a2_entry())
    ref = manifest_file(tmp_path, [bad, sibling])
    result = import_material_manifest([asset('src1', A1['sha256']), asset('src2', A2['sha256'])],
                                      ref, mode='test')
    assert len(result['candidates']) == 1                      # sibling survives
    assert result['candidates'][0]['source_sha256'] == A2['sha256']
    invalid = [f for f in result['failures'] if f['reason'] == 'MANIFEST_ENTRY_INVALID']
    assert len(invalid) == 1
    assert invalid[0].get('detail', '').startswith('schema:%s' % field_path)
    assert result['bound_shas'] == {A1['sha256'], A2['sha256']}  # valid SHA stays bound


def test_malformed_entry_with_valid_sha_stays_bound_and_blocks_narrato_bypass(tmp_path):
    """EK-17 protection half (UNIT ONLY): a malformed entry that still declares a
    hash-shaped media SHA must KEEP that SHA in bound_shas — the manifest gate governs
    the refused media's Narrato analysis (pipeline run_v2 drops bound analysis refs),
    so failed Manifest material cannot re-enter the pool through Narrato. Valid
    siblings are bound too."""
    malformed = entry(A1['sha256'], A1['path'])
    del malformed['visual']                                   # structural malformation, valid SHA
    malformed['review']['content_sha256'] = entry_content_sha256(malformed)
    sibling = bind(_a2_entry())
    ref = manifest_file(tmp_path, [malformed, sibling])
    result = import_material_manifest([asset('src1', A1['sha256']), asset('src2', A2['sha256'])],
                                      ref, mode='test')
    assert {f['reason'] for f in result['failures']} == {'MANIFEST_ENTRY_INVALID'}
    assert len(result['candidates']) == 1                      # only the sibling imports
    assert result['bound_shas'] == {A1['sha256'], A2['sha256']}  # refused entry stays bound
    # pipeline.py run_v2 consumption semantics: analysis refs of bound assets are
    # dropped before the library — neither the refused media nor the sibling can
    # come back through a Narrato-style analysis of the same files
    narrato = [{'asset_id': aid, 'source_sha256': sha, 'usable': True, 'duration_ticks': 316000,
                'coverage': [{'in_tick': 0, 'out_tick': 96000, 'status': 'success',
                              'observations': [{'text': '果肉特写', 'frame_id': 'f', 'source_tick': 0}]}],
                'detect': None, 'product_identity_verified': False, 'analysis_evidence_sha256': 'd' * 64}
               for aid, sha in (('src1', A1['sha256']), ('src2', A2['sha256']))]
    filtered = [r for r in narrato if r.get('source_sha256') not in result['bound_shas']]
    assert filtered == []
    library = build_shot_library([asset('src1', A1['sha256']), asset('src2', A2['sha256'])],
                                 filtered, {'job_id': 'j', 'revision': 1},
                                 material_candidates=result['candidates'])
    assert all(c['candidate_id'].startswith('mcat-') for c in library['candidates'])
    assert {c['source_sha256'] for c in library['candidates']} == {A2['sha256']}


def _coordinator_nonhashable_reproduced(name, expected_detail_prefix):
    adv = Path(r'F:\codex\laox_director_lab\runtime\evidence\koc-v1\repair-1') / name
    if not adv.is_file():
        pytest.skip('coordinator nonhashable evidence file not present')
    result = import_material_manifest([asset('a1', A1['sha256']), asset('a2', A2['sha256'])],
                                      {'path': str(adv), 'sha256': _sha256(adv)}, mode='test')
    entries = json.loads(adv.read_text(encoding='utf-8'))['entries']
    assert len(result['candidates']) == len(entries) - 1       # 20 valid siblings
    assert len(result['failures']) == 1
    refused = result['failures'][0]
    assert refused['material_id'] == 'mat-koc-dragonfruit-test-shot-001'
    assert refused['reason'] == 'MANIFEST_ENTRY_INVALID'
    assert refused.get('detail', '').startswith(expected_detail_prefix)
    assert result['bound_shas'] == {A1['sha256'], A2['sha256']}


def test_coordinator_nonhashable_entry_file_reproduced():
    """Coordinator adversarial file verbatim (repair-2 / EK-17 evidence): first entry
    carries source_sha256=[] in an otherwise valid TEST manifest. Import in test mode
    with this job's own a1/a2 assets must refuse exactly that entry
    (MANIFEST_ENTRY_INVALID, no TypeError) and import every valid sibling."""
    _coordinator_nonhashable_reproduced('coordinator-nonhashable-entry.json',
                                        'schema:source_sha256')


def test_coordinator_nonhashable_nested_file_reproduced():
    """Coordinator nested adversarial file verbatim (msg_c4748bb729f7): first entry
    carries audio.policy=[] but a VALID declared SHA. The full Schema gate must refuse
    the entry (no TypeError in the structural walk) while its valid SHA stays bound —
    the refused media cannot bypass the manifest gate through Narrato analysis."""
    _coordinator_nonhashable_reproduced('coordinator-nonhashable-nested.json',
                                        'schema:audio/policy')
