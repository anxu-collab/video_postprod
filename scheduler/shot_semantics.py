# -*- coding: utf-8 -*-
"""Deterministic per-observation/per-clause action semantics (r3, approved by
REDIAGNOSIS_PLAN_REVIEW.md). Zero model calls.

Invariants (the r2 failures this module exists to prevent):
- Every judgement reads ONE observation or ONE sentence clause at a time. A
  merged window keeps the per-observation results and is NEVER re-judged on
  its concatenated text: obs1's "对半切开的" state descriptor plus obs3's
  hand-movement "继续/逐渐" markers must not conjure an opening action.
- A progress marker only counts when it sits in the SAME clause as the target
  verb+object it is supposed to qualify; hand/camera movement never promotes
  a completed product state into an action.
- "切开/掰开 + 的" is a state descriptor ("切开的火龙果"), not a live verb.
- Action windows for auto selection need an ATTESTED RUN: >=2 adjacent
  observation ticks each independently attesting the same (kind, object,
  in_progress), optionally end-bounded by an adjacent tick attesting the
  completion of the same object family. A lone tick is recorded honestly as
  attested_tick and never blesses a span; midpoints/tau are inference and are
  not used to extend action evidence.
"""
import re
import statistics

RULE_VERSION = 'semantic-actions-v1'

ACTION_KINDS = ('open_packaging', 'split_fruit', 'takeout', 'wrapup')
ACTION_OBJECTS = ('packaging', 'fruit', 'flesh', 'product', 'tool', 'unknown')
# the r2 collapsed category; only ever read from legacy artifacts
LEGACY_ACTION_USES = ('open_box',)

# (kind, verbs, object words that must co-occur in the SAME clause, object category)
_ACTION_TABLE = (
    ('open_packaging',
     ('打开', '拆开', '撕开', '开封', '揭开', '开箱', '开盒', '拆封'),
     ('包装', '包装盒', '盒子', '纸盒', '盒', '箱子', '箱', '袋子', '袋', '封口')),
    ('split_fruit',
     ('掰开', '切开', '剖开', '分开', '掰成'),
     ('火龙果', '果实', '两半', '果肉', '水果', '果')),
    ('takeout',
     ('取出', '拿出', '拿出来', '舀出', '倒出', '夹起'),
     ('火龙果', '果实', '果肉', '果', '产品', '实物', '内容物', '商品')),
    ('wrapup',
     ('盖上', '收起', '合上', '放回', '收尾'),
     ('包装', '包装盒', '盒子', '盒', '箱子', '箱', '袋子', '袋', '火龙果', '果实', '果', '产品', '商品')),
)

# object word -> category (longest words first inside each category)
_OBJECT_CATEGORIES = {
    'packaging': ('包装盒', '包装', '盒子', '纸盒', '箱子', '袋子', '封口', '盒', '箱', '袋'),
    'fruit': ('火龙果', '果实', '两半', '水果', '果'),
    'flesh': ('果肉', '切面', '横截面', '断面', '籽粒', '籽'),
    'product': ('产品', '实物', '内容物', '商品'),
    'tool': ('刀刃', '剪刀', '刀片', '刀'),
}
_FRUIT_FAMILY = ('fruit', 'flesh')

STRONG_PROGRESS_MARKERS = ('正在', '开始', '逐渐', '慢慢', '继续', '用力', '向两侧', '一点一点', '一步步')
COMPLETION_MARKERS = ('已经', '已打开', '已掰开', '已切', '完毕', '完成后', '静态', '摆放', '放置',
                      '呈现', '展示', '保持', '没有变化', '无变化', '留在', '静止', '稳定')
NEGATION_MARKERS = ('没有', '未', '不再', '无法', '看不见')
SPEECH_PRESERVATION_MARKERS = ('正在讲话', '正在说话', '开口说话', '讲话介绍', '对着镜头说',
                               '保留原声', '保留原声和嘴型', '口播', '正在介绍')
