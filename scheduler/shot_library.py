# -*- coding: utf-8 -*-
"""Batch candidate shot library (S3, r3 semantics).

Candidates are built only from successful per-file analysis (plus scene-detection
cuts where requested). A batch/cut window is only an ANALYSIS bound. All action
semantics are decided PER OBSERVATION (scheduler.shot_semantics); a merged window
keeps the per-observation results and is NEVER re-judged on its concatenated
text — the r2 fabrication where obs1's state descriptor plus obs3's hand
movement conjured an "opening" is structurally impossible here.

Action evidence comes from ATTESTED RUNS over the source's full observation
grid (see shot_semantics.action_runs): >=2 adjacent ticks each independently
attesting the same (kind, object, in-progress), optionally end-bounded by an
adjacent completion observation. A lone attesting tick is recorded as
attested_tick and never blesses a span. Midpoint geometry between observations
only bounds DISPLAY candidates; it never creates action evidence.
"""
from scheduler.contracts import validate_named, canonical_hash
from scheduler.shot_semantics import (RULE_VERSION, action_runs, clip_runs, display_kind,
                                      parse_observation, speech_preservation_required)

USE_RULE_VERSION = RULE_VERSION            # 'semantic-actions-v1'
DEFAULT_MIN_CANDIDATE_TICKS = 24000
OBSTRUCTION_MARKERS = ['严重遮挡', '完全遮挡', '大部分被遮挡', '严重模糊', '完全模糊']


def _candidate_display(parses):
    """Display kind of a window: majority of its own observations' display
    kinds (per-observation, never concatenated)."""
    order = [p['display'] for p in parses]
    counts = {}
    for d in order:
        counts[d] = counts.get(d, 0) + 1
    best = sorted((d for d in counts if d != 'other'), key=lambda d: (-counts[d], order.index(d)))
    return best[0] if best else 'other'


