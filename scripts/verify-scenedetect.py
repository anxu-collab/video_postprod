"""Verify the isolated PySceneDetect installation without production media."""
import datetime
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UV = Path('C:/Users/anxu/AppData/Roaming/Python/Python312/Scripts/uv.exe')


def run(args):
    result = subprocess.run([str(arg) for arg in args], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or result.stdout[-2000:])
    return {'exit_code': result.returncode, 'stdout': result.stdout.strip(),
            'stderr': result.stderr.strip()}


def verify():
    import cv2
    import numpy
    import scenedetect
    from scenedetect import ContentDetector, detect

    baseline = json.loads((ROOT / 'state/deployment/scenedetect-baseline.json').read_text())
    packages = {d.metadata['Name'].lower().replace('_', '-'): d.version
                for d in metadata.distributions()}
    changes = {name: [old, packages.get(name)]
               for name, old in baseline['packages'].items() if packages.get(name) != old}
    if changes:
        raise RuntimeError(f'Existing package versions changed: {changes}')
    for name, expected in baseline['protected_hashes'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Protected file changed: {name}')
    if packages.get('scenedetect-headless') != '0.7.1':
        raise RuntimeError('Unexpected PySceneDetect version')
    if any(name in packages for name in ['scenedetect', 'scenedetect-core', 'opencv-python']):
        raise RuntimeError('Conflicting package variant installed')
    dependency_check = run([UV, 'pip', 'check', '--python', sys.executable])
    cli = run([ROOT / 'envs/narrato/Scripts/scenedetect.exe', 'version'])
    with tempfile.TemporaryDirectory(prefix='scenedetect-check-') as folder:
        video = Path(folder) / 'two-scenes.mp4'
        generation = run([
            ROOT / 'tools/ffmpeg/ffmpeg.exe', '-nostdin', '-n', '-v', 'error',
            '-f', 'lavfi', '-i', 'color=c=red:s=160x120:r=25:d=1',
            '-f', 'lavfi', '-i', 'color=c=blue:s=160x120:r=25:d=1',
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
            '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', video])
        scenes = detect(str(video), ContentDetector(threshold=12, min_scene_len=1),
                        show_progress=False)
        frame_ranges = [[start.frame_num, end.frame_num] for start, end in scenes]
        if len(frame_ranges) != 2 or frame_ranges[0][0] != 0 or frame_ranges[-1][1] != 50:
            raise RuntimeError(f'Unexpected scenes: {frame_ranges}')
        error = abs(frame_ranges[1][0] - 25)
        if error > 1 or frame_ranges[0][1] != frame_ranges[1][0]:
            raise RuntimeError(f'Incorrect cut: {frame_ranges}')
    return {
        'schema_version': 'scenedetect-install/v1', 'installed_status': 'PASS',
        'verified_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'python': sys.executable, 'package': 'scenedetect-headless',
        'version': metadata.version('scenedetect-headless'), 'license': 'BSD-3-Clause',
        'module_path': scenedetect.__file__, 'opencv_version': cv2.__version__,
        'numpy_version': numpy.__version__, 'existing_packages_unchanged': True,
        'protected_files_unchanged': True,
        'added_packages': {n: v for n, v in packages.items() if n not in baseline['packages']},
        'lock_sha256': hashlib.sha256((ROOT / 'locks/scenedetect.lock').read_bytes()).hexdigest(),
        'dependency_check': dependency_check, 'cli_check': cli,
        'synthetic_video_generation': generation,
        'scene_test': {'fps': 25, 'frames': 50, 'scenes': frame_ranges,
                       'expected_cut_frame': 25, 'cut_error_frames': error},
        'production_integrated': False, 'paid_media_requests': 0,
        'executor': 'Codex after ZCode headless timeout; provider_not_configured reported',
        'rollback': 'Uninstall only added_packages; retain existing environment and evidence',
        'scope': 'Installation and synthetic hard-cut smoke test only; no production footage analysis'}


if __name__ == '__main__':
    try:
        report = verify()
    except Exception as error:
        print(json.dumps({'installed_status': 'FAIL', 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
    target = ROOT / 'state/deployment/scenedetect.json'
    temporary = target.with_suffix('.json.partial')
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(target)
    print(json.dumps(report, ensure_ascii=False))