# words that gesture at an action without saying which: never auto-resolved
_AMBIGUOUS_ACTION_WORDS = ('处理', '操作', '弄', '搞', '安排一下', '来一下')
_CJK = re.compile(r'[\u4e00-\u9fff]')
_CLAUSE_SPLIT = re.compile(r'[^，。！？!?；;、…\n]+')
# verb and its object must sit within this many CJK characters of each other;
# keeps "手掌五指分开…位于白心火龙果的前方" (fingers apart, fruit far away) out
_PROXIMITY_CHARS = 9
# "切开/掰开 + 的" = state modifier, not a live verb
_MODIFIER_SUFFIX = '的'


def _clauses(text):
    return [c for c in (m.group(0).strip() for m in _CLAUSE_SPLIT.finditer(text or '')) if c]


def _cjk_distance(clause, i, j):
    between = clause[min(i, j) + 1:max(i, j)]
    return len(_CJK.findall(between))


def _object_category(word):
    for cat, words in _OBJECT_CATEGORIES.items():
        if word in words:
            return cat
    return 'unknown'


def _clause_actions(clause):
    """All (kind, object, state) bindings a SINGLE clause supports."""
    actions = []
    negated = any(m in clause for m in NEGATION_MARKERS)
    completed = any(m in clause for m in COMPLETION_MARKERS)
    strong = any(m in clause for m in STRONG_PROGRESS_MARKERS)
    for kind, verbs, objects in _ACTION_TABLE:
        for verb in verbs:
            start = 0
            while True:
                i = clause.find(verb, start)
                if i < 0:
                    break
                start = i + len(verb)
                if negated and any(m in clause[max(0, i - 3):i] for m in NEGATION_MARKERS):
                    continue
                modifier = clause[i + len(verb):i + len(verb) + 1] == _MODIFIER_SUFFIX
                best = None
                for obj_word in objects:
                    j = clause.find(obj_word)
                    while j >= 0:
                        dist = _cjk_distance(clause, i + len(verb) - 1, j + len(obj_word) - 1)
                        if dist <= _PROXIMITY_CHARS and (best is None or dist < best[0]):
                            best = (dist, obj_word)
                        j = clause.find(obj_word, j + 1)
                if best is None:
                    continue
                if modifier:
                    state = 'completed'          # "切开的火龙果": result, not action
                elif completed and not strong:
                    state = 'completed'
                elif completed and strong:
                    state = 'completed'          # a strong marker never rescues a completed clause
                else:
                    state = 'in_progress'        # unmarked action clause shows the action itself
                actions.append({'kind': kind, 'object': _object_category(best[1]), 'state': state})
    return actions


def _ambiguous(clause, actions):
    # Multiple actions in one clause resolve deterministically (the strongest
    # kind wins in parse_action_intent); only gesture-words are unresolved.
    return any(w in clause for w in _AMBIGUOUS_ACTION_WORDS)


def _family_words(family):
    words = list(_OBJECT_CATEGORIES.get(family, ()))
    for fam in _FRUIT_FAMILY:
        if family in _FRUIT_FAMILY:
            words += list(_OBJECT_CATEGORIES.get(fam, ()))
    return tuple(dict.fromkeys(words))


def _completion_attests(text, family):
    """The next tick attests the action ended: completion wording plus a
    same-family object word in the SAME text (observed end, not a midpoint)."""
    if not any(m in text for m in COMPLETION_MARKERS):
        return False
    return any(w in text for w in _family_words(family))


def display_kind(text):
    """Display classification for ONE text (never a concatenation)."""
    if any(k in text for k in ('特写', '细节', '颗粒', '切面', '截面', '里面', '内部', '果肉',
                               '纹理', '断面', '仔细看', '籽粒', '籽')):
        return 'detail'
    if any(k in text for k in ('包装', '盒子', '袋子', '封口', '标签', '外观', '正面')):
        return 'packaging'
    return 'other'


