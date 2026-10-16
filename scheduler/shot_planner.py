# -*- coding: utf-8 -*-
"""Per-sentence shot selection, timeline layout, and compilation to timeline/v1 (S5, r3).

Selection enforces product AND per-sentence action semantics BEFORE scoring.
Every judgement is per-observation/per-clause (scheduler.shot_semantics): an
action requirement only counts when an ATTESTED RUN — >=2 adjacent observation
ticks each independently attesting the same (kind, object, in-progress) —
overlaps the candidate window, and the selected sub-window must contain at
least one attesting tick. A lone attesting tick or a midpoint span is never
enough (ACTION_EVIDENCE_INSUFFICIENT, reported honestly instead of guessed).
Deterministic hash sorting breaks ties; input order and model suggestions never
decide the narrative. Source-window reuse is forbidden by default; an operator
replacement may allow overlap only with a recorded reason. Operator-confirmed
action windows (evidence insufficient) are bound to the sentence's own
(kind, object) and source range — they never unlock a different action.
"""
from scheduler.contracts import validate_named, canonical_hash
from scheduler.script_plan import ACTION_USES, USE_PRIORITY, USE_KEYWORDS
from scheduler.shot_semantics import RULE_VERSION, _ACTION_TABLE, parse_action_intent

GRID = 1920
# words that carry no product/content meaning; their bigrams cannot prove relatedness
_GENERIC_GRAMS = {'今天', '看看', '这款', '我们', '大家', '一下', '这个', '还是', '一个', '非常', '特别'}


def _action_grams():
    """Action verbs, action object words, packaging words and generic connectives
    never prove product correspondence. Detail descriptors (果肉/切面/特写…) are
    CONTENT and stay usable."""
    grams = set(_GENERIC_GRAMS)
    for _kind, verbs, objects in _ACTION_TABLE:
        for w in list(verbs) + list(objects):
            grams.add(w)
            for i in range(len(w) - 1):
                grams.add(w[i:i + 2])
    for w in USE_KEYWORDS.get('packaging', []):
        grams.add(w)
        for i in range(len(w) - 1):
            grams.add(w[i:i + 2])
    return grams


def _packaging_grams():
    grams = set()
    for w in USE_KEYWORDS.get('packaging', []):
        grams.add(w)
        for i in range(len(w) - 1):
            grams.add(w[i:i + 2])
    return grams


_PACKAGING_GRAMS = None


_ACTION_GRAMS = None


def _grid(value):
    return int(round(value / GRID)) * GRID


