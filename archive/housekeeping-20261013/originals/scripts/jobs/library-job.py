"""Build (and optionally submit) a beat-mode edit-job/v2 from a material library product.

Every usable clip of the product becomes one asset whose provenance is a material
card: the library's own reviewed clip entry plus the optional clean window from a
windows file. The scheduler must already be running on 127.0.0.1:8760.

  python scripts/library-job.py --account laozhou --product tianyiqing \
      --windows config/accounts/laozhou/material_windows.json \
      --document config/accounts/laozhou/products/tianyiqing.md \
      --job-id tyq-demo --brief "..." --target 25 --max 28 [--submit]
"""
import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scheduler.security import operator_token  # noqa: E402
from workers.common import atomic_json  # noqa: E402

BASE = 'http://127.0.0.1:8760'
DEFAULT_LIBRARY = Path('F:/codex/laox_director_lab/material-library')
TICKS = 48000


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def api(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 headers=dict({'Authorization': 'Bearer ' + operator_token(),
                                               'Content-Type': 'application/json'}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit('%s %s -> %s %s' % (method, path, e.code, e.read().decode('utf-8', 'replace')))


def material_cards(library, product, windows):
    index = json.loads((library / 'index.json').read_text(encoding='utf-8'))
    entry = next((p for p in index['products'] if p['product_id'] == product), None)
    if entry is None:
        raise SystemExit('PRODUCT_NOT_IN_LIBRARY:' + product)
    manifest_path = library / entry['manifest']
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    usable = set(manifest['usable_statuses'])
    out = []
    for clip in manifest['clips']:
        if clip['status'] not in usable:
            continue
        video = manifest_path.parent / (clip.get('file') or 'videos/%s.mp4' % clip['clip_id'])
        if not video.is_file() or sha256(video) != clip['sha256']:
            raise SystemExit('CLIP_FILE_MISSING_OR_CHANGED:' + clip['clip_id'])
        card = dict(clip, card_source={'manifest': str(manifest_path), 'manifest_sha256': sha256(manifest_path)})
        window = windows.get(clip['clip_id'])
        if window:
            card['usable_window_sec'] = window['usable_window_sec']
            card['usable_window_basis'] = window['basis']
        path = ROOT / 'artifacts/material-cards' / product / (clip['clip_id'] + '.json')
        if not path.is_file() or json.loads(path.read_text(encoding='utf-8')) != card:
            atomic_json(path, card)
        out.append((clip, video, path))
    return out


def build_job(args):
    windows = json.loads(Path(args.windows).read_text(encoding='utf-8'))['windows'] if args.windows else {}
    cards = material_cards(Path(args.library), args.product, windows)
    job = api('GET', '/v2/job-template?account=' + args.account)
    job['job_id'] = args.job_id
    job['profile'] = args.profile or job['profile']
    job['intent'].update(brief=args.brief, target_duration_ticks=int(args.target * TICKS) // 1920 * 1920,
                         max_duration_ticks=int(args.max * TICKS) // 1920 * 1920, selection_mode='beat',
                         product_terms=args.terms)
    job['assets'] = [{'asset_id': clip['clip_id'], 'path': str(video), 'sha256': clip['sha256'], 'mode': 'broll',
                      'role': 'product', 'analysis': {'mode': args.analysis}, 'provenance_ref': str(card),
                      'locked': None} for clip, video, card in cards]
    if args.document:
        doc = (ROOT / args.document).resolve() if not Path(args.document).is_absolute() else Path(args.document)
        job['product_documents'] = [{'document_id': 'doc-product', 'path': str(doc), 'sha256': sha256(doc),
                                     'media_type': 'text/markdown' if doc.suffix == '.md' else 'text/plain'}]
    if args.script_file:
        text = Path(args.script_file).read_text(encoding='utf-8').strip()
        job['script_input'] = {'mode': 'pasted', 'script_text': text, 'generation_note': None,
                               'script_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
    else:
        job['script_input'] = {'mode': 'generated', 'script_text': None, 'script_sha256': None,
                               'generation_note': args.brief[:500]}
    job['clef_selector'] = dict(job.get('clef_selector') or {}, mode='off')
    job['cost']['paid_enabled'] = True
    return job


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--product', required=True)
    ap.add_argument('--library', default=str(DEFAULT_LIBRARY))
    ap.add_argument('--windows')
    ap.add_argument('--document')
    ap.add_argument('--profile')
    ap.add_argument('--terms', nargs='*', default=[])
    ap.add_argument('--job-id', required=True)
    ap.add_argument('--brief', required=True)
    ap.add_argument('--target', type=float, default=25)
    ap.add_argument('--max', type=float, default=28)
    ap.add_argument('--script-file', help='reuse an already confirmed script (pasted mode) instead of generating')
    ap.add_argument('--analysis', choices=['material_card', 'single_shot'], default='material_card',
                    help='material_card uses the reviewed card as visual evidence (no paid vision call)')
    ap.add_argument('--submit', action='store_true')
    args = ap.parse_args()
    job = build_job(args)
    if not args.submit:
        print(json.dumps(job, ensure_ascii=False, indent=1))
        return
    print(json.dumps(api('POST', '/v2/jobs', job, {'Idempotency-Key': job['job_id']}), ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
