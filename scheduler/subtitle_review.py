"""Deterministic script↔audio comparison for subtitle review (S2).

Comparison is NOT a diagnosis of how the voice actually read: homophone and
number differences stay `uncertain` until an operator listens and decides.
Unknown sentence windows stay null; time is never split evenly to fake a match.
"""
import difflib
import re
from scheduler.contracts import validate_named

REDUB = 'SPEECH_ERROR_REDUB_REQUIRED'
_NORM = re.compile(r'[^a-z0-9\u4e00-\u9fff]')


def _norm(text):
    return _NORM.sub('', text.lower())


def compare_script_audio(script_plan, alignment, narration_plan):
    audio_sha = narration_plan['source']['audio_sha256']
    if alignment.get('audio_sha256') != audio_sha:
        raise ValueError('SUBTITLE_ALIGNMENT_STALE:' + str(alignment.get('audio_sha256')))
    duration = narration_plan['source']['actual_duration_ticks']
    words = [w for w in alignment.get('words', []) if w.get('text')]
    sentences = script_plan['sentences']

    # Build normalized char streams with offsets.
    sent_spans = []  # (start, end) in script stream
    S_parts = []
    cursor = 0
    for s in sentences:
        n = _norm(s['text'])
        sent_spans.append((cursor, cursor + len(n)))
        S_parts.append(n)
        cursor += len(n)
    S = ''.join(S_parts)
    word_spans = []  # (start, end) in asr stream, parallel to kept words
    A_parts = []
    acursor = 0
    kept = []
    for w in words:
        n = _norm(w['text'])
        if not n:
            continue
        word_spans.append((acursor, acursor + len(n)))
        A_parts.append(n)
        acursor += len(n)
        kept.append(w)
    A = ''.join(A_parts)

    opcodes = difflib.SequenceMatcher(None, S, A, autojunk=False).get_opcodes() if S and A else []

    # Assign every opcode's script/asr characters to sentences; insertions and
    # replacements attach to the preceding sentence (audio continues past it).
    mapped = [[] for _ in sentences]  # list of (a_start, a_end) A-subranges per sentence
    for tag, a1, a2, b1, b2 in opcodes:
        if tag == 'equal':
            for index, (s0, s1) in enumerate(sent_spans):
                lo, hi = max(a1, s0), min(a2, s1)
                if lo < hi:
                    mapped[index].append((b1 + (lo - a1), b1 + (hi - a1)))
        elif tag in ('insert', 'replace'):
            owner = next((i for i, (s0, s1) in enumerate(sent_spans) if s0 < a1 <= s1), None)
            if owner is None and a1 == 0:
                owner = 0 if sent_spans else None
            if owner is not None and b2 > b1:
                mapped[owner].append((b1, b2))

    items = []
    for index, (s, (s0, s1)) in enumerate(zip(sentences, sent_spans)):
        norm_len = s1 - s0
        ranges = sorted(mapped[index])
        if norm_len == 0 or not ranges:
            items.append({'sentence_id': s['sentence_id'], 'script_text': s['text'], 'asr_text': None,
                          'words': [], 'window': None, 'coverage': 0.0,
                          'diff_type': 'no_match', 'status': 'omission' if norm_len else 'uncertain',
                          'decision': None})
            continue
        a_lo, a_hi = ranges[0][0], ranges[-1][1]
        covered = sum(min(hi, a_hi) - max(lo, a_lo) for lo, hi in ranges)
        covered = min(covered, norm_len)
        hit = [i for i, (w0, w1) in enumerate(word_spans) if w1 > a_lo and w0 < a_hi]
        in_window = [kept[i] for i in range(hit[0], hit[-1] + 1)] if hit else []
        asr_text = ''.join(w['text'] for w in in_window)
        window = None
        if in_window:
            window = {'in_tick': max(0, in_window[0]['start_tick']),
                      'out_tick': min(duration, in_window[-1]['end_tick'])}
        missing = covered < norm_len
        extra = (a_hi - a_lo) > covered
        script_norm = S[s0:s1]
        asr_norm = A[a_lo:a_hi]
        if missing and extra:
            diff = 'mixed'
        elif missing:
            diff = 'missing'
        elif extra:
            diff = 'extra'
        elif script_norm == asr_norm:
            diff = 'none'
        else:
            diff = 'substitution'
        coverage = covered / norm_len
        if diff == 'none':
            status = 'matched'
        elif diff == 'extra':
            status = 'addition'
        elif coverage < 0.25:
            status = 'omission'
        else:
            status = 'uncertain'
        items.append({'sentence_id': s['sentence_id'], 'script_text': s['text'], 'asr_text': asr_text,
                      'words': [{'text': w['text'], 'start_tick': w['start_tick'], 'end_tick': w['end_tick']} for w in in_window],
                      'window': window, 'coverage': round(coverage, 4), 'diff_type': diff,
                      'status': status, 'decision': None})

    # Extend windows over trailing raw words (punctuation/particles with no normalized
    # text) up to the next sentence's start, so cues track the actual spoken audio.
    for index, item in enumerate(items):
        window = item.get('window')
        if not window:
            continue
        nxt = items[index + 1].get('window') if index + 1 < len(items) else None
        boundary = nxt['in_tick'] if nxt else duration
        tail = max((w['end_tick'] for w in words if window['in_tick'] <= w['start_tick'] < boundary), default=window['out_tick'])
        window['out_tick'] = min(duration, max(window['out_tick'], tail))

    review = {
        'schema_version': 'subtitle-review/v1',
        'job_id': script_plan.get('job_id', 'job'), 'revision': script_plan.get('revision', 1),
        'audio': {'audio_sha256': audio_sha,
                  'alignment_evidence_sha256': None,  # filled by the pipeline from the real evidence file
                  'actual_duration_ticks': duration},
        'script': {'script_sha256': script_plan['script_sha256'], 'sentence_count': len(sentences)},
        'items': [dict(item, audio_sha256=audio_sha) for item in items],
    }
    return validate_named(review, 'subtitle-review-v1')


