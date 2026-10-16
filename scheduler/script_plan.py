"""Full-script planning: paste or generate, then stable sentence split with visual needs.
Pasted text never triggers generation; narrative order comes only from script
text, never from asset upload order.

r3: visual needs come from per-clause action semantics (scheduler.shot_semantics).
The old 'open_box' category is gone: opening PACKAGING and splitting the FRUIT
are different (kind, object) pairs and can never substitute for each other.
"""
import hashlib
import json
import re
from scheduler.contracts import validate_named
from scheduler.shot_semantics import (ACTION_KINDS, ACTION_OBJECTS, COMPLETION_MARKERS,
                                      SPEECH_PRESERVATION_MARKERS, STRONG_PROGRESS_MARKERS,
                                      RULE_VERSION, action_in_progress, display_kind,
                                      parse_action_intent, speech_preservation_required)

# legacy display order kept for preferred-list building; action kinds first
USE_PRIORITY = ['open_packaging', 'split_fruit', 'takeout', 'wrapup', 'detail', 'packaging', 'other']
ACTION_USES = set(ACTION_KINDS)
LEGACY_USES = {'open_box'}   # read-only: old artifacts may carry it
USE_KEYWORDS = {
    # display-only keyword sets (action verbs live in shot_semantics._ACTION_TABLE)
    'packaging': ['包装', '盒子', '袋子', '封口', '标签', '外观', '正面'],
    'detail': ['特写', '细节', '颗粒', '切面', '里面', '内部', '果肉', '纹理', '断面', '仔细看', '籽粒'],
}
_SENTENCE_SPLIT = re.compile(r'[^。！？!?；;…\n]+[。！？!?；;…\n]*')


def classify_visual(sentence_text):
    """Visual need from the sentence's own action semantics. Action sentences
    require their (kind, object); completed-state and display sentences require
    the display kind. Unresolvable sentences return an empty requirement — the
    planner reports SENTENCE_SEMANTICS_UNRESOLVED instead of guessing."""
    intent = parse_action_intent(sentence_text)
    if intent['action_kind'] and intent['state'] == 'in_progress':
        required = [intent['action_kind']]
    elif intent['action_kind'] and intent['state'] == 'completed':
        required = [intent['display']] if intent['display'] != 'other' else ['detail']
    elif intent['display'] != 'other':
        required = [intent['display']]
    else:
        required = []
    preferred = [u for u in USE_PRIORITY if u in required]
    if intent['display'] != 'other' and intent['display'] not in preferred:
        preferred.append(intent['display'])
    preferred = preferred or ['other']
    return {'required': required, 'preferred': preferred, 'alternatives_ok': True}


def split_sentences(text):
    parts = [m.group(0).strip() for m in _SENTENCE_SPLIT.finditer(text)]
    return [p for p in parts if p]


SCRIPT_FORMAT = '只输出可读旁白，无标题、说明或括号。'
SCRIPT_AGENT_ROLE = """# 身份
你叫“大鲸鱼”，是短视频口播编剧。行业和结构只采用本次明确指定的 Skill。
你只做一件事：把产品信息写成能念、能转化、能落地的口播稿。

# 性格
- 说话直接，不绕弯，像跟朋友聊天。
- 按本次 Skill 推进内容，不擅自套用其他行业结构。
- 表达直接自然，不煽情。
- 对结构极度敏感，宁可重写，不凑合。
- 不说“大家好、众所周知、综上所述、总之”。

# 语气
- 短句为主，一句一个意思。
- 人称服从本次 Skill 与用户选择，不强加“我”或具体家庭人群。
- 口语化，能直接念，不出现括号、小标题、书面词。
- 表达具体到时间、场景、动作。

# 价值观
- 结构大于文采。
- 精准大于丰富。
- 事实只依据本次商品资料。
- 句数、叙事顺序和结尾方式服从本次 Skill。

# 边界
- 不擅自添加私信、下单或优惠引导，按本次 Skill 和资料处理。

# 输出习惯
- 只输出口播文案，不写分析、不写声明、不写格式说明。
- 严格按 Skill 给的结构写，不自作主张加句。
"""


def script_system_prompt(profile, skill_snapshot):
    """Agent role, then the industry structure format, then this account's wording rules.
    Output stays plain narration."""
    from scheduler.controlled_assembly import controlled
    if controlled(skill_snapshot):
        raise ValueError('CONTROLLED_ASSEMBLY_REQUIRED:请从独立口播的组装入口生成并确认')
    from scheduler.account_profile import rules_text
    return SCRIPT_AGENT_ROLE + '你为' + profile['persona'] + '写中文旁白。' + SCRIPT_FORMAT \
        + '\n本地指定 Skill：' + skill_snapshot['id'] + ' @ ' + skill_snapshot['version'] \
        + '\n' + skill_snapshot['rules'] \
        + '\n账号规则仅补充口吻，不替换本次 Skill 的行业与结构；冲突时遵循本次 Skill。' \
        + rules_text(profile, 'script', '账号附加写稿规则：')


