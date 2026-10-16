# -*- coding: utf-8 -*-
"""Beat-paced shot planning over reviewed material cards.

An asset's provenance file may be a material card: one reviewed clip entry from a
material library (content/edit semantics, review status, optional usable window).
Beat mode cuts on the confirmed narration. Every sentence starts a new shot, a
sentence longer than MAX_SHOT_TICKS is split into several short shots, and each
shot is taken from the clean part of the card whose wording best matches the
sentence. Picture length equals the confirmed narration; nothing is looped,
sped up or re-timed.
"""
import math

from scheduler.contracts import validate_named
from scheduler.shot_planner import GRID, _bigrams, _ceil

TIMESCALE = 48000
USABLE_STATUSES = {'approved', 'approved_with_caveats'}
MAX_SHOT_TICKS = 144000     # 3.0 s: longer sentences get more than one shot
MIN_SHOT_TICKS = 57600      # 1.2 s: never split a sentence below this
MAX_TAIL_HOLD_TICKS = 38400  # 0.8 s of picture after the last word, at most
_NO_PRODUCT = {None, '', 'none', 'absent'}
# Problem/proof visualisations only carry a line that actually talks about them;
# they never fill in for a sentence without a wording match.
_MATCH_ONLY_ROLES = {'problem', 'function_proof', 'turn'}
# Vessel / river pictures only illustrate a line that actually says so.
_CIRCULATION_WORDS = ('循环', '血管', '河道', '血流')


def material_from_card(card, duration_ticks):
    """Selection view of a material card; None when the provenance is not a card."""
    if not isinstance(card, dict) or not isinstance(card.get('content'), dict) \
            or not isinstance(card.get('edit'), dict):
        return None
    content, edit = card['content'], card['edit']
    craft = (card.get('creative') or {}).get('real_product_craft') or {}
    parts = [content.get('narration_target'), content.get('story_beat'), content.get('scene'),
             ' '.join(content.get('claim_tags') or [])]
    if craft.get('product_presence') not in _NO_PRODUCT:
        parts += list(craft.get('hero_details') or [])
    window = card.get('usable_window_sec') or [0, None]
    in_tick = _ceil(round(float(window[0] or 0) * TIMESCALE))
    out_tick = duration_ticks if window[1] is None else min(duration_ticks, int(float(window[1]) * TIMESCALE))
    out_tick = out_tick // GRID * GRID
    return {
        'clip_id': str(card.get('clip_id') or ''),
        'shot': str(card.get('shot_id') or card.get('clip_id') or ''),
        'product': craft.get('product_presence') not in _NO_PRODUCT,
        'status': card.get('status'),
        'usable': card.get('status') in USABLE_STATUSES and out_tick - in_tick >= GRID,
        'role': edit.get('edit_role') or 'other',
        'hook': int(edit.get('hook_strength') or 0),
        'narration_target': content.get('narration_target') or '',
        'story_beat': content.get('story_beat') or '',
        'text': ' '.join(p for p in parts if p),
        'in_tick': in_tick, 'out_tick': out_tick,
        'caveat': card.get('status_note') if card.get('status') == 'approved_with_caveats' else None,
    }


def card_coverage(material):
    """Visual evidence for analysis mode material_card: the library's reviewed wording
    over the clean window, labelled as card text rather than model observation."""
    return [{'status': 'success', 'description': material['text'], 'error': None, 'frame_refs': [],
             'in_tick': material['in_tick'], 'out_tick': material['out_tick'],
             'observations': [{'frame_id': 'card-' + material['clip_id'], 'source_tick': material['in_tick'],
                               'text': material['text'], 'model_id': 'material-card',
                               'prompt_version': 'card-v1'}]}]


def script_menu(materials):
    """What the script writer may cover: one line per usable clip, in library order."""
    return [{'画面': m['story_beat'], '口播落点': m['narration_target'],
             '可用秒数': round((m['out_tick'] - m['in_tick']) / TIMESCALE, 1)}
            for m in materials if m['usable']]


