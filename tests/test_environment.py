import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_doctor_exists_and_emits_versioned_json():
    script = ROOT / 'scripts/doctor.ps1'
    assert script.is_file(), 'P0 environment doctor is not implemented'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script), '-Json'], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report['schema_version'] == 'postprod-doctor/v1'
    assert report['environment_ready'], report
    assert report['upstreams']['mpt']['commit'] == '4204d0f39afa8ada6f3edc31604141f959fe254e'
    assert report['upstreams']['narrato']['commit'] == '9fa69e022d4add41205ee385207561df8796b3f1'
    assert report['upstreams']['mpt']['clean'] and report['upstreams']['narrato']['clean']
    assert report['environments']['mpt']['version'].startswith('3.11.')
    assert report['environments']['narrato']['version'].startswith('3.12.')
    assert report['environments']['scheduler']['version'].startswith('3.12.')

def test_protected_sources_unchanged():
    import hashlib
    baseline = json.loads((ROOT / 'state/deployment/protected-baseline.json').read_text(encoding='utf-8-sig'))
    for record in baseline:
        assert hashlib.sha256(Path(record['path']).read_bytes()).hexdigest() == record['sha256']