CHOICE_STATUS = {'asr_error_confirmed': 'asr_error_confirmed', 'speech_error_confirmed': 'speech_error_confirmed',
                 'confirm_omission': 'omission', 'confirm_addition': 'addition', 'confirm_matched': 'matched'}


def apply_decision(review, sentence_id, decision):
    import datetime
    for item in review['items']:
        if item['sentence_id'] != sentence_id:
            continue
        choice = decision.get('choice')
        if choice not in CHOICE_STATUS:
            raise ValueError('SUBTITLE_DECISION_UNKNOWN:' + str(choice))
        if not decision.get('operator_id'):
            raise ValueError('SUBTITLE_DECISION_OPERATOR_REQUIRED')
        if choice == 'speech_error_confirmed' and not (decision.get('notes') or '').strip():
            raise ValueError('SUBTITLE_DECISION_NOTES_REQUIRED')
        # bind the decision to THIS sentence's actual audio (main narration or redub clip)
        binding = item.get('audio_sha256') or review['audio']['audio_sha256']
        item['decision'] = {'choice': choice, 'operator_id': decision['operator_id'],
                            'notes': decision.get('notes', ''),
                            'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            'audio_sha256_at_decision': binding}
        item['status'] = CHOICE_STATUS[choice]
        return review
    raise ValueError('SUBTITLE_SENTENCE_NOT_FOUND:' + str(sentence_id))


def unresolved_items(review, stop_on_speech_error=False):
    unresolved = []
    for item in review['items']:
        status = item['status']
        if status == 'speech_error_confirmed':
            unresolved.append(item['sentence_id'])
            continue
        if status in ('matched', 'asr_error_confirmed'):
            continue
        # omission/addition/uncertain pass only with an explicit operator decision
        if item.get('decision'):
            continue
        unresolved.append(item['sentence_id'])
    if stop_on_speech_error and any(i['status'] == 'speech_error_confirmed' for i in review['items']):
        unresolved.append(REDUB)
    return unresolved


def _char_tick_map(text, words):
    """Map each display-text character to a real word tick via deterministic
    char-level alignment against the ASR word stream. Unmapped characters fall
    back to the nearest mapped neighbour; no time is invented."""
    S = _norm(text)
    kept = [w for w in words if _norm(w['text'])]
    word_norms = [_norm(w['text']) for w in kept]
    A = ''.join(word_norms)
    a_word_index = []
    for i, wn in enumerate(word_norms):
        a_word_index.extend([i] * len(wn))
    char_ticks = [None] * len(S)
    if S and A:
        for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, S, A, autojunk=False).get_opcodes():
            if tag != 'equal':
                continue
            for k in range(a2 - a1):
                w = kept[a_word_index[b1 + k]]
                char_ticks[a1 + k] = (w['start_tick'], w['end_tick'])
    # fill unmapped chars with the nearest mapped neighbour (left first)
    last = None
    for i in range(len(char_ticks)):
        if char_ticks[i] is not None:
            last = char_ticks[i]
        elif last is not None:
            char_ticks[i] = last
    nxt = None
    for i in range(len(char_ticks) - 1, -1, -1):
        if char_ticks[i] is not None:
            nxt = char_ticks[i]
        elif nxt is not None:
            char_ticks[i] = nxt
    # project back to raw text positions (normalized chars appear in order)
    positions = []
    idx = 0
    for ch in text:
        if _norm(ch):
            positions.append(char_ticks[idx] if idx < len(char_ticks) else None)
            idx += 1
        else:
            positions.append(positions[-1] if positions else None)
    return positions