def _split(start, end):
    """Grid-aligned shots covering [start, end): as few as keep each <= MAX_SHOT_TICKS
    while none drops below MIN_SHOT_TICKS."""
    length = end - start
    count = max(1, math.ceil(length / MAX_SHOT_TICKS))
    while count > 1 and length / count < MIN_SHOT_TICKS:
        count -= 1
    cuts = [start + (length * i // count) // GRID * GRID for i in range(count)] + [end]
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a >= GRID]


def _closing_reveal(pool, used, uses, previous, need, hold):
    """Bottle shot for the last line. A window one grid short of the tail still counts;
    the picture ends one frame early instead of leaving the bottle off screen."""
    for fit in (need, need - GRID):
        if fit < GRID or (fit < need and need - fit > hold):
            continue
        options = []
        for m in pool:
            if m['role'] != 'product_reveal' or not m['product']:
                continue
            window = _free_window(used, m, fit)
            if window is None:
                continue
            options.append((m['shot'] != previous, uses.get(m['source_sha256'], 0) == 0,
                            m['hook'], m['source_sha256'], m, window))
        if options:
            chosen = max(options)
            return 0, chosen[4], chosen[5], fit
    return None


def _free_window(used, material, need):
    start = material['in_tick']
    for a, b in sorted(used.get(material['source_sha256'], [])):
        if a - start >= need:
            break
        start = max(start, b)
    return (start, start + need) if material['out_tick'] - start >= need else None


def plan_beats(script_plan, narration_plan, materials, constraints):
    if constraints.get('grid_ticks', GRID) != GRID:
        raise ValueError('GRID_MUST_BE_1920')
    if constraints.get('fixed_assets'):
        raise ValueError('BEAT_MODE_FIXED_SEGMENTS_UNSUPPORTED')
    windows = {s['sentence_id']: s for s in narration_plan['sentences'] if s.get('status') == 'aligned'}
    order = [s for s in script_plan['sentences'] if s['sentence_id'] in windows]
    if not order:
        raise ValueError('NARRATION_HAS_NO_ALIGNED_SENTENCES')
    pool = [m for m in materials if m['usable']]

    hold = _ceil(constraints.get('tail_hold_ticks') or 0)
    if hold > MAX_TAIL_HOLD_TICKS:
        raise ValueError('BEAT_TAIL_HOLD_TOO_LONG')
    spans = []
    for index, sentence in enumerate(order):
        w = windows[sentence['sentence_id']]
        start = 0 if index == 0 else _ceil(w['in_tick'])
        end = _ceil(windows[order[index + 1]['sentence_id']]['in_tick']) if index + 1 < len(order) else _ceil(w['out_tick']) + hold
        if spans:
            start = max(start, spans[-1][2])
        spans.append((sentence, start, max(end, start + GRID)))

    used, uses = {}, {}
    clips, missing = [], []
    previous = None
    for position, (sentence, s_start, s_end) in enumerate(spans):
        grams = _bigrams(sentence['text'])
        pieces = _split(s_start, s_end)
        for piece, (start, end) in enumerate(pieces):
            closing = position == len(spans) - 1 and piece == len(pieces) - 1
            need = end - start
            ranked = []
            for m in pool:
                window = _free_window(used, m, need)
                if window is None:
                    continue
                overlap = len(grams & _bigrams(m['text']))
                if m['role'] in _MATCH_ONLY_ROLES and not any(w in sentence['text'] for w in _CIRCULATION_WORDS):
                    overlap = 0
                ranked.append((overlap, m, window))
            reveal = _closing_reveal(pool, used, uses, previous, need, hold) if closing else None
            if not ranked and reveal is None:
                missing.append({'sentence_id': sentence['sentence_id'], 'requirement': ['beat_material'],
                                'reason': 'BEAT_MATERIAL_EXHAUSTED:没有剩余足够长的可用素材窗口（需 %.1f 秒）' % (need / TIMESCALE)})
                continue
            best = max((r[0] for r in ranked), default=0)
            floor = max(1, (best + 1) // 2)
            first = not clips
            fresh = [r for r in ranked if best and r[0] >= floor and r[1]['shot'] != previous]
            if fresh:
                fallback = False
                rel, chosen, window = max(fresh, key=lambda r: (uses.get(r[1]['source_sha256'], 0) == 0, r[0],
                                                                 r[1]['hook'] if first else 0, r[1]['source_sha256']))
            else:
                # No wording match: show the product, the hero reveal on the closing line.
                # A reveal that is one grid short of the hold still closes the film;
                # the picture ends a frame early rather than leaving the bottle off screen.
                fallback = True
                if reveal is not None:
                    rel, chosen, window, need = reveal
                    end = start + need
                else:
                    others = [r for r in ranked if r[1]['shot'] != previous] or ranked
                    safe = [r for r in others if r[1]['role'] not in _MATCH_ONLY_ROLES]
                    if not safe:
                        safe = [r for r in ranked if r[1]['role'] not in _MATCH_ONLY_ROLES]
                    fresh = safe or others
                    rel, chosen, window = max(fresh, key=lambda r: (
                        closing and r[1]['product'] and r[1]['role'] == 'product_reveal', r[1]['product'],
                        uses.get(r[1]['source_sha256'], 0) == 0, r[1]['hook'], r[1]['source_sha256']))
            sha = chosen['source_sha256']
            used.setdefault(sha, []).append(window)
            uses[sha] = uses.get(sha, 0) + 1
            previous = chosen['shot']
            clips.append({'clip_id': 'clip-beat-%03d' % (len(clips) + 1), 'source': 'pool', 'candidate_id': None,
                          'asset_id': chosen['asset_id'], 'source_sha256': sha,
                          'source_in_tick': window[0], 'source_out_tick': window[1],
                          'timeline_start_tick': start, 'duration_tick': need,
                          'sentence_ids': [sentence['sentence_id']],
                          'match_reason': 'beat: 句内第%s镜，按素材口播落点匹配' % (sum(1 for c in clips if c['sentence_ids'] == [sentence['sentence_id']]) + 1),
                          'match_evidence': {'mode': 'beat', 'material_clip_id': chosen['clip_id'],
                                             'relevance_bigrams': rel, 'fallback': fallback,
                                             'narration_target': chosen['narration_target'][:200],
                                             'caveat': chosen['caveat']},
                          'fixed_role': None, 'overlap_exception': None})

    rows = []
    for index, sentence in enumerate(order):
        w = windows[sentence['sentence_id']]
        nxt = windows[order[index + 1]['sentence_id']]['in_tick'] if index + 1 < len(order) else w['out_tick']
        out = max(w['in_tick'] + 1, min(w['out_tick'], nxt))
        audio_in = w.get('audio_in_tick', w['in_tick'])
        # ASR can attach the next sentence's first word to this window. Picture
        # already stops where the next sentence starts; the audio span has to
        # stay the same length, or the merged narration event is rejected.
        audio_out = audio_in + (out - w['in_tick'])
        rows.append({'sentence_id': sentence['sentence_id'], 'in_tick': w['in_tick'], 'out_tick': out,
                     'audio_in_tick': audio_in, 'audio_out_tick': audio_out,
                     'redub': bool(w.get('redub', False)), 'claim_ids': sentence['claim_ids']})
    decision = {
        'schema_version': 'edit-decision/v1',
        'job_id': script_plan.get('job_id', 'job'), 'revision': script_plan.get('revision', 1),
        'inputs': {'script_sha256': script_plan['script_sha256'],
                   'narration_audio_sha256': narration_plan['source']['audio_sha256'],
                   'library_sha256': constraints.get('library_sha256') or '0' * 64},
        'clips': clips, 'missing': missing, 'tail_pause_ticks': hold,
        'narration': {'audio_sha256': narration_plan['source']['audio_sha256'],
                      'body_start_tick': 0, 'sentences': rows},
        'compiled_timeline_sha256': None,
    }
    return validate_named(decision, 'edit-decision-v1')