def _observation_windows(batch_in, batch_out, observations):
    """Split a batch window at observation boundaries (midpoint geometry — an
    ANALYSIS bound only). Adjacent spans whose own observations yield the same
    display kind stay merged. Action evidence is attached per-observation and
    is unaffected by this geometry."""
    obs = sorted((o for o in observations if o.get('text')), key=lambda o: o.get('source_tick', 0))
    if len(obs) <= 1:
        return [(batch_in, batch_out, obs)]
    ticks = [o['source_tick'] for o in obs]
    bounds = [batch_in]
    for i in range(1, len(ticks)):
        bounds.append((ticks[i - 1] + ticks[i]) // 2)
    bounds.append(batch_out)
    spans = [(bounds[i], bounds[i + 1], [obs[i]]) for i in range(len(obs))]
    merged = []
    for start, end, os_ in spans:
        kind = _candidate_display([parse_observation(o['text']) for o in os_])
        if merged and _candidate_display([parse_observation(o['text']) for o in merged[-1][2]]) == kind:
            merged[-1] = (merged[-1][0], end, merged[-1][2] + os_)
        else:
            merged.append((start, end, os_))
    return merged


def _candidate(asset, ref, in_tick, out_tick, observations, runs):
    duration = ref.get('duration_ticks', 0)
    texts = [o.get('text', '') for o in observations]
    parses = [parse_observation(t) for t in texts]
    config_hash = canonical_hash({'use_rule_version': USE_RULE_VERSION})
    candidate_id = 'cand-' + canonical_hash({'src': ref['source_sha256'], 'in': in_tick, 'out': out_tick,
                                             'cfg': config_hash})[:32]
    joined = ' '.join(texts)
    window_valid = 0 <= in_tick < out_tick <= duration + 1920
    obstructed = any(m in joined for m in OBSTRUCTION_MARKERS)
    speech_locked = any(p['speech'] for p in parses)
    usable, reason = True, None
    if not window_valid:
        usable, reason = False, 'WINDOW_OUT_OF_RANGE'
    elif ref.get('usable') is False:
        usable, reason = False, 'ANALYSIS_UNUSABLE'
    elif not observations:
        usable, reason = False, 'NO_OBSERVATIONS'
    elif obstructed:
        usable, reason = False, 'SEVERELY_OBSTRUCTED'
    elif speech_locked:
        # original speech with visible mouth must be routed to a fixed role by the
        # operator; it never silently becomes silent free-pool B-roll
        usable, reason = False, 'SPEECH_PRESERVATION_REQUIRED'
    min_ticks = ref.get('min_candidate_ticks')
    if min_ticks is not None and (out_tick - in_tick) < min_ticks and usable:
        usable, reason = False, 'WINDOW_BELOW_MIN_TICKS'
    return {
        'candidate_id': candidate_id,
        'source_asset_id': asset['asset_id'],
        'source_sha256': ref['source_sha256'],
        'source_path': ref.get('path'),
        'in_tick': in_tick, 'out_tick': out_tick,
        'use': _candidate_display(parses),
        'supported_actions': clip_runs(runs, in_tick, out_tick),
        'action_text': texts[0] if texts else None,
        'product_description': max(texts, key=len) if texts else None,
        'product_identity_verified': bool(ref.get('product_identity_verified', False)),
        'occlusion': 'severe' if obstructed else 'unknown', 'clarity': 'unknown',
        'speech_locked': speech_locked,
        'evidence': {'analysis_evidence_sha256': ref.get('analysis_evidence_sha256', '0' * 64),
                     'frame_count': len({o.get('frame_id') for o in observations}),
                     'observation_count': len(observations),
                     'observation_texts': texts[:8],
                     'observation_ticks': [o.get('source_tick') for o in observations[:16]],
                     'sampling_interval_ticks': ref.get('sampling_interval_ticks'),
                     'per_observation': [{'tick': o.get('source_tick'),
                                          'actions': p['actions'], 'display': p['display']}
                                         for o, p in zip(observations, parses)][:16]},
        'fixed_role': None,
        'usable': usable, 'unusable_reason': reason,
    }


def _detection_windows(detect, duration):
    cuts = sorted(set(detect.get('cuts') or []))
    cuts = [c for c in cuts if 0 <= c <= duration + 1920]
    if not cuts or cuts[0] != 0:
        cuts = [0] + cuts
    if cuts[-1] < duration:
        cuts = cuts + [duration]
    return list(zip(cuts, cuts[1:]))


def build_shot_library(assets, analysis_refs, config=None, material_candidates=None):
    """material_candidates (optional): native-shape candidates imported from a reviewed
    material manifest via scheduler.material_manifest (explicit material branch — never
    synthesized Narrato coverage). Analysis refs keep their read-only Narrato meaning
    and are untouched."""
    config = dict(config or {})
    min_ticks = config.get('min_candidate_ticks', DEFAULT_MIN_CANDIDATE_TICKS)
    refs = {r['asset_id']: r for r in analysis_refs}
    product_assets = {a['asset_id'] for a in assets if a.get('role') == 'product'}
    candidates, failures = [], []
    sources, action_runs_by_sha, observation_grid = {}, {}, {}
    for asset in assets:
        if asset.get('role') != 'product':
            continue  # fixed/locked assets never enter the free pool
        ref = refs.get(asset['asset_id'])
        if not ref or ref.get('source_sha256') != asset['sha256']:
            failures.append({'asset_id': asset['asset_id'], 'source_sha256': asset['sha256'],
                             'reason': 'ANALYSIS_MISSING_FOR_ASSET'})
            continue
        coverage = [c for c in ref.get('coverage', []) if c.get('status') == 'success']
        if ref.get('usable') is False or not coverage:
            failures.append({'asset_id': asset['asset_id'], 'source_sha256': asset['sha256'],
                             'reason': 'ANALYSIS_UNUSABLE'})
            continue
        # the asset's FULL observation grid drives action runs (neighbors outside
        # any single candidate window still bound runs and completion ends)
        grid = sorted((o for c in coverage for o in c.get('observations', []) if o.get('text')),
                      key=lambda o: o.get('source_tick', 0))
        grid_ticks = [o['source_tick'] for o in grid]
        parses = [parse_observation(o['text']) for o in grid]
        runs = action_runs(grid_ticks, parses)
        from scheduler.shot_semantics import sampling_interval
        ref = dict(ref, min_candidate_ticks=min_ticks,
                   sampling_interval_ticks=sampling_interval(grid_ticks))
        sources[asset['asset_id']] = {'sha256': asset['sha256'],
                                      'duration_ticks': ref.get('duration_ticks', 0),
                                      'product_identity_verified': bool(ref.get('product_identity_verified', False))}
        action_runs_by_sha[asset['sha256']] = runs
        observation_grid[asset['sha256']] = [{'tick': o['source_tick'], 'text': o['text'][:200]}
                                             for o in grid[:64]]
        mode = (asset.get('analysis') or {}).get('mode', 'single_shot')
        if mode == 'detect_scenes':
            detect = ref.get('detect')
            if not detect or not detect.get('cuts'):
                failures.append({'asset_id': asset['asset_id'], 'source_sha256': asset['sha256'],
                                 'reason': 'SCENE_DETECTION_MISSING'})
                continue
            for in_tick, out_tick in _detection_windows(detect, ref.get('duration_ticks', 0)):
                observations = [o for c in coverage for o in c.get('observations', [])
                                if in_tick <= o.get('source_tick', -1) < out_tick]
                # a cut window with mixed observations splits at observation
                # boundaries too — display geometry only; action evidence rides
                # on the per-observation runs attached to each candidate
                for start, end, obs in _observation_windows(in_tick, out_tick, observations):
                    candidates.append(_candidate(asset, ref, start, end, obs, runs))
        else:
            for batch in coverage:
                observations = batch.get('observations', [])
                in_tick, out_tick = batch.get('in_tick', 0), batch.get('out_tick', batch.get('in_tick', 0) + 1)
                for start, end, obs in _observation_windows(in_tick, out_tick, observations):
                    candidates.append(_candidate(asset, ref, start, end, obs, runs))
    candidates.sort(key=lambda c: (c['source_sha256'], c['in_tick'], c['out_tick']))
    for extra in material_candidates or []:
        if extra.get('source_asset_id') not in product_assets:
            raise ValueError('MATERIAL_CANDIDATE_ASSET_NOT_PRODUCT:' + str(extra.get('candidate_id')))
        if extra.get('fixed_role') is not None or extra.get('speech_locked'):
            raise ValueError('MATERIAL_CANDIDATE_FIXED_OR_LOCKED:' + str(extra.get('candidate_id')))
        if not str(extra.get('candidate_id', '')).startswith('mcat-'):
            raise ValueError('MATERIAL_CANDIDATE_ID_PREFIX_MISSING:' + str(extra.get('candidate_id')))
        if extra.get('evidence', {}).get('frame_count') != 0:
            raise ValueError('MATERIAL_CANDIDATE_MUST_NOT_CLAIM_FRAME_DETECTION:' + str(extra.get('candidate_id')))
        candidates.append(extra)
    candidates.sort(key=lambda c: (c['source_sha256'], c['in_tick'], c['out_tick']))
    library = {
        'schema_version': 'shot-library/v1',
        'job_id': config.get('job_id', 'job'), 'revision': config.get('revision', 1),
        'library_sha256': canonical_hash({'candidates': [{'id': c['candidate_id'], 'in': c['in_tick'], 'out': c['out_tick']}
                                                          for c in candidates],
                                          'failures': failures}),
        'config': {'use_rule_version': USE_RULE_VERSION, 'min_candidate_ticks': min_ticks,
                   'detection_config_hash': next((r.get('detect', {}).get('config_hash')
                                                  for r in analysis_refs if r.get('detect')), None)},
        'sources': sources,
        'action_runs': action_runs_by_sha,
        'observation_grid': observation_grid,
        'candidates': candidates,
        'failures': failures,
    }
    return validate_named(library, 'shot-library-v1')