def build_cues(review):
    """Cue chunk boundaries snap to REAL aligned word ticks, never to character
    proportions. When a chunk has no aligned word evidence at all, its boundary
    falls back to the sentence window (whole-sentence cue), not an invented split."""
    cues = []
    for item in review['items']:
        status = item['status']
        window = item.get('window')
        if status in ('matched', 'asr_error_confirmed', 'confirm_matched'):
            text = item['script_text']
        elif status in ('addition', 'omission') and item.get('decision'):
            text = (item['asr_text'] or '')
        else:
            continue
        words = item.get('words') or []
        if not text or not window or not words:
            continue
        ticks = _char_tick_map(text, words)
        # chunk into <=16 char pieces; boundary tick = the word tick at the cut char
        chunks = []
        start = 0
        while start < len(text):
            remaining = text[start:]
            # a tail of punctuation only never becomes its own cue
            if len(remaining) <= 16 or not re.sub(r'[\W_]', '', text[start + 16:]):
                chunks.append((start, len(text)))
                break
            cut = start + 16
            chunks.append((start, cut))
            start = cut
        for order, (a, b) in enumerate(chunks):
            first = ticks[a] if a < len(ticks) else None
            last_char = ticks[b - 1] if b - 1 < len(ticks) else None
            s = first[0] if first else window['in_tick']
            if order + 1 < len(chunks):
                boundary = ticks[b] if b < len(ticks) else None
                e = boundary[0] if boundary else (last_char[1] if last_char else window['out_tick'])
            else:
                # the final chunk carries the sentence window's real tail (trailing
                # particles/punctuation have no normalized text but real audio)
                e = max(last_char[1] if last_char else window['out_tick'], window['out_tick'], s + 48)
            e = max(e, s + 48)
            cues.append({'cue_id': 'cue-%d' % (len(cues) + 1), 'start_tick': s,
                         'end_tick': e, 'text': text[a:b], 'claim_ids': []})
        # clamp monotonic within the sentence
        for i in range(len(cues) - 1, 0, -1):
            if cues[i - 1]['end_tick'] > cues[i]['start_tick']:
                cues[i - 1]['end_tick'] = max(cues[i - 1]['start_tick'] + 48, cues[i]['start_tick'])
    return cues