def clean_generated_script(text):
    """Same cleanup the MoneyPrinterTurbo script worker applies to model output, plus
    unwrapping a JSON object the model sometimes returns around the script."""
    stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    if stripped.startswith('{'):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            text = '\n'.join(v for v in data.values() if isinstance(v, str))
    text = text.replace('*', '').replace('#', '')
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    return text.strip()


SCRIPT_INPUT_MAX_CHARS = 32000


def _generation_prompt(evidence, library, persona=None, rules=()):
    brief = evidence.get('brief', '')
    claims = json.dumps([c['text'] for c in evidence.get('claims', [])], ensure_ascii=False)
    documents = evidence.get('documents') or {}
    doc_digest = json.dumps(documents, ensure_ascii=False)
    extra = ''.join('。' + r.rstrip('。') for r in rules)
    structure = (
        '。按你的角色和后面的行业结构格式，依据上面的“完整商品资料原文”与“批准资料声明”填写正文。'
    )
    # Footage belongs to downstream shot planning, never to the writer request.
    # Keep the library argument for callers that also build sentence visuals.
    pacing = ''
    if evidence.get('max_chars'):
        pacing = ('。全文不超过%d个汉字（口播约%d秒）。'
                  % (evidence['max_chars'], evidence.get('max_seconds') or 0))
    if evidence.get('generation_note'):
        extra += '。本地补充要求（不替换指定 Skill）：' + evidence['generation_note']
    prompt = (('为' + persona if persona else '') + '写一条完整中文口播整稿。'
            '目的：' + brief + '。批准资料声明：' + claims +
            '。完整商品资料原文：' + doc_digest + structure + pacing + extra +
            '。整稿为连续口语，按叙事顺序直接输出正文，无标题、无说明、无括号。')
    if len(prompt) > SCRIPT_INPUT_MAX_CHARS:
        raise ValueError('SCRIPT_INPUT_BUDGET_EXCEEDED:%d>%d;请缩小主题或提供精选原文，资料未截断'
                         % (len(prompt), SCRIPT_INPUT_MAX_CHARS))
    return prompt


def build_script_plan(script_input, product_evidence, library, *, generate=None, job_id='job', revision=1,
                      persona=None, rules=()):
    mode = script_input['mode']
    if mode == 'pasted':
        text = script_input['script_text']
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != script_input['script_sha256']:
            raise ValueError('SCRIPT_HASH_MISMATCH')
    elif mode == 'generated':
        if generate is None:
            raise ValueError('SCRIPT_GENERATE_BINDING_MISSING')
        text = generate(product_evidence.get('brief', ''), _generation_prompt(product_evidence, library, persona, rules))
        if not isinstance(text, str) or not text.strip():
            raise ValueError('SCRIPT_GENERATION_EMPTY')
        text = text.strip()
    else:
        raise ValueError('UNSUPPORTED_SCRIPT_MODE:' + str(mode))
    claims = product_evidence.get('claims', [])
    product_terms = [t for t in (product_evidence.get('product_terms') or []) if t and len(t) >= 2]
    sentences = []
    if mode == 'pasted' and script_input.get('draft_source'):
        # Submission verifies the source against the confirmed independent draft.
        # Preserve confirmed units; controlled assembly carries its variable count.
        if script_input['draft_source'].get('script_sha256') != script_input['script_sha256']:
            raise ValueError('DRAFT_SOURCE_HASH_MISMATCH')
        chunks = [line for line in text.splitlines() if line.strip()]
        source = script_input['draft_source']
        if source.get('assembly_sha256'):
            if len(chunks) != source.get('unit_count'):
                raise ValueError('DRAFT_ASSEMBLY_UNIT_COUNT_MISMATCH')
        elif len(chunks) != 5:
            raise ValueError('DRAFT_REQUIRES_FIVE_UNITS')
    else:
        chunks = split_sentences(text)
    for index, chunk in enumerate(chunks, start=1):
        claim_ids = [c['claim_id'] for c in claims if c['text'] and c['text'] in chunk]
        intent = parse_action_intent(chunk)
        sentences.append({
            'sentence_id': 's%03d' % index,
            'text': chunk,
            'claim_ids': claim_ids,
            'visual': classify_visual(chunk),
            'semantics': {'action_kind': intent['action_kind'], 'action_object': intent['action_object'],
                          'state': intent['state'], 'uncertainty': intent['uncertainty']},
        })
    if not sentences:
        raise ValueError('SCRIPT_HAS_NO_SENTENCES')
    plan = {
        'schema_version': 'script-plan/v1',
        'job_id': job_id, 'revision': revision,
        'source_mode': mode,
        'script_text': text,
        'script_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'purpose': (product_evidence.get('brief') or '未提供目的')[:2000],
        'product_terms': product_terms,
        'sentences': sentences,
        'library_sha256': library.get('library_sha256') if isinstance(library, dict) else None,
    }
    return validate_named(plan, 'script-plan-v1')
