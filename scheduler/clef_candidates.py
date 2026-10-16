"""Immutable, complete candidate snapshots for the Clef selector."""
import hashlib
import json
from scheduler.shot_semantics import speech_preservation_required

SCHEMA_VERSION = 'clef-candidates/v1'
TIMESCALE = 48000


def _stable_id(source_sha256, in_tick, out_tick, fallback_asset_id=None):
    fallback = fallback_asset_id if not source_sha256 else None
    raw = json.dumps([source_sha256, in_tick, out_tick, fallback], separators=(',', ':')).encode()
    return 'clef-' + hashlib.sha256(raw).hexdigest()[:32]


def _global_asset_id(source_sha256):
    return 'asset-' + source_sha256 if isinstance(source_sha256, str) and len(source_sha256) == 64 and all(c in '0123456789abcdef' for c in source_sha256) else None


def _duration(start, end):
    if isinstance(start, int) and isinstance(end, int):
        return round((end - start) / TIMESCALE, 6)
    return None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(v) for v in value), key=str)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _coverage_rows(assets, analysis_refs):
    amap = {a.get('asset_id'): a for a in assets}
    rows = []
    for ref in analysis_refs or []:
        asset = amap.get(ref.get('asset_id'), {})
        for index, coverage in enumerate(ref.get('coverage') or []):
            rows.append((asset, ref, coverage, index))
    return rows


