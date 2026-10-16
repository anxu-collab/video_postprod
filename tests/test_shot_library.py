"""Counterexamples for the batch candidate shot library (S3); written before implementation."""
import hashlib, json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def build(assets, refs, config=None):
    from scheduler.shot_library import build_shot_library
    return build_shot_library(assets, refs, config or {})


def asset(aid, role='product', mode='broll', analysis='single_shot'):
    return {'asset_id': aid, 'sha256': hashlib.sha256(aid.encode()).hexdigest(), 'role': role,
            'mode': mode, 'analysis': {'mode': analysis}}


def obs(text, tick):
    return {'text': text, 'frame_id': 'f%d' % tick, 'source_tick': tick}


def ref(aid, coverage, detect=None, ok=True, duration=960000, identity=False):
    return {'asset_id': aid, 'source_sha256': hashlib.sha256(aid.encode()).hexdigest(),
            'usable': ok, 'duration_ticks': duration, 'coverage': coverage,
            'detect': detect, 'product_identity_verified': identity,
            'analysis_evidence_sha256': hashlib.sha256(json.dumps(coverage).encode()).hexdigest()}


def test_schema_file_valid():
    schema = json.loads((ROOT / 'contracts/shot-library-v1.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)


def test_packaging_and_open_box_of_one_20s_source_stay_separate():
    lib = build([asset('src1')],
                [ref('src1', [
                    {'in_tick': 96000, 'out_tick': 240000, 'status': 'success',
                     'observations': [obs('红色包装盒正面，标签清晰', 120000)]},
                    {'in_tick': 384000, 'out_tick': 576000, 'status': 'success',
                     'observations': [obs('双手撕开包装，取出里面的果实', 400000)]},
                ])])
    assert len(lib['candidates']) == 2
    # r3: display kinds per observation ('里面' in the opening text is a detail
    # descriptor); the opening action is per-observation metadata, not a window claim
    uses = sorted(c['use'] for c in lib['candidates'])
    assert uses == ['detail', 'packaging']
    opening = next(c for c in lib['candidates'] if c['in_tick'] == 384000)
    assert any(a['kind'] == 'open_packaging' and a['evidence'] == 'attested_tick'
               for a in opening.get('supported_actions', []))
    assert lib['candidates'][0]['in_tick'] == 96000 and lib['candidates'][0]['out_tick'] == 240000


def test_failed_analysis_missing_frames_and_bad_times_not_selectable():
    lib = build([asset('bad'), asset('good')],
                [ref('bad', [{'in_tick': 0, 'out_tick': 48000, 'status': 'failed', 'observations': []}], ok=False),
                 ref('good', [{'in_tick': 0, 'out_tick': 96000, 'status': 'success',
                               'observations': [obs('果实特写', 48000)]}])])
    cand = {c['source_asset_id']: c for c in lib['candidates']}
    assert all(c['usable'] for c in cand.values())
    assert any(f['asset_id'] == 'bad' for f in lib['failures'])
    # a window beyond the probe duration is rejected even if analysis claims success
    lib2 = build([asset('weird')], [ref('weird', [{'in_tick': 0, 'out_tick': 960000 + 48000, 'status': 'success',
                                                   'observations': [obs('画面', 48000)]}])])
    assert all(not c['usable'] for c in lib2['candidates']) or not lib2['candidates']


def test_fixed_assets_never_enter_free_pool():
    lib = build([asset('open', role='fixed_opening', mode='spoken_locked'),
                 asset('body', role='product')],
                [ref('open', [{'in_tick': 0, 'out_tick': 96000, 'status': 'success',
                               'observations': [obs('人物开口说话', 48000)]}]),
                 ref('body', [{'in_tick': 0, 'out_tick': 96000, 'status': 'success',
                               'observations': [obs('商品放在桌上', 48000)]}])])
    assert [c['source_asset_id'] for c in lib['candidates']] == ['body']


def test_detect_scenes_candidates_from_cuts_without_crossing_unverified_boundaries():
    cuts = [0, 240000, 384000, 960000]
    lib = build([asset('multi', analysis='detect_scenes')],
                [ref('multi', [{'in_tick': 0, 'out_tick': 960000, 'status': 'success',
                                'observations': [obs('包装盒正面', 100000), obs('打开盒子', 300000), obs('果实特写', 500000)]}],
                     detect={'cuts': cuts, 'config_hash': 'det-v1', 'detector': 'content-v1'})])
    windows = [(c['in_tick'], c['out_tick']) for c in lib['candidates']]
    assert windows == [(0, 240000), (240000, 384000), (384000, 960000)]
    # cut points are scene evidence, not action semantics: display use comes from
    # each window's own observations; the action is metadata, never a window claim
    by_span = {(c['in_tick']): c for c in lib['candidates']}
    assert by_span[0]['use'] == 'packaging'
    assert by_span[240000]['use'] == 'packaging'
    assert any(a['kind'] == 'open_packaging' and a['evidence'] == 'attested_tick'
               for a in by_span[240000].get('supported_actions', []))


def test_candidate_ids_stable_and_independent_of_upload_order_and_script():
    def lib_with_order(order):
        refs = {r['asset_id']: r for r in [
            ref('alpha', [{'in_tick': 0, 'out_tick': 96000, 'status': 'success', 'observations': [obs('包装', 48000)]}]),
            ref('beta', [{'in_tick': 48000, 'out_tick': 144000, 'status': 'success', 'observations': [obs('开盒', 96000)]}])]}
        return build([asset(a) for a in order], [refs[a] for a in order])
    a = lib_with_order(['alpha', 'beta'])
    b = lib_with_order(['beta', 'alpha'])
    ids_a = [c['candidate_id'] for c in sorted(a['candidates'], key=lambda c: (c['source_sha256'], c['in_tick']))]
    ids_b = [c['candidate_id'] for c in sorted(b['candidates'], key=lambda c: (c['source_sha256'], c['in_tick']))]
    assert ids_a == ids_b
    # deterministic ordering regardless of insertion order
    assert [c['source_asset_id'] for c in a['candidates']] == ['alpha', 'beta']
    assert [c['source_asset_id'] for c in b['candidates']] == ['alpha', 'beta']


def test_identity_and_uncertainty_fields_recorded_not_guessed():
    lib = build([asset('x')], [ref('x', [{'in_tick': 0, 'out_tick': 96000, 'status': 'success',
                                          'observations': [obs('类似火龙果的果实', 48000)]}], identity=False)])
    c = lib['candidates'][0]
    assert c['product_identity_verified'] is False
    assert c['occlusion'] == 'unknown'          # not guessed from a single frame sample
    assert c['usable'] is True                  # usable for generic shots, identity stays unverified
