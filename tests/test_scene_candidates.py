"""Scene-detection candidate handling (scheduler side) and worker op boundary.

The scheduler environment must not import scenedetect; the Narrato env runs the
real detector (scripts/verify-scenedetect.py). These tests cover the scheduler's
consumption of detection output and the worker operation contract.
"""
import hashlib, json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_scheduler_never_imports_scenedetect():
    for p in (ROOT / 'scheduler').glob('*.py'):
        assert 'scenedetect' not in p.read_text(encoding='utf-8'), str(p)


def test_scene_detect_operation_is_registered_for_narrato_only():
    from workers.common import OPS, validate_operation
    assert 'scene.detect' in OPS['narrato']
    assert 'scene.detect' not in OPS['mpt']
    envelope = {'protocol': 'postprod-worker/v1', 'op': 'scene.detect',
                'operation_id': 'x' * 8, 'node_key': 'k', 'attempt_id': 'a', 'lease_token': 1,
                'deadline_utc': '2026-10-11T00:00:00+00:00', 'input_refs': [], 'params': {},
                'provider_route_id': 'configured', 'paid_intent_id': None}
    validate_operation(envelope, 'narrato')
    with pytest.raises(ValueError):
        validate_operation(envelope, 'mpt')


def test_detection_params_carry_no_script_or_order_data():
    from workers.narrato.analysis import scene_params_are_script_free
    params = {'threshold': 12, 'min_scene_len_frames': 12}
    assert scene_params_are_script_free(params)
    assert not scene_params_are_script_free({'threshold': 12, 'brief': '今天介绍火龙果'})
    assert not scene_params_are_script_free({'threshold': 12, 'asset_order': ['a', 'b']})


def test_short_scene_windows_merge_or_mark_not_usable():
    from scheduler.shot_library import build_shot_library
    sha = hashlib.sha256(b'scene-src').hexdigest()
    asset = {'asset_id': 'scene-src', 'sha256': sha, 'role': 'product', 'mode': 'broll',
             'analysis': {'mode': 'detect_scenes'}}
    ref = {'asset_id': 'scene-src', 'source_sha256': sha, 'usable': True, 'duration_ticks': 480000,
           'coverage': [{'in_tick': 0, 'out_tick': 480000, 'status': 'success',
                         'observations': [{'text': '画面', 'frame_id': 'f', 'source_tick': 240000}]}],
           'detect': {'cuts': [0, 9600, 240000, 480000], 'config_hash': 'det-v1', 'detector': 'content-v1'},
           'product_identity_verified': False,
           'analysis_evidence_sha256': '0' * 64}
    lib = build_shot_library([asset], [ref], {'min_candidate_ticks': 48000})
    # the 0.2s blip window (0..9600) is below the minimum and never becomes selectable
    for c in lib['candidates']:
        if c['out_tick'] - c['in_tick'] < 48000:
            assert c['usable'] is False and c['unusable_reason']
