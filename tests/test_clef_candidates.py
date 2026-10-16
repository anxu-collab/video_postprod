import hashlib
from scheduler.clef_candidates import build_candidate_snapshot


def test_snapshot_keeps_short_duplicate_locked_failed_and_manifest_refusal():
    sha = hashlib.sha256(b'source').hexdigest()
    assets = [
        {'asset_id': 'a', 'sha256': sha, 'product_id': 'p', 'role': 'product', 'path': 'a.mp4'},
        {'asset_id': 'locked', 'sha256': 'b' * 64, 'product_id': 'p', 'role': 'fixed', 'path': 'fixed.mp4'},
    ]
    refs = [
        {'asset_id': 'a', 'source_sha256': sha, 'duration_ticks': 480000, 'coverage': [
            {'in_tick': 0, 'out_tick': 9600, 'status': 'success', 'scene': 'kitchen', 'observations': []},
            {'in_tick': 9600, 'out_tick': 19200, 'status': 'failed', 'error': 'timeout'},
            {'in_tick': 19200, 'out_tick': 28800, 'status': 'unknown'},
        ]},
        {'asset_id': 'locked', 'source_sha256': 'b' * 64, 'duration_ticks': 96000, 'coverage': [
            {'in_tick': 0, 'out_tick': 96000, 'status': 'success', 'observations': []},
        ]},
    ]
    native = {'candidate_id': 'old-1', 'source_asset_id': 'a', 'source_sha256': sha,
              'source_path': 'a.mp4', 'in_tick': 0, 'out_tick': 9600,
              'usable': False, 'unusable_reason': 'WINDOW_BELOW_MIN_TICKS', 'speech_locked': False}
    library = {'candidates': [native], 'failures': [{'asset_id': 'missing', 'reason': 'ANALYSIS_MISSING_FOR_ASSET'}]}
    report = {'entries': [{'asset_id': 'a', 'status': 'refused', 'reason': 'MANIFEST_STALE'}]}
    snapshot = build_candidate_snapshot(assets, refs, library, material_report=report)
    assert snapshot['schema_version'] == 'clef-candidates/v1'
    rows = snapshot['candidates']
    short = next(c for c in rows if c['native'] == native)
    assert 'WINDOW_BELOW_MIN_TICKS' not in short['gate_reasons']
    clean_native = build_candidate_snapshot([assets[0]], [refs[0]], {'candidates': [native]})
    short_clean = next(c for c in clean_native['candidates'] if c['native'] == native)
    assert short_clean['selection_eligible'] is True
    assert 'MANIFEST_STALE' in short['gate_reasons']
    assert short['duration_sec'] == 0.2
    assert any(c['quality_status'] == 'failed' and c['native'] is None for c in rows)
    assert any(c['quality_status'] == 'unknown' and c['native'] is None for c in rows)
    assert any(c['asset_id'] == 'locked' and not c['selection_eligible'] for c in rows)
    assert any('MANIFEST_STALE' in c['gate_reasons'] for c in rows if c['asset_id'] == 'a')
    assert snapshot['analysis_failures'] == library['failures']
    clean = build_candidate_snapshot([assets[0]], [refs[0]], {'candidates': []})
    raw = next(c for c in clean['candidates'] if c['quality_status'] == 'success')
    assert raw['native'] is None and raw['selection_eligible'] is True


def test_candidate_identity_binds_source_hash_and_interval_but_not_description():
    sha = 'a' * 64
    asset = {'asset_id': 'a', 'sha256': sha, 'role': 'product', 'path': 'a.mp4'}
    ref = {'asset_id': 'a', 'source_sha256': sha, 'coverage': [
        {'in_tick': 0, 'out_tick': 24000, 'status': 'success', 'observations': []}]}
    first = build_candidate_snapshot([asset], [ref], {'candidates': []})['candidates'][0]
    ref['coverage'][0]['out_tick'] = 48000
    second = build_candidate_snapshot([asset], [ref], {'candidates': []})['candidates'][0]
    assert first['candidate_id'] != second['candidate_id']


def test_fixed_audio_remains_visible_and_cannot_be_silent_broll():
    sha = 'c' * 64
    asset = {'asset_id': 'opening', 'sha256': sha, 'role': 'fixed_opening', 'path': 'opening.mp4'}
    ref = {'asset_id': 'opening', 'source_sha256': sha, 'coverage': [
        {'in_tick': 0, 'out_tick': 96000, 'status': 'success', 'observations': []}]}
    candidate = build_candidate_snapshot([asset], [ref], {'candidates': []})['candidates'][0]
    assert candidate['native'] is None
    assert candidate['audio_policy'] == 'must_preserve_original'
    assert candidate['selection_eligible'] is False
    assert 'FIXED_OR_LOCKED_ASSET' in candidate['gate_reasons']


def test_stage_a_missing_analysis_and_manifest_bound_hash_stay_visible_but_blocked():
    sha = 'd' * 64
    assets = [{'asset_id': 'a-v2', 'sha256': sha, 'role': 'product', 'path': 'a.mp4'}]
    unknown = build_candidate_snapshot(assets, [], {})['candidates'][0]
    assert unknown['quality_status'] == 'unknown'
    assert unknown['duration_sec'] is None and unknown['native'] is None
    ref = {'asset_id': 'a-v2', 'source_sha256': sha, 'coverage': [
        {'in_tick': 0, 'out_tick': 48000, 'status': 'success',
         'observations': [{'source_tick': 24000, 'text': '正在讲话，对着镜头介绍'}]}]}
    imported = {'bound_shas': {sha}, 'report': {'refused': [
        {'material_id': 'bad-entry', 'product_id': '?', 'reason': 'MANIFEST_ENTRY_INVALID'}]}}
    snapshot = build_candidate_snapshot(assets, [ref], {}, material_report=imported)
    row = snapshot['candidates'][0]
    assert row['description'] == '正在讲话，对着镜头介绍'
    assert row['audio_policy'] == 'must_preserve_original'
    assert 'ORIGINAL_AUDIO_MUST_BE_PRESERVED' in row['gate_reasons']
    assert 'MANIFEST_BOUND_RAW_ANALYSIS_BLOCKED' in row['gate_reasons']
    import json
    json.dumps(snapshot, ensure_ascii=False)  # set-valued importer fields are serialized safely