def parse_observation(text):
    """Structured parse of ONE observation. Never guesses: ambiguity, negation
    and completion resolve to honest non-actions."""
    actions, ambiguous = [], False
    for clause in _clauses(text):
        ca = _clause_actions(clause)
        if _ambiguous(clause, ca):
            ambiguous = True
        actions.extend(ca)
    return {'actions': actions,
            'display': display_kind(text or ''),
            'speech': speech_preservation_required(text or ''),
            'ambiguous': ambiguous,
            'text': text or ''}


def parse_action_intent(text):
    """Sentence-side intent: same clause rules. uncertainty=True sentences stop
    the planner (SENTENCE_SEMANTICS_UNRESOLVED) instead of being guessed."""
    actions, ambiguous, verb_seen_unbound = [], False, False
    for clause in _clauses(text):
        ca = _clause_actions(clause)
        if _ambiguous(clause, ca):
            ambiguous = True
        if not ca:
            for _kind, verbs, _objs in _ACTION_TABLE:
                for verb in verbs:
                    if verb in clause:
                        verb_seen_unbound = True
        actions.extend(ca)
    primary = None
    if actions:
        order = {k: i for i, k in enumerate(ACTION_KINDS)}
        in_progress = [a for a in actions if a['state'] == 'in_progress']
        pool = in_progress or actions
        primary = sorted(pool, key=lambda a: order.get(a['kind'], 99))[0]
    uncertainty = ambiguous or verb_seen_unbound
    return {'action_kind': primary['kind'] if primary else None,
            'action_object': primary['object'] if primary else None,
            'state': primary['state'] if primary else None,
            'uncertainty': bool((ambiguous or verb_seen_unbound) and not primary),
            'display': display_kind(text or '')}


def action_in_progress(text):
    """Back-compatible boolean: does this SINGLE text show an action happening?
    Completion wording dominates; progress markers only qualify their own clause."""
    return any(a['state'] == 'in_progress' for a in parse_observation(text or '')['actions'])


def speech_preservation_required(text):
    return any(m in (text or '') for m in SPEECH_PRESERVATION_MARKERS)


def sampling_interval(ticks):
    diffs = sorted({b - a for a, b in zip(sorted(ticks), sorted(ticks)[1:]) if b > a})
    if not diffs:
        return 48000
    # the FASTEST observed cadence: a sparse grid must not stretch adjacency so
    # far that a 10s unobserved gap counts as continuous sampling
    return int(min(480000, max(24000, diffs[0])))


def _same_family(a, b):
    if a == b:
        return True
    return a in _FRUIT_FAMILY and b in _FRUIT_FAMILY


def action_runs(ticks, parses):
    """Inferred runs from a source's observation grid — PROPOSALS, never
    validated boundaries (M1 clarification).

    ticks/parses are parallel arrays for ALL observations of one source (sorted
    by tick). Returns entries:
      evidence 'inferred_run'  — >=2 consecutive ticks (gap <= the fastest
                                 observed cadence) each independently attesting
                                 the same (kind, object, in_progress). The span
                                 between first and last tick and EVERY inter-tick
                                 interval is inference, recorded as unverified
                                 gaps. A completion observation only proves the
                                 action ended AT that tick; it never extends a
                                 run's continuity forward.
      evidence 'attested_tick' — a lone attesting tick; span == [tick, tick].
    Neither kind is sufficient for auto window selection; only operator
    CONFIRMED interval evidence is (see confirmed interval store below).
    """
    grid = sorted(zip(ticks, parses), key=lambda tp: tp[0])
    interval = sampling_interval([t for t, _ in grid])
    runs = []
    keys = sorted({(a['kind'], a['object']) for _t, p in grid for a in p['actions']
                   if a['state'] == 'in_progress'})
    for kind, obj in keys:
        attesting = [t for t, p in grid
                     if any(a['kind'] == kind and a['object'] == obj
                            and a['state'] == 'in_progress' for a in p['actions'])]
        groups, current = [], []
        for t in attesting:
            if current and t - current[-1] <= interval:
                current.append(t)
            else:
                if current:
                    groups.append(current)
                current = [t]
        if current:
            groups.append(current)
        for g in groups:
            gaps = [{'from': a, 'to': b, 'ticks': b - a, 'verified': False}
                    for a, b in zip(g, g[1:])]
            if len(g) >= 2:
                runs.append({'kind': kind, 'object': obj, 'evidence': 'inferred_run',
                             'ticks': list(g), 'span': [g[0], g[-1]], 'gaps': gaps,
                             'basis': '相邻观测点各自独立同证 %s/%s 进行中；点间区间未验证（推断），仅作待确认提案'
                                      % (kind, obj)})
            else:
                runs.append({'kind': kind, 'object': obj, 'evidence': 'attested_tick',
                             'ticks': list(g), 'span': [g[0], g[0]], 'gaps': [],
                             'basis': '单观测点；不证明任何跨度，仅作待确认提案'})
    runs.sort(key=lambda r: (r['span'][0], r['kind'], r['object']))
    return runs


