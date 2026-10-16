"""Offline acceptance of the v2 orchestration (plan §6, A1–A5).

Drives the real scheduler queue, service endpoints, gates, caches and FFmpeg
render with worker transports mocked (labeled in the report). The canonical
runner is scripts/acceptance-orchestration-v2.py --offline.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/acceptance-orchestration-v2.py'


def load_script():
    spec = importlib.util.spec_from_file_location('acceptance_orchestration_v2', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_acceptance_full_flow():
    module = load_script()
    report = module.run_offline(keep=False)
    try:
        assert report['verdict'] == 'OFFLINE_PASS', json.dumps(report.get('error', ''), ensure_ascii=False)
        for key in ['A1', 'A2', 'A3', 'A4', 'A5']:
            assert report[key]['verdict'] == 'PASS', (key, report[key])
        assert report['A1']['tts_calls'] == 0                      # uploaded narration: zero synthesis
        assert report['A5']['tts_delta'] == 0 and report['A5']['analyze_delta'] == 0
        assert report['A5']['replacement_tts_delta'] == 0
        assert Path(report['output']['path']).is_file()
    finally:
        # keep the report for inspection even when assertions fail later
        (ROOT / 'state/deployment/orchestration-v2').mkdir(parents=True, exist_ok=True)
        (ROOT / 'state/deployment/orchestration-v2/acceptance-offline.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def test_mocks_are_labeled_not_silent():
    module = load_script()
    report = module.run_offline(keep=False)
    assert report['mocks']['worker_transport'].startswith('mocked')
    assert 'ffmpeg render' in ' '.join(report['mocks']['real'])