def _ceil(value):
    return -((-int(value)) // GRID) * GRID


def _bigrams(text):
    cleaned = ''.join(ch for ch in text if '\u4e00' <= ch <= '\u9fff')
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _candidate_text(candidate):
    return ' '.join(candidate.get('evidence', {}).get('observation_texts') or []) \
        or (candidate.get('product_description') or '')


def sentence_product_terms(sentence, product_terms):
    """Product evidence a candidate must show for THIS sentence.

    Operator product_terms are a JOB-LEVEL lock: every sentence inherits them,
    including sentences that omit the product name ("打开包装。" still means THIS
    product's package). Sentence-specific content bigrams only apply when the
    operator gave no product terms at all; action/packaging/generic vocabulary
    never proves product correspondence."""
    text = sentence['text']
    locked = [t for t in (product_terms or []) if len(t) >= 2]
    mentioned = [t for t in locked if t in text]
    if mentioned:
        return mentioned
    if locked:
        return list(locked)
    global _ACTION_GRAMS, _PACKAGING_GRAMS
    if _ACTION_GRAMS is None:
        _ACTION_GRAMS = _action_grams(); _PACKAGING_GRAMS = _packaging_grams()
    base = _bigrams(text) - _ACTION_GRAMS
    content = base - _PACKAGING_GRAMS
    return sorted(content if content else base)


def _confirmed_intervals_for(confirmed, source_sha, kind, obj, need_ticks=None):
    """Operator-confirmed interval evidence for (source, kind, object) — the ONLY
    auto-selectable action basis. Inferred runs never qualify."""
    out = []
    for c in confirmed or []:
        if c.get('source_sha256') != source_sha or c.get('action_kind') != kind \
                or c.get('action_object') != obj:
            continue
        if need_ticks is not None and c['out_tick'] - c['in_tick'] < need_ticks:
            continue
        out.append(c)
    return out


def _first_confirmed_window(used, source_sha, interval, need_ticks):
    """Earliest free sub-window inside a CONFIRMED interval. The interval itself
    (hash + boundaries + reviewer + basis) is the time-segment evidence."""
    base_in, base_out = interval['in_tick'], interval['out_tick']
    if base_out - base_in < need_ticks:
        return None
    intervals = sorted(w for w in used.get(source_sha, [])
                       if w[1] > base_in and w[0] < base_out)
    start = base_in
    for a, b in intervals:
        if a - start >= need_ticks:
            break
        start = max(start, b)
    if base_out - start >= need_ticks:
        return (start, start + need_ticks)
    return None


def candidate_eligibility(candidate, sentence, product_terms, used=None, need_ticks=None,
                          confirmed=None):
    """Shared hard constraint for auto planning, the alternatives endpoint and
    operator replacements. Returns (ok, reason).

    An in-progress action requirement passes ONLY on operator-confirmed interval
    evidence for the same (kind, object) on the candidate's source (M1). Inferred
    runs / lone ticks are honest insufficient evidence, never a pass."""
    if not candidate.get('usable'):
        return False, 'CANDIDATE_UNUSABLE'
    if candidate.get('fixed_role') is not None or candidate.get('speech_locked'):
        return False, 'FIXED_SOURCE_IN_FREE_POOL'
    if candidate.get('occlusion') == 'severe':
        return False, 'SEVERELY_OBSTRUCTED'
    sup = candidate.get('supported_actions')
    if sup is None and not str(candidate.get('candidate_id', '')).startswith('mcat-'):
        # r2-era artifacts carry no per-observation semantics; they must not
        # silently pass the r3 gate (M5)
        return False, 'LEGACY_LIBRARY_RULE_VERSION'
    if need_ticks is not None and (candidate['out_tick'] - candidate['in_tick']) < need_ticks:
        return False, 'WINDOW_TOO_SHORT'
    if used is not None and not _window_free(used, candidate['source_sha256'],
                                             candidate['in_tick'], candidate['in_tick'] + need_ticks):
        return False, 'SOURCE_WINDOW_ALREADY_USED'
    text = _candidate_text(candidate)
    terms = sentence_product_terms(sentence, product_terms)
    if terms and not candidate.get('product_identity_verified') and not any(t in text for t in terms):
        return False, 'PRODUCT_NOT_SHOWN_IN_CANDIDATE'
    intent = parse_action_intent(sentence['text'])
    if intent['uncertainty']:
        return False, 'SENTENCE_SEMANTICS_UNRESOLVED'
    need = sentence['visual']['required'][0] if sentence['visual']['required'] else None
    preferred = sentence['visual']['preferred']
    if need in ACTION_USES and intent['state'] == 'in_progress':
        intervals = _confirmed_intervals_for(confirmed, candidate['source_sha256'],
                                             intent['action_kind'], intent['action_object'],
                                             need_ticks)
        if not intervals:
            same_action = [a for a in (sup or [])
                           if a['kind'] == intent['action_kind'] and a['object'] == intent['action_object']]
            if same_action:
                return False, 'ACTION_EVIDENCE_INSUFFICIENT'   # inferred only: awaiting confirmation
            same_kind = [a for a in (sup or []) if a['kind'] == intent['action_kind']]
            return False, ('ACTION_OBJECT_NOT_MATCHED' if same_kind else 'ACTION_NOT_OBSERVED_IN_WINDOW')
    else:
        if need:
            if candidate['use'] != need:
                return False, 'REQUIRED_USE_NOT_MATCHED'
        else:
            if candidate['use'] not in preferred:
                return False, 'USE_NOT_PREFERRED'
    if candidate['use'] == 'other':
        # generic 'other' B-roll may only serve a sentence whose own product it shows
        if not terms or not (_bigrams(text) & _bigrams(''.join(terms))):
            return False, 'UNRELATED_OTHER_FOOTAGE'
    return True, None


def _score(candidate, sentence, product_terms):
    terms = sentence_product_terms(sentence, product_terms)
    text = _candidate_text(candidate)
    term_hits = sum(1 for t in terms if t in text)
    return (term_hits,
            len(_bigrams(text) & _bigrams(sentence['text'])),
            candidate['out_tick'] - candidate['in_tick'],
            candidate.get('evidence', {}).get('frame_count', 0))


def _window_free(used, source_sha, in_tick, out_tick):
    return all(out_tick <= u[0] or in_tick >= u[1] for u in used.get(source_sha, []))


def _first_free_window(used, candidate, need_ticks):
    """Earliest free sub-window of the candidate's evidence window that does not
    overlap any window already used from the same source (display shots)."""
    base_in, base_out = candidate['in_tick'], candidate['out_tick']
    if base_out - base_in < need_ticks:
        return None
    intervals = sorted(w for w in used.get(candidate['source_sha256'], [])
                       if w[1] > base_in and w[0] < base_out)
    start = base_in
    for a, b in intervals:
        if a - start >= need_ticks:
            break
        start = max(start, b)
    if base_out - start >= need_ticks:
        return (start, start + need_ticks)
    return None


def _sentence_action(sentence):
    """(intent, is_action_requirement) — an in-progress action requirement drives
    confirmed-interval selection; completed-state sentences are display needs."""
    intent = parse_action_intent(sentence['text'])
    need = sentence['visual']['required'][0] if sentence['visual']['required'] else None
    return intent, need in ACTION_USES and intent['state'] == 'in_progress'


def _confirmed_window(used, candidate, intent, need_ticks, confirmed):
    """Pick the window for an action sentence: earliest CONFIRMED interval for
    (source, kind, object) that fits. The interval's reviewer/basis/hash is the
    recorded time-segment evidence — nothing else qualifies."""
    for interval in _confirmed_intervals_for(confirmed, candidate['source_sha256'],
                                             intent['action_kind'], intent['action_object'],
                                             need_ticks):
        window = _first_confirmed_window(used, candidate['source_sha256'], interval, need_ticks)
        if window is not None:
            return window, interval
    return None, None


def _proposed_windows(library, intent):
    """Inferred proposals for a missing action sentence: surfaced explicitly so
    the operator can review and confirm a REAL window; never auto-selected."""
    proposals = []
    asset_by_sha = {s['sha256']: aid for aid, s in (library.get('sources') or {}).items()}
    for sha, runs in (library.get('action_runs') or {}).items():
        for r in runs:
            if r['kind'] != intent['action_kind'] or r['object'] != intent['action_object']:
                continue
            proposals.append({'source_asset_id': asset_by_sha.get(sha), 'source_sha256': sha,
                              'in_tick': r['span'][0], 'out_tick': r['span'][1],
                              'evidence': r['evidence'], 'ticks': r['ticks'],
                              'gaps': r.get('gaps', [])})
            if len(proposals) >= 8:
                return proposals
    return proposals


def _require_semantic_library(library):
    version = (library.get('config') or {}).get('use_rule_version')
    if version != RULE_VERSION:
        raise ValueError('LIBRARY_RULE_VERSION_STALE:' + str(version))


def plan_shots(script_plan, narration_plan, library, constraints, confirmed_windows=None,
               reusable_intervals=None):
    _require_semantic_library(library)
    grid = constraints.get('grid_ticks', GRID)
    if grid != GRID:
        raise ValueError('GRID_MUST_BE_1920')
    min_clip = constraints.get('min_clip_ticks', GRID)
    fixed_assets = constraints.get('fixed_assets') or []
    sentences = script_plan['sentences']
    windows = {s['sentence_id']: s for s in narration_plan['sentences'] if s.get('status') == 'aligned'
               and s.get('in_tick') is not None and s.get('out_tick') is not None}
    order = [s for s in sentences if s['sentence_id'] in windows]

    opening = [f for f in fixed_assets if f['role'] == 'fixed_opening']
    locked_body = [f for f in fixed_assets if f['role'] == 'locked_body']
    closing = [f for f in fixed_assets if f['role'] == 'fixed_closing']
    if len(opening) > 1 or len(closing) > 1:
        raise ValueError('DUPLICATE_FIXED_ROLE')
    body_start = _grid(sum(f['duration_ticks'] for f in opening))

    spans = []
    for index, s in enumerate(order):
        w = windows[s['sentence_id']]
        start = body_start if index == 0 else _ceil(body_start + w['in_tick'])
        if index + 1 < len(order):
            end = _ceil(body_start + windows[order[index + 1]['sentence_id']]['in_tick'])
        else:
            end = _ceil(body_start + w['out_tick'])
        if spans:
            start = max(start, spans[-1][1])
        spans.append((start, max(end, start + grid)))

    candidates = [c for c in library.get('candidates', []) if c.get('usable')]
    for c in candidates:
        if c.get('fixed_role') is not None or c.get('speech_locked'):
            raise ValueError('FIXED_SOURCE_IN_FREE_POOL:' + str(c['candidate_id']))
    product_terms = script_plan.get('product_terms') or []
    # confirmed evidence pool: job-bound windows + hash-reusable reviewed intervals.
    # These are the ONLY auto-selectable action basis (M1 clarification); inferred
    # runs from observations are proposals, surfaced via missing.proposed_windows.
    confirmed_pool = list(reusable_intervals or [])
    for conf in confirmed_windows or []:
        confirmed_pool.append({'source_sha256': conf.get('source_sha256'),
                               'action_kind': conf.get('action_kind'),
                               'action_object': conf.get('action_object'),
                               'in_tick': conf['in_tick'], 'out_tick': conf['out_tick'],
                               'operator_id': conf.get('operator_id'),
                               'basis': conf.get('basis', ''),
                               'origin': {'kind': 'job_payload', 'sentence_id': conf.get('sentence_id'),
                                          'source_asset_id': conf.get('source_asset_id')}})
    used = {}
    clips, missing = [], []
    clip_counter = [0]

    def add_fixed(f, start):
        clip_counter[0] += 1
        clips.append({'clip_id': 'clip-fixed-%02d' % clip_counter[0], 'source': 'fixed',
                      'candidate_id': None, 'asset_id': f['asset_id'], 'source_sha256': f['source_sha256'],
                      'source_in_tick': f.get('source_in_tick', 0), 'source_out_tick': f['duration_ticks'],
                      'timeline_start_tick': start, 'duration_tick': f['duration_ticks'],
                      'sentence_ids': [], 'match_reason': 'operator-declared fixed segment keeps full original audio',
                      'match_evidence': {'role': f['role']}, 'fixed_role': f['role'], 'overlap_exception': None})
        return start + f['duration_ticks']

    fixed_cursor = 0
    for f in opening:
        fixed_cursor = add_fixed(f, fixed_cursor)

    def pool_sorted(sentence, need_ticks, used_map, relaxed=False):
        target = dict(sentence, visual=dict(sentence['visual'], required=[])) if relaxed else sentence
        intent, is_action = _sentence_action(sentence)
        pool = []
        for c in candidates:
            ok, _reason = candidate_eligibility(c, target, product_terms, need_ticks=need_ticks,
                                                confirmed=confirmed_pool)
            if not ok:
                continue
            if is_action and not relaxed:
                window, interval = _confirmed_window(used_map, c, intent, need_ticks, confirmed_pool)
                if window is None:
                    continue
                pool.append((c, window, interval))
            else:
                window = _first_free_window(used_map, c, need_ticks)
                if window is None:
                    continue
                pool.append((c, window, None))
        pool.sort(key=lambda cwr: (-_score(cwr[0], sentence, product_terms)[0], -_score(cwr[0], sentence, product_terms)[1],
                                   -(cwr[0]['out_tick'] - cwr[0]['in_tick']), cwr[0]['source_sha256'], cwr[0]['in_tick']))
        return pool

    def pick(sentence, need_ticks, used_map):
        """Single shot honoring the required action (shared hard constraints)."""
        pool = pool_sorted(sentence, need_ticks, used_map)
        return pool[0] if pool else None

    def pick_relaxed(sentence, need_ticks, used_map):
        """Second shot of a split: same product/state constraints; the required
        action category was already satisfied by the lead shot."""
        pool = pool_sorted(sentence, need_ticks, used_map, relaxed=True)
        return pool[0] if pool else None

    def missing_reason(sentence):
        if parse_action_intent(sentence['text'])['uncertainty']:
            return 'SENTENCE_SEMANTICS_UNRESOLVED'
        for c in library.get('candidates', []):
            _ok, reason = candidate_eligibility(c, sentence, product_terms, confirmed=confirmed_pool)
            if reason == 'ACTION_EVIDENCE_INSUFFICIENT':
                return 'SHOT_EVIDENCE_INSUFFICIENT'
        return 'NO_RELATED_CANDIDATE_FOR_REQUIRED_USE'

    def confirm_window(sentence, g_start, g_end):
        """Operator-confirmed action window: bound to THIS sentence's (kind,
        object) and an explicit source range; never unlocks anything else."""
        conf = next((c for c in (confirmed_windows or [])
                     if c.get('sentence_id') == sentence['sentence_id']), None)
        if conf is None:
            return False
        intent = parse_action_intent(sentence['text'])
        if intent['uncertainty'] or not intent['action_kind']:
            return False   # an unresolved sentence has nothing to confirm against
        src = (library.get('sources') or {}).get(conf.get('source_asset_id'))
        if src is None or src.get('sha256') != conf.get('source_sha256'):
            raise ValueError('CONFIRMED_WINDOW_SOURCE_NOT_IN_LIBRARY:' + sentence['sentence_id'])
        if not (0 <= conf['in_tick'] < conf['out_tick'] <= src.get('duration_ticks', 0)):
            raise ValueError('CONFIRMED_WINDOW_OUT_OF_BOUNDS:' + sentence['sentence_id'])
        duration = g_end - g_start
        if conf['out_tick'] - conf['in_tick'] != duration:
            raise ValueError('CONFIRMED_WINDOW_DURATION_MISMATCH:%s need=%d got=%d'
                             % (sentence['sentence_id'], duration, conf['out_tick'] - conf['in_tick']))
        if conf.get('action_kind') != intent['action_kind'] or conf.get('action_object') != intent['action_object']:
            return False   # bound to a different action than the sentence requires: not applied
        grid_texts = ' '.join((o.get('text') or '') for o in
                              (library.get('observation_grid') or {}).get(src['sha256'], [])
                              if conf['in_tick'] <= o.get('tick', -1) < conf['out_tick'])
        if terms_lock(product_terms) and not src.get('product_identity_verified') \
                and not any(t in grid_texts for t in product_terms):
            raise ValueError('CONFIRMED_WINDOW_PRODUCT_NOT_SHOWN:' + sentence['sentence_id'])
        overlap_ok = conf.get('allow_source_window_overlap', False)
        if not overlap_ok and not _window_free(used, src['sha256'], conf['in_tick'], conf['out_tick']):
            raise ValueError('CONFIRMED_WINDOW_SOURCE_OVERLAP_FORBIDDEN:' + sentence['sentence_id'])
        clip_counter[0] += 1
        clips.append({'clip_id': 'clip-%03d' % clip_counter[0], 'source': 'pool', 'candidate_id': None,
                      'asset_id': conf['source_asset_id'], 'source_sha256': src['sha256'],
                      'source_in_tick': conf['in_tick'], 'source_out_tick': conf['out_tick'],
                      'timeline_start_tick': g_start, 'duration_tick': duration,
                      'sentence_ids': [sentence['sentence_id']],
                      'match_reason': 'operator-confirmed action window: %s' % conf.get('basis', ''),
                      'match_evidence': {'action_kind': intent['action_kind'],
                                         'action_object': intent['action_object'],
                                         'confirmed_window': {'in_tick': conf['in_tick'],
                                                              'out_tick': conf['out_tick'],
                                                              'evidence': 'operator_confirmed',
                                                              'operator_id': conf.get('operator_id'),
                                                              'basis': conf.get('basis', '')},
                                         'observation_texts': [t[:200] for t in grid_texts.split(' ') if t][:3]},
                      'fixed_role': None,
                      'overlap_exception': ({'reason': conf.get('basis', 'operator allowed reuse'),
                                             'operator_choice': True} if
                                            not _window_free(used, src['sha256'], conf['in_tick'], conf['out_tick'])
                                            else None)})
        used.setdefault(src['sha256'], []).append((conf['in_tick'], conf['out_tick']))
        return True

    def terms_lock(product_terms_):
        return [t for t in (product_terms_ or []) if len(t) >= 2]

    for sentence, (g_start, g_end) in zip(order, spans):
        if confirm_window(sentence, g_start, g_end):
            continue
        duration = g_end - g_start
        picked = pick(sentence, duration, used)
        if picked is not None:
            chosen, window, interval = picked
            clip_counter[0] += 1
            evidence = {'use': chosen['use'], 'candidate_window': [chosen['in_tick'], chosen['out_tick']],
                        'observation_texts': chosen['evidence'].get('observation_texts', [])[:3]}
            if interval is not None:
                evidence['action_evidence'] = 'confirmed_interval'
                evidence['confirmed'] = {k: interval.get(k) for k in
                                         ('in_tick', 'out_tick', 'operator_id', 'basis', 'origin')}
            clips.append({'clip_id': 'clip-%03d' % clip_counter[0], 'source': 'pool',
                          'candidate_id': chosen['candidate_id'], 'asset_id': chosen['source_asset_id'],
                          'source_sha256': chosen['source_sha256'],
                          'source_in_tick': window[0], 'source_out_tick': window[1],
                          'timeline_start_tick': g_start, 'duration_tick': duration,
                          'sentence_ids': [sentence['sentence_id']],
                          'match_reason': 'use=%s; action served from an operator-CONFIRMED interval; deterministic hash tie-break'
                                          % chosen['use'],
                          'match_evidence': evidence,
                          'fixed_role': None, 'overlap_exception': None})
            used.setdefault(chosen['source_sha256'], []).append(window)
            continue
        # No single window covers the sentence: try two shots. The lead shot honors
        # the required action; the follow-up must still match product/state rules.
        lead = pick(sentence, max(min_clip, grid), used)
        if lead is None:
            requirement = sentence['visual']['required'] or sentence['visual']['preferred']
            entry = {'sentence_id': sentence['sentence_id'], 'requirement': requirement,
                     'reason': missing_reason(sentence)}
            intent, _is_action = _sentence_action(sentence)
            if entry['reason'] in ('SHOT_EVIDENCE_INSUFFICIENT', 'NO_RELATED_CANDIDATE_FOR_REQUIRED_USE') \
                    and intent['action_kind']:
                entry['proposed_windows'] = _proposed_windows(library, intent)
            missing.append(entry)
            continue
        d1 = min(duration - min_clip, (lead[0]['out_tick'] - lead[0]['in_tick']) // grid * grid)
        d1 = max(min_clip, d1)
        d2 = duration - d1
        lead_window = (lead[1][0], lead[1][0] + d1)
        used.setdefault(lead[0]['source_sha256'], []).append(lead_window)
        follow = pick_relaxed(sentence, d2, used) if d2 >= min_clip else None
        if follow is None or follow[0]['candidate_id'] == lead[0]['candidate_id']:
            used[lead[0]['source_sha256']].remove(lead_window)
            requirement = sentence['visual']['required'] or sentence['visual']['preferred']
            entry = {'sentence_id': sentence['sentence_id'], 'requirement': requirement,
                     'reason': missing_reason(sentence)}
            intent2, _a2 = _sentence_action(sentence)
            if entry['reason'] in ('SHOT_EVIDENCE_INSUFFICIENT', 'NO_RELATED_CANDIDATE_FOR_REQUIRED_USE') \
                    and intent2['action_kind']:
                entry['proposed_windows'] = _proposed_windows(library, intent2)
            missing.append(entry)
            continue
        for (shot, shot_window, shot_interval), offset, length, why in (
                (lead, g_start, d1, 'lead shot honors required action'),
                ((follow[0], follow[1], None), g_start + d1, d2, 'follow-up shot covers remaining sentence span')):
            window = lead_window if shot is lead[0] else shot_window
            clip_counter[0] += 1
            evidence = {'use': shot['use'], 'candidate_window': [shot['in_tick'], shot['out_tick']]}
            if shot is lead[0] and shot_interval is not None:
                evidence['action_evidence'] = 'confirmed_interval'
                evidence['confirmed'] = {k: shot_interval.get(k) for k in
                                         ('in_tick', 'out_tick', 'operator_id', 'basis', 'origin')}
            clips.append({'clip_id': 'clip-%03d' % clip_counter[0], 'source': 'pool',
                          'candidate_id': shot['candidate_id'], 'asset_id': shot['source_asset_id'],
                          'source_sha256': shot['source_sha256'],
                          'source_in_tick': window[0], 'source_out_tick': window[1],
                          'timeline_start_tick': offset, 'duration_tick': length,
                          'sentence_ids': [sentence['sentence_id']],
                          'match_reason': 'split into two related shots (%s): no single candidate window covered the sentence' % why,
                          'match_evidence': evidence,
                          'fixed_role': None, 'overlap_exception': None})
            if shot is not lead[0]:  # the lead window was reserved before picking the follow-up
                used.setdefault(shot['source_sha256'], []).append(window)

    body_end = spans[-1][1] if spans else body_start
    for f in locked_body:
        body_end = add_fixed(f, _grid(body_end))
    for f in closing:
        add_fixed(f, _grid(body_end))

    tail_pause = 0
    if spans:
        last_audio_end = body_start + windows[order[-1]['sentence_id']]['out_tick']
        tail_pause = max(0, spans[-1][1] - _ceil(last_audio_end))
        if tail_pause > GRID:
            raise ValueError('TAIL_PAUSE_EXCEEDS_ONE_FRAME')
    sentence_rows = []
    for index, s in enumerate(order):
        w = windows[s['sentence_id']]
        nxt = windows[order[index + 1]['sentence_id']]['in_tick'] if index + 1 < len(order) else w['out_tick']
        sentence_rows.append({'sentence_id': s['sentence_id'], 'in_tick': w['in_tick'],
                              'out_tick': max(w['in_tick'] + 1, min(w['out_tick'], nxt)),
                              'audio_in_tick': w.get('audio_in_tick', w['in_tick']),
                              'audio_out_tick': w.get('audio_out_tick', max(w['in_tick'] + 1, min(w['out_tick'], nxt))),
                              'redub': bool(w.get('redub', False)),
                              'claim_ids': s['claim_ids']})
    decision = {
        'schema_version': 'edit-decision/v1',
        'job_id': script_plan.get('job_id', 'job'), 'revision': script_plan.get('revision', 1),
        'inputs': {'script_sha256': script_plan['script_sha256'],
                   'narration_audio_sha256': narration_plan['source']['audio_sha256'],
                   'library_sha256': library.get('library_sha256', '0' * 64)},
        'clips': sorted(clips, key=lambda c: c['timeline_start_tick']),
        'missing': missing,
        'tail_pause_ticks': tail_pause,
        'narration': {'audio_sha256': narration_plan['source']['audio_sha256'],
                      'body_start_tick': body_start,
                      'sentences': sentence_rows},
        'compiled_timeline_sha256': None,
    }
    return validate_named(decision, 'edit-decision-v1')


def apply_replacements(decision, library, replacements, script_plan=None, confirmed=None,
                       reusable_intervals=None):
    """Operator single-shot replacements: same duration, evidence-checked windows.

    Every replacement must pass the same product/action hard constraints as auto
    planning (an action sentence may only be replaced onto a source carrying a
    CONFIRMED interval for the sentence's (kind, object) — inferred runs never
    qualify). Overlapping an already-used source window requires an explicit
    reason and is recorded as an overlap_exception; nothing is replaced silently.
    There is no semantic override: a different action kind/object is a script
    change, not a swap."""
    _require_semantic_library(library)
    confirmed_pool = list(reusable_intervals or []) + [
        {'source_sha256': c.get('source_sha256'), 'action_kind': c.get('action_kind'),
         'action_object': c.get('action_object'), 'in_tick': c['in_tick'], 'out_tick': c['out_tick'],
         'operator_id': c.get('operator_id'), 'basis': c.get('basis', ''),
         'origin': {'kind': 'job_payload', 'sentence_id': c.get('sentence_id')}}
        for c in (confirmed or [])]
    by_clip = {c['clip_id']: c for c in decision['clips']}
    by_candidate = {c['candidate_id']: c for c in library.get('candidates', [])}
    sentences = {s['sentence_id']: s for s in (script_plan or {}).get('sentences', [])}
    product_terms = (script_plan or {}).get('product_terms') or []
    used = {}
    for c in decision['clips']:
        if c['source'] == 'pool':
            used.setdefault(c['source_sha256'], []).append((c['source_in_tick'], c['source_out_tick']))
    for r in replacements:
        clip = by_clip.get(r['clip_id'])
        cand = by_candidate.get(r['candidate_id'])
        if not clip or clip['source'] != 'pool':
            raise ValueError('REPLACEMENT_TARGET_NOT_POOL_CLIP:' + str(r.get('clip_id')))
        if not cand or not cand.get('usable'):
            raise ValueError('REPLACEMENT_CANDIDATE_UNUSABLE:' + str(r.get('candidate_id')))
        sentence = next((sentences[sid] for sid in clip.get('sentence_ids') or [] if sid in sentences), None)
        duration = clip['duration_tick']
        if sentence is not None:
            ok, reason = candidate_eligibility(cand, sentence, product_terms, used=None, need_ticks=duration,
                                               confirmed=confirmed_pool)
            if not ok:
                raise ValueError('REPLACEMENT_' + reason)
        if cand['out_tick'] - cand['in_tick'] < duration:
            raise ValueError('REPLACEMENT_WINDOW_TOO_SHORT')
        # Free ONLY this clip's own window instance; every other clip (including
        # the same source's other windows, even an identical tuple) still blocks.
        old = (clip['source_in_tick'], clip['source_out_tick'])
        remaining = {k: [w for w in v] for k, v in used.items()}
        kept, freed = [], False
        for w in remaining.get(clip['source_sha256'], []):
            if not freed and w == old:
                freed = True
            else:
                kept.append(w)
        remaining[clip['source_sha256']] = kept
        intent, is_action = _sentence_action(sentence) if sentence is not None else (None, False)
        if sentence is not None and is_action:
            new_window, _interval = _confirmed_window(remaining, cand, intent, duration, confirmed_pool)
            if new_window is None:
                # a confirmed interval exists but every window collides with other
                # clips: overlap territory — refused unless explicitly allowed
                unblocked, _ = _confirmed_window({}, cand, intent, duration, confirmed_pool)
                if unblocked is None:
                    raise ValueError('REPLACEMENT_ACTION_WINDOW_UNCONFIRMED:' + str(r.get('candidate_id')))
                if not r.get('allow_source_window_overlap'):
                    raise ValueError('REPLACEMENT_SOURCE_WINDOW_OVERLAP_FORBIDDEN')
                new_window = unblocked
        else:
            new_window = (cand['in_tick'], cand['in_tick'] + duration)
        needs_exception = not _window_free(remaining, cand['source_sha256'], new_window[0], new_window[1])
        if needs_exception and not r.get('allow_source_window_overlap'):
            raise ValueError('REPLACEMENT_SOURCE_WINDOW_OVERLAP_FORBIDDEN')
        remaining.setdefault(cand['source_sha256'], []).append(new_window)
        used.clear(); used.update(remaining)
        clip.update(candidate_id=cand['candidate_id'], asset_id=cand['source_asset_id'],
                    source_sha256=cand['source_sha256'],
                    source_in_tick=new_window[0], source_out_tick=new_window[1],
                    match_reason='operator replacement: ' + r.get('reason', ''),
                    match_evidence={'use': cand['use'], 'candidate_window': [cand['in_tick'], cand['out_tick']],
                                    'observation_texts': cand['evidence'].get('observation_texts', [])[:3]})
        clip['overlap_exception'] = {'reason': r.get('reason', 'operator allowed reuse'),
                                     'operator_choice': True} if needs_exception else None
    return decision


# ---- whole-clip selection & merge (USER_SCOPE_WHOLE_CLIP_SELECTION, new default) ----

def _whole_source_units(library, product_terms, script_text):
    """One unit per product-role SOURCE FILE: whole-file window, coarse relevance
    from its own observations. Clearly unrelated sources are excluded; nothing is
    cut, looped or re-timed here.

    Relevance basis (r6 repair): when the operator gave EXPLICIT product terms,
    a source must show one of them (or carry verified identity) — description
    similarity can never substitute a different product (西瓜/耳机 footage is
    not 火龙果). The conservative script-content-bigram basis applies ONLY when
    no explicit terms exist; with no basis at all the source stays excluded."""
    units = []
    terms = [t for t in (product_terms or []) if len(t) >= 2]
    action_grams = _action_grams()
    script_grams = _bigrams(script_text or '') - action_grams - _GENERIC_GRAMS
    texts_by_sha = {}
    for c in library.get('candidates', []):
        texts_by_sha.setdefault(c['source_sha256'], []).append(c)
    for asset_id, src in (library.get('sources') or {}).items():
        cands = texts_by_sha.get(src['sha256'], [])
        if not cands:
            continue
        ok = [c for c in cands if c.get('usable') and not c.get('speech_locked')
              and c.get('occlusion') != 'severe']
        if not ok:
            continue
        text = ' '.join(_candidate_text(c) for c in cands)
        identity = src.get('product_identity_verified') or any(c.get('product_identity_verified') for c in cands)
        if terms:
            related = identity or any(t in text for t in terms)
        else:
            related = identity or bool(script_grams and (_bigrams(text) & script_grams))
        if not related:
            continue   # 明确无关/错误主体：排除，不参与填充
        duration = ((src.get('duration_ticks', 0) + GRID - 1) // GRID) * GRID
        if duration < GRID:
            continue
        units.append({'asset_id': asset_id, 'source_sha256': src['sha256'],
                      'duration_ticks': duration, 'text': text, 'identity': identity,
                      'script_grams': len(_bigrams(text) & script_grams) if script_grams else 0})
    units.sort(key=lambda u: (u['source_sha256'],))
    return units


def plan_whole_clips(script_plan, narration_plan, library, constraints, whole_clip_replacements=None):
    """Whole-clip default flow: pick WHOLE source files by coarse relevance,
    ordered by the script; adjacent sentences may share one whole clip; each
    source is used at most once; total picture length prefers the narration
    length. No internal cuts, no loops, no speed changes, no confirmed windows.
    Material shortage is reported honestly (WHOLE_CLIP_MATERIAL_SHORT)."""
    _require_semantic_library(library)
    if constraints.get('grid_ticks', GRID) != GRID:
        raise ValueError('GRID_MUST_BE_1920')
    fixed_assets = constraints.get('fixed_assets') or []
    opening = [f for f in fixed_assets if f['role'] == 'fixed_opening']
    locked_body = [f for f in fixed_assets if f['role'] == 'locked_body']
    closing = [f for f in fixed_assets if f['role'] == 'fixed_closing']
    if len(opening) > 1 or len(closing) > 1:
        raise ValueError('DUPLICATE_FIXED_ROLE')
    body_start = _grid(sum(f['duration_ticks'] for f in opening))
    sentences = script_plan['sentences']
    windows = {s['sentence_id']: s for s in narration_plan['sentences'] if s.get('status') == 'aligned'}
    order = [s for s in sentences if s['sentence_id'] in windows]
    if not order:
        raise ValueError('NARRATION_HAS_NO_ALIGNED_SENTENCES')

    product_terms = script_plan.get('product_terms') or []
    units = _whole_source_units(library, product_terms, script_plan.get('script_text') or '')
    by_asset = {u['asset_id']: u for u in units}
    replacements = {r['clip_id']: r for r in (whole_clip_replacements or [])}
    body_end = body_start + max(w['out_tick'] for w in windows.values())
    body_need = body_end - body_start

    # r5 repair: length-aware coarse ranking. Relevance first; among equally
    # relevant sources prefer the one whose WHOLE length comes closest to the
    # remaining audio need (then the longer) — a 6s clip serves a 5s narration
    # better than a 2s one, and combination filling uses several whole clips.
    def score(unit, sentence, remaining):
        text = unit['text']
        return (sum(1 for t in product_terms if t in text),
                len(_bigrams(text) & _bigrams(sentence['text'])),
                -abs(unit['duration_ticks'] - remaining),
                unit['duration_ticks'])

    groups = []          # [(unit, [sentence_ids])] in narrative order
    extras = []          # coverage fill: whole clips beyond the anchors
    used_assets = set()
    cursor_need = body_need
    for sentence in order:
        avail = [u for u in units if u['asset_id'] not in used_assets]
        span = min(cursor_need, max(1, windows[sentence['sentence_id']]['out_tick']
                                    - windows[sentence['sentence_id']]['in_tick']))
        if groups and avail:
            prev = groups[-1][0]
            if score(prev, sentence, span) >= max(score(u, sentence, span) for u in avail):
                groups[-1][1].append(sentence['sentence_id'])
                continue
        if not avail:
            if groups:
                groups[-1][1].append(sentence['sentence_id'])   # reuse tail group when exhausted
                continue
            return {'schema_version': 'edit-decision/v1',
                    'job_id': script_plan.get('job_id', 'job'), 'revision': script_plan.get('revision', 1),
                    'inputs': {'script_sha256': script_plan['script_sha256'],
                               'narration_audio_sha256': narration_plan['source']['audio_sha256'],
                               'library_sha256': library.get('library_sha256', '0' * 64)},
                    'clips': [], 'missing': [{'sentence_id': order[0]['sentence_id'],
                                              'requirement': ['whole_clip_material'],
                                              'reason': ('WHOLE_CLIP_RELEVANCE_BASIS_MISSING:没有可用的相关完整素材（可填写商品关键词或资料提供粗匹配依据）'
                                                         if not [t for t in product_terms if len(t) >= 2] else
                                                         'WHOLE_CLIP_PRODUCT_NOT_SHOWN:整条素材库中没有显示指定商品的完整素材；不能以描述相似用其它商品替代')}],
                    'tail_pause_ticks': 0,
                    'narration': {'audio_sha256': narration_plan['source']['audio_sha256'],
                                  'body_start_tick': body_start, 'sentences': []},
                    'compiled_timeline_sha256': None}
        best = max(avail, key=lambda u: score(u, sentence, span))
        used_assets.add(best['asset_id'])
        cursor_need = max(GRID, cursor_need - best['duration_ticks'])
        groups.append((best, [sentence['sentence_id']]))

    # coverage fill: one sentence may be served by SEVERAL whole clips; keep
    # adding the most script-relevant unused whole sources until the narration
    # is covered or relevant sources are exhausted (no cuts, loops, re-timing).
    def fill_score(unit):
        shortfall = max(0, body_need - sum(u['duration_ticks'] for u, _ in groups)
                        - sum(u['duration_ticks'] for u in extras))
        return (unit['script_grams'],
                -abs(unit['duration_ticks'] - shortfall),
                unit['duration_ticks'])

    while sum(u['duration_ticks'] for u, _ in groups) + sum(u['duration_ticks'] for u in extras) < body_need:
        avail = [u for u in units if u['asset_id'] not in used_assets]
        if not avail:
            break
        nxt = max(avail, key=fill_score)
        used_assets.add(nxt['asset_id'])
        extras.append(nxt)

    # whole-clip replacements: swap one chosen clip's whole source (coarse
    # relevance still enforced); timeline re-flows, narration untouched
    chosen = [(u, sids) for u, sids in groups] + [(u, []) for u in extras]
    for group_index, (unit, sids) in enumerate(list(chosen)):
        clip_id = 'clip-wc-%02d' % (group_index + 1)
        r = replacements.get(clip_id)
        if not r:
            continue
        new_unit = by_asset.get(r.get('source_asset_id'))
        if new_unit is None:
            raise ValueError('WHOLE_CLIP_REPLACEMENT_SOURCE_NOT_IN_LIBRARY:' + str(r.get('source_asset_id')))
        if new_unit['asset_id'] in {u['asset_id'] for i, (u, _s) in enumerate(chosen) if i != group_index}:
            raise ValueError('WHOLE_CLIP_REPLACEMENT_SOURCE_ALREADY_USED:' + new_unit['asset_id'])
        chosen[group_index] = (new_unit, sids)
    groups = [g for g in chosen if g[1]]
    extras = [u for u, sids in chosen if not sids]

    picture_total = sum(u['duration_ticks'] for u, _ in groups) + sum(u['duration_ticks'] for u in extras)
    missing = []
    if picture_total < body_need:
        shortage = body_need - picture_total
        missing.append({'sentence_id': order[-1]['sentence_id'], 'requirement': ['whole_clip_material'],
                        'reason': 'WHOLE_CLIP_MATERIAL_SHORT:整条素材总长不足，缺口约 %.1f 秒；请补充或换用更长的相关完整素材'
                                  % (shortage / 48000)})

    clips = []
    counter = [0]

    def add_fixed(f, start):
        counter[0] += 1
        clips.append({'clip_id': 'clip-fixed-%02d' % counter[0], 'source': 'fixed',
                      'candidate_id': None, 'asset_id': f['asset_id'], 'source_sha256': f['source_sha256'],
                      'source_in_tick': 0, 'source_out_tick': f['duration_ticks'],
                      'timeline_start_tick': start, 'duration_tick': f['duration_ticks'],
                      'sentence_ids': [], 'match_reason': 'operator-declared fixed segment keeps full original audio',
                      'match_evidence': {'role': f['role']}, 'fixed_role': f['role'], 'overlap_exception': None})
        return start + f['duration_ticks']

    cursor = 0
    for f in opening:
        cursor = add_fixed(f, cursor)
    emitted = [(u, sids, False) for u, sids in groups] + [(u, [], True) for u in extras]
    for group_index, (unit, sids, is_extra) in enumerate(emitted):
        counter[0] += 1
        clips.append({'clip_id': 'clip-wc-%02d' % (group_index + 1), 'source': 'pool', 'candidate_id': None,
                      'asset_id': unit['asset_id'], 'source_sha256': unit['source_sha256'],
                      'source_in_tick': 0, 'source_out_tick': unit['duration_ticks'],
                      'timeline_start_tick': cursor, 'duration_tick': unit['duration_ticks'],
                      'sentence_ids': list(sids),
                      'match_reason': ('whole-clip: 组合补足整条素材（覆盖旁白长度，不裁切）' if is_extra
                                       else 'whole-clip: 整条源文件按整体相关度选择并合并且不裁切'),
                      'match_evidence': {'mode': 'whole_clip', 'identity': unit['identity'],
                                         'coverage_fill': is_extra,
                                         'observation_digest': unit['text'][:200]},
                      'fixed_role': None, 'overlap_exception': None})
        cursor += unit['duration_ticks']
    body_cursor = _grid(cursor)
    for f in locked_body:
        body_cursor = add_fixed(f, body_cursor)
    for f in closing:
        add_fixed(f, _grid(body_cursor))

    sentence_rows = []
    for index, s in enumerate(order):
        w = windows[s['sentence_id']]
        nxt = windows[order[index + 1]['sentence_id']]['in_tick'] if index + 1 < len(order) else w['out_tick']
        sentence_rows.append({'sentence_id': s['sentence_id'], 'in_tick': w['in_tick'],
                              'out_tick': max(w['in_tick'] + 1, min(w['out_tick'], nxt)),
                              'audio_in_tick': w.get('audio_in_tick', w['in_tick']),
                              'audio_out_tick': w.get('audio_out_tick', max(w['in_tick'] + 1, min(w['out_tick'], nxt))),
                              'redub': bool(w.get('redub', False)),
                              'claim_ids': s['claim_ids']})
    decision = {
        'schema_version': 'edit-decision/v1',
        'job_id': script_plan.get('job_id', 'job'), 'revision': script_plan.get('revision', 1),
        'inputs': {'script_sha256': script_plan['script_sha256'],
                   'narration_audio_sha256': narration_plan['source']['audio_sha256'],
                   'library_sha256': library.get('library_sha256', '0' * 64)},
        'clips': sorted(clips, key=lambda c: c['timeline_start_tick']),
        'missing': missing,
        'tail_pause_ticks': 0,
        'narration': {'audio_sha256': narration_plan['source']['audio_sha256'],
                      'body_start_tick': body_start,
                      'sentences': sentence_rows},
        'compiled_timeline_sha256': None,
    }
    return validate_named(decision, 'edit-decision-v1')


def apply_whole_clip_replacements(decision, whole_clip_replacements):
    """Marked obsolete: replacements are applied inside plan_whole_clips from the
    job payload so the timeline re-flows in one pass. Kept as a thin validator
    so external callers fail loudly instead of silently re-editing."""
    if whole_clip_replacements:
        raise ValueError('WHOLE_CLIP_REPLACEMENTS_APPLIED_AT_PLANNING')
    return decision


def compile_edit_decision(decision, resources):
    """Compile to a timeline/v1 consumed by the existing renderer and validator."""
    sources = resources['sources']
    timeline_resources = []
    refs = {}

    def add(ref_id, path, sha, kind, probe=None, provenance=None):
        if ref_id not in refs:
            refs[ref_id] = {'ref_id': ref_id, 'path': str(path), 'sha256': sha, 'kind': kind,
                            'probe_ref': probe, 'provenance_ref': provenance}
            timeline_resources.append(refs[ref_id])
        return ref_id

    for asset_id, src in sources.items():
        probe_id = add(asset_id + '-probe', src['probe'].get('path', 'probe://' + asset_id),
                       src['probe'].get('sha256', '0' * 64), 'probe')
        add(asset_id, src['path'], src['sha256'], 'video', probe_id, None)
        if src.get('visual_evidence'):
            add(asset_id + '-visual', src['visual_evidence']['path'], src['visual_evidence']['sha256'], 'evidence')

    narration_ref = add('narration-audio', resources['narration']['path'],
                        resources['narration']['sha256'], 'audio',
                        add('narration-audio-probe', resources['narration']['probe'].get('path', 'probe://narration'),
                            resources['narration']['probe'].get('sha256', '0' * 64), 'probe'))
    alignment_ref = add('alignment', resources['alignment']['path'], resources['alignment']['sha256'], 'evidence')

    video_clips, audio_events = [], []
    for clip in decision['clips']:
        src = sources[clip['asset_id']]
        if clip['source'] == 'fixed':
            locked = src.get('locked') or {}
            boundary = locked.get('boundary') or {}
            duration = src['probe']['duration_ticks']
            full_out = ((duration + GRID - 1) // GRID) * GRID
            if clip['source_in_tick'] != 0 or clip['source_out_tick'] != full_out or clip['duration_tick'] != full_out:
                raise ValueError('FIXED_CLIP_MUST_KEEP_FULL_SOURCE:' + clip['clip_id'])
            boundary_ref = add(clip['asset_id'] + '-boundary', boundary['path'], boundary['sha256'], 'evidence')
            video_clips.append({'clip_id': clip['clip_id'], 'mode': 'spoken_locked',
                                'base_source_ref': clip['asset_id'], 'video_ref': clip['asset_id'],
                                'source_in_tick': 0, 'source_out_tick': full_out,
                                'timeline_start_tick': clip['timeline_start_tick'], 'duration_tick': full_out,
                                'speed': 1, 'boundary_evidence_ref': boundary_ref,
                                'visual_evidence_ref': clip['asset_id'] + '-visual',
                                'lip_evidence_ref': None, 'lip_result_ref': None})
            audio_events.append({'event_id': 'speech-' + clip['clip_id'], 'role': 'dialogue',
                                 'audio_ref': clip['asset_id'], 'stream_index': locked.get('audio_stream_index', 0),
                                 'source_in_tick': 0, 'source_out_tick': full_out,
                                 'timeline_start_tick': clip['timeline_start_tick'], 'duration_tick': full_out,
                                 'gain_db': 0, 'sync_clip_id': clip['clip_id'], 'claim_ids': [],
                                 'alignment_ref': None})
        else:
            if clip['source_out_tick'] - clip['source_in_tick'] != clip['duration_tick']:
                raise ValueError('POOL_CLIP_WINDOW_MISMATCH:' + clip['clip_id'])
            probe_duration = src['probe']['duration_ticks']
            if clip['source_out_tick'] > probe_duration + GRID:
                raise ValueError('POOL_CLIP_WINDOW_OUT_OF_RANGE:' + clip['clip_id'])
            video_clips.append({'clip_id': clip['clip_id'], 'mode': 'broll',
                                'base_source_ref': clip['asset_id'], 'video_ref': clip['asset_id'],
                                'source_in_tick': clip['source_in_tick'], 'source_out_tick': clip['source_out_tick'],
                                'timeline_start_tick': clip['timeline_start_tick'], 'duration_tick': clip['duration_tick'],
                                'speed': 1, 'boundary_evidence_ref': None,
                                'visual_evidence_ref': clip['asset_id'] + '-visual',
                                'lip_evidence_ref': None, 'lip_result_ref': None})
    body_start = decision['narration']['body_start_tick']
    redubs = resources.get('sentence_redubs') or {}

    # Contiguous non-redub sentence windows tile the confirmed narration; merge
    # them into ONE audio event so the rendered stem is a 1:1 copy of the
    # operator-confirmed audio (no per-event trim/resample artifacts). A redubbed
    # sentence stays its own event; surrounding runs merge separately.
    runs = []
    for s in decision['narration']['sentences']:
        is_redub = bool(s.get('redub')) and s['sentence_id'] in redubs
        if runs and not is_redub and not runs[-1]['redub'] \
                and s['audio_in_tick'] >= runs[-1]['audio_out'] \
                and (body_start + s['in_tick']) - runs[-1]['timeline_end'] \
                    == s['audio_in_tick'] - runs[-1]['audio_out']:
            runs[-1]['audio_out'] = s['audio_out_tick']
            runs[-1]['timeline_end'] = body_start + s['out_tick']
            runs[-1]['sentence_ids'].append(s['sentence_id'])
            runs[-1]['claim_ids'] = sorted(set(runs[-1]['claim_ids']) | set(s['claim_ids']))
        else:
            runs.append({'redub': is_redub, 'sentence_id': s['sentence_id'],
                         'audio_in': s['audio_in_tick'], 'audio_out': s['audio_out_tick'],
                         'timeline_start': body_start + s['in_tick'],
                         'timeline_end': body_start + s['out_tick'],
                         'sentence_ids': [s['sentence_id']], 'claim_ids': list(s['claim_ids'])})
    for run in runs:
        if run['redub']:
            redub = redubs[run['sentence_id']]
            audio_ref = add('redub-' + run['sentence_id'], redub['path'], redub['sha256'], 'audio',
                            add('redub-' + run['sentence_id'] + '-probe', redub['probe'].get('path', 'probe://redub'),
                                redub['probe'].get('sha256', '0' * 64), 'probe'))
            stream = redub['probe']['stream_indices']['audio']
        else:
            audio_ref = narration_ref
            stream = resources['narration']['probe']['stream_indices']['audio']
        audio_events.append({'event_id': 'narration-' + '-'.join(run['sentence_ids'])[:80], 'role': 'narration',
                             'audio_ref': audio_ref, 'stream_index': stream,
                             'source_in_tick': run['audio_in'], 'source_out_tick': run['audio_out'],
                             'timeline_start_tick': run['timeline_start'],
                             'duration_tick': run['timeline_end'] - run['timeline_start'],
                             'gain_db': 0, 'sync_clip_id': None, 'claim_ids': run['claim_ids'],
                             'alignment_ref': alignment_ref})
    cursor = max((c['timeline_start_tick'] + c['duration_tick'] for c in video_clips), default=0)
    bgm = resources.get('bgm')
    if bgm:
        bgm_ref = add('bgm-audio', bgm['path'], bgm['sha256'], 'audio',
                      add('bgm-audio-probe', bgm['probe'].get('path', 'probe://bgm'),
                          bgm['probe'].get('sha256', '0' * 64), 'probe'))
        audio_events.append({'event_id': 'bgm', 'role': 'bgm', 'audio_ref': bgm_ref,
                             'stream_index': bgm['probe']['stream_indices']['audio'],
                             'source_in_tick': 0, 'source_out_tick': cursor,
                             'timeline_start_tick': 0, 'duration_tick': cursor,
                             'gain_db': bgm.get('gain_db', -32), 'sync_clip_id': None, 'claim_ids': [],
                             'alignment_ref': None})
    voice_stem = resources['voice_stem']
    stem_ref = add('voice-stem', voice_stem['path'], voice_stem['sha256'], 'audio',
                   add('voice-stem-probe', voice_stem['probe'].get('path', 'probe://stem'),
                       voice_stem['probe'].get('sha256', '0' * 64), 'probe'))
    timeline = {
        'schema_version': 'timeline/v1', 'timeline_id': decision['job_id'] + '-r' + str(decision['revision']),
        'job_id': decision['job_id'], 'revision': decision['revision'], 'timescale': 48000,
        'canvas': {'width': 768, 'height': 1376, 'fps': 25, 'sar': '1:1'},
        'duration_ticks': cursor, 'resources': timeline_resources,
        'video_clips': sorted(video_clips, key=lambda c: c['timeline_start_tick']),
        'audio_events': audio_events, 'voice_stem_ref': stem_ref,
        'subtitles': {'source_audio_ref': stem_ref, 'source_audio_sha256': voice_stem['sha256'],
                      'alignment_ref': alignment_ref, 'template': resources['subtitle_template'],
                      'cues': resources['cues']},
        'template_events': [], 'review': {'status': 'pending', 'evidence_refs': []},
    }
    decision['compiled_timeline_sha256'] = canonical_hash(timeline)
    return timeline