# ---- operator-confirmed interval evidence (the ONLY auto-selectable basis) ----

CONFIRMED_STORE_DIR = 'state/confirmed-action-windows'


def _store_path(root, sha):
    from pathlib import Path
    return Path(root) / CONFIRMED_STORE_DIR / sha[:2] / (sha + '.json')


def _atomic_write(path, data):
    import json as _json
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.partial')
    tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(path)


def load_confirmed_intervals(root):
    """Reusable operator-confirmed action intervals, keyed by source hash."""
    import json as _json
    from pathlib import Path
    entries = []
    base = Path(root) / CONFIRMED_STORE_DIR
    if not base.is_dir():
        return entries
    for p in sorted(base.glob('*/*.json')):
        try:
            entries.extend(_json.loads(p.read_text(encoding='utf-8')).get('intervals', []))
        except (ValueError, OSError):
            continue
    return entries


def record_confirmed_interval(root, entry):
    """Persist one confirmed interval under its source hash for reuse while the
    hash matches. The entry carries reviewer, basis and origin — evidence is
    never self-produced by the parser."""
    import json as _json
    p = _store_path(root, entry['source_sha256'])
    data = {'source_sha256': entry['source_sha256'], 'intervals': []}
    if p.is_file():
        try:
            data = _json.loads(p.read_text(encoding='utf-8'))
        except ValueError:
            pass
    data['intervals'] = [i for i in data.get('intervals', [])
                         if not (i.get('action_kind') == entry['action_kind']
                                 and i.get('action_object') == entry['action_object']
                                 and i.get('in_tick') == entry['in_tick']
                                 and i.get('out_tick') == entry['out_tick'])]
    data['intervals'].append(entry)
    _atomic_write(p, data)
    return entry


def clip_runs(runs, in_tick, out_tick):
    """Runs overlapping a window, spans clipped to the window (evidence honest).
    A lone attested_tick (zero-length span) is kept when its tick lies inside
    the window — it must stay visible as insufficient evidence, never dropped."""
    out = []
    for r in runs:
        lo, hi = max(r['span'][0], in_tick), min(r['span'][1], out_tick)
        if r['evidence'] == 'attested_tick':
            if in_tick <= r['span'][0] < out_tick:
                out.append(dict(r))
            continue
        if lo >= hi:
            continue
        entry = dict(r, span=[lo, hi])
        out.append(entry)
    return out


def supported_actions_for(ticks, texts, in_tick, out_tick, interval=None):
    """Convenience for hand-built candidates/tests: per-observation parses plus
    runs clipped to the window."""
    parses = [parse_observation(t) for t in texts]
    runs = action_runs(ticks, parses)
    return clip_runs(runs, in_tick, out_tick)