def build_candidate_snapshot(assets, analysis_refs, library, *, material_report=None):
    """Normalize all known analysis/library material, including failures and locks.

    This is a reporting snapshot, not a re-analysis or an eligibility filter.
    """
    assets = list(assets or [])
    analysis_refs = list(analysis_refs or [])
    library = library or {}
    refs_by_asset = {r.get('asset_id'): r for r in analysis_refs}
    assets_by_id = {a.get('asset_id'): a for a in assets}
    manifest_reasons = {}
    material_report_safe = _json_safe(material_report)
    bound_shas = set()
    if isinstance(material_report, dict):
        bound_shas = {str(v) for v in (material_report.get('bound_shas') or []) if isinstance(v, str)}
        report_body = material_report.get('report') if isinstance(material_report.get('report'), dict) else material_report
        entries = list(report_body.get('entries') or report_body.get('results') or [])
        entries.extend(report_body.get('refused') or [])
        if isinstance(material_report.get('failures'), list):
            entries.extend(material_report['failures'])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            aid = entry.get('asset_id') or entry.get('source_asset_id')
            product_id = entry.get('product_id')
            refused = (entry.get('status') in ('refused', 'rejected', 'blocked') or
                       entry.get('accepted') is False or entry in (report_body.get('refused') or []) or
                       entry in (material_report.get('failures') or []))
            if refused:
                reason = str(entry.get('reason') or entry.get('status') or 'MANIFEST_REFUSED')
                if aid:
                    manifest_reasons.setdefault(('asset', aid), []).append(reason)
                if product_id:
                    manifest_reasons.setdefault(('product', product_id), []).append(reason)

    rows = []
    attached_native = set()

    def emit(asset, ref, coverage, native, fallback_interval=None):
        asset = asset or {}
        ref = ref or {}
        coverage = coverage or {}
        native = native if isinstance(native, dict) else None
        start = (native or {}).get('in_tick', coverage.get('in_tick'))
        end = (native or {}).get('out_tick', coverage.get('out_tick'))
        if start is None and fallback_interval:
            start = fallback_interval[0]
        if end is None and fallback_interval:
            end = fallback_interval[1]
        sha = (native or {}).get('source_sha256') or ref.get('source_sha256') or asset.get('sha256')
        aid = (native or {}).get('source_asset_id') or asset.get('asset_id') or ref.get('asset_id')
        reasons = []
        role = asset.get('role')
        if role not in ('product', 'broll', 'free_broll'):
            reasons.append('ASSET_ROLE_UNKNOWN' if role is None else 'FIXED_OR_LOCKED_ASSET')
        if native and native.get('usable') is False:
            native_reason = str(native.get('unusable_reason') or 'NATIVE_MARKED_UNUSABLE')
            # The historical minimum-window filter no longer controls pool membership.
            if native_reason != 'WINDOW_BELOW_MIN_TICKS':
                reasons.append(native_reason)
        observations = coverage.get('observations') or []
        speech_texts = [o.get('text', '') if isinstance(o, dict) else str(o) for o in observations]
        speech_texts.extend([coverage.get('text', ''), coverage.get('description', '')])
        speech_observed = any(speech_preservation_required(text) for text in speech_texts)
        locked_role = role in ('fixed_opening', 'fixed_closing', 'locked_body', 'fixed', 'spoken_locked')
        explicit_audio = (native or {}).get('audio_policy') or asset.get('audio_policy')
        audio_policy = explicit_audio or ('must_preserve_original' if locked_role or speech_observed or (native or {}).get('speech_locked') else 'unknown')
        expected_sha = ref.get('source_sha256') or asset.get('sha256')
        if ref.get('source_sha256') and asset.get('sha256') and ref.get('source_sha256') != asset.get('sha256'):
            reasons.append('SOURCE_HASH_MISMATCH')
        if native and expected_sha and native.get('source_sha256') and native.get('source_sha256') != expected_sha:
            reasons.append('NATIVE_SOURCE_HASH_MISMATCH')
        if sha in bound_shas and not str((native or {}).get('candidate_id') or '').startswith('mcat-'):
            reasons.append('MANIFEST_BOUND_RAW_ANALYSIS_BLOCKED')
        if audio_policy == 'must_preserve_original' or speech_observed or (native or {}).get('speech_locked'):
            reasons.append('ORIGINAL_AUDIO_MUST_BE_PRESERVED')
        reasons.extend(manifest_reasons.get(('asset', aid), []))
        product_id = asset.get('product_id') or (native or {}).get('product_id')
        reasons.extend(manifest_reasons.get(('product', product_id), []))
        status = coverage.get('status') or ('success' if native else 'unknown')
        if status != 'success':
            reasons.append('ANALYSIS_' + str(status).upper())
        native_key = (aid, sha, start, end, (native or {}).get('candidate_id'))
        if native:
            attached_native.add(native_key)
        observation_details = [{'tick': o.get('source_tick'), 'text': o.get('text')}
                               for o in observations if isinstance(o, dict) and isinstance(o.get('text'), str)]
        observation_text = max((o['text'] for o in observation_details), key=len, default=None)
        description = ((native or {}).get('product_description') or (native or {}).get('action_text')
                       or coverage.get('description') or coverage.get('text') or observation_text)
        duration_ticks = ref.get('duration_ticks')
        rows.append({
            'candidate_id': _stable_id(sha, start, end, aid),
            'global_asset_id': _global_asset_id(sha),
            'asset_id': aid, 'source_asset_id': aid,
            'product_id': product_id,
            'source_path': (native or {}).get('source_path') or asset.get('path') or ref.get('path'),
            'file_hash': sha, 'source_sha256': sha,
            'duration_sec': _duration(start, end),
            'source_duration_sec': _duration(0, duration_ticks) if isinstance(duration_ticks, int) else None,
            'start_sec': _duration(0, start) if isinstance(start, int) else None,
            'end_sec': _duration(0, end) if isinstance(end, int) else None,
            'in_tick': start, 'out_tick': end,
            'description': description, 'scene': coverage.get('scene') or (native or {}).get('scene'),
            'actors': coverage.get('actors') or (native or {}).get('actors') or [],
            'actions': coverage.get('actions') or (native or {}).get('supported_actions') or [],
            'observations': observation_details,
            'shot_type': coverage.get('shot_type') or (native or {}).get('use'),
            'tags': coverage.get('tags') or (native or {}).get('tags') or [],
            'quality_status': status,
            'audio_policy': audio_policy,
            'selection_eligible': not reasons,
            'gate_reasons': list(dict.fromkeys(reasons)),
            'native': native,
            'analysis_coverage': coverage,
        })

    # Preserve every supplied Narrato coverage interval, regardless of status.
    for asset, ref, coverage, _index in _coverage_rows(assets, analysis_refs):
        start, end = coverage.get('in_tick'), coverage.get('out_tick')
        matches = []
        for native in library.get('candidates') or []:
            if native.get('source_asset_id') != asset.get('asset_id'):
                continue
            if str(native.get('candidate_id') or '').startswith('mcat-'):
                continue
            if native.get('source_sha256') != (ref.get('source_sha256') or asset.get('sha256')):
                continue
            ns, ne = native.get('in_tick'), native.get('out_tick')
            if isinstance(ns, int) and isinstance(ne, int) and isinstance(start, int) and isinstance(end, int) and ns < end and ne > start:
                matches.append(native)
        if matches:
            for native in matches:
                emit(asset, ref, coverage, native)
        else:
            emit(asset, ref, coverage, None)

    # Preserve library candidates even when they have no matching coverage row.
    for native in library.get('candidates') or []:
        aid = native.get('source_asset_id')
        asset = assets_by_id.get(aid, {})
        ref = refs_by_asset.get(aid, {})
        key = (aid, native.get('source_sha256'), native.get('in_tick'), native.get('out_tick'), native.get('candidate_id'))
        if key in attached_native:
            continue
        emit(asset, ref, {}, native)

    # Missing/failed analysis records stay visible even if there is no interval.
    for failure in library.get('failures') or []:
        aid = failure.get('asset_id')
        if not any(r.get('asset_id') == aid and r.get('quality_status') != 'success' for r in rows):
            asset = assets_by_id.get(aid, {})
            failure_sha = failure.get('source_sha256') or asset.get('sha256')
            failure_role = asset.get('role')
            failure_locked = failure_role in ('fixed_opening', 'fixed_closing', 'locked_body', 'fixed', 'spoken_locked')
            failure_reasons = [str(failure.get('reason') or 'ANALYSIS_FAILED')]
            if failure_locked:
                failure_reasons.append('FIXED_OR_LOCKED_ASSET')
            if failure_sha in bound_shas:
                failure_reasons.append('MANIFEST_BOUND_RAW_ANALYSIS_BLOCKED')
            rows.append({
                'candidate_id': _stable_id(failure_sha, None, None, aid),
                'global_asset_id': _global_asset_id(failure_sha),
                'asset_id': aid, 'source_asset_id': aid, 'product_id': asset.get('product_id'),
                'source_path': asset.get('path'), 'file_hash': failure_sha,
                'source_sha256': failure_sha,
                'duration_sec': None, 'source_duration_sec': None, 'start_sec': None, 'end_sec': None,
                'in_tick': None, 'out_tick': None, 'description': None, 'scene': None,
                'actors': [], 'actions': [], 'shot_type': None, 'tags': [],
                'quality_status': 'failed', 'audio_policy': asset.get('audio_policy') or ('must_preserve_original' if failure_locked else 'unknown'),
                'selection_eligible': False, 'gate_reasons': failure_reasons,
                'native': None, 'analysis_coverage': None,
            })

    represented = {row.get('asset_id') for row in rows}
    for asset in assets:
        aid = asset.get('asset_id')
        if aid in represented:
            continue
        sha = asset.get('sha256')
        locked = asset.get('role') in ('fixed_opening', 'fixed_closing', 'locked_body', 'fixed', 'spoken_locked')
        reasons = ['ANALYSIS_NOT_AVAILABLE']
        if locked:
            reasons.append('FIXED_OR_LOCKED_ASSET')
        if sha in bound_shas:
            reasons.append('MANIFEST_BOUND_RAW_ANALYSIS_BLOCKED')
        rows.append({'candidate_id': _stable_id(sha, None, None, aid),
                     'global_asset_id': _global_asset_id(sha),
                     'asset_id': aid, 'source_asset_id': aid, 'product_id': asset.get('product_id'),
                     'source_path': asset.get('path'), 'file_hash': sha, 'source_sha256': sha,
                     'duration_sec': None, 'source_duration_sec': None, 'start_sec': None, 'end_sec': None,
                     'in_tick': None, 'out_tick': None, 'description': None, 'scene': None,
                     'actors': [], 'actions': [], 'observations': [], 'shot_type': None, 'tags': [],
                     'quality_status': 'unknown',
                     'audio_policy': asset.get('audio_policy') or ('must_preserve_original' if locked else 'unknown'),
                     'selection_eligible': False, 'gate_reasons': reasons, 'native': None, 'analysis_coverage': None})

    rows.sort(key=lambda c: (str(c.get('source_sha256') or ''),
                             -1 if c.get('in_tick') is None else c['in_tick'],
                             -1 if c.get('out_tick') is None else c['out_tick'], c['candidate_id']))
    return {
        'schema_version': SCHEMA_VERSION,
        'candidates': rows,
        'analysis_failures': list(library.get('failures') or []),
        'material_report': material_report_safe,
        'library_sha256': library.get('library_sha256'),
    }
