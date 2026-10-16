"""Volcengine Ark text-agent route: tiered model config for the script channel.

Route name `ark-text-agent` is the default text route for new edit-job/v2 tasks.
config/ark-text-agent.json is the source for the script/review model IDs, thinking
settings, token bounds and budget estimates; do not duplicate those values here.
Loading configuration does not verify model availability or current platform prices.

reserve_input_tokens bounds only the budget reservation; max_input_tokens is the
real usage bound. Existing task routes and the paid-call ledger remain unchanged.
"""
import json
from pathlib import Path

from workers.common import ROOT

CONFIG_PATH = ROOT / 'config' / 'ark-text-agent.json'
ROUTE = 'ark-text-agent'
TIERS = ('script', 'review')


def load_config(path=CONFIG_PATH):
    """Load and validate the tier config. Fails closed on any missing field."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version') != 'ark-text-agent/v1':
        raise ValueError('ARK_TEXT_AGENT_CONFIG_VERSION:' + str(data.get('schema_version')))
    tiers = data.get('tiers') or {}
    missing = [t for t in TIERS if t not in tiers]
    if missing:
        raise ValueError('ARK_TEXT_AGENT_TIER_MISSING:' + ','.join(missing))
    required = ('model', 'max_input_tokens', 'max_output_tokens', 'reserve_input_tokens',
                'reasoning_headroom', 'thinking', 'conservative_bound_per_million')
    out = {}
    for name in TIERS:
        tier = dict(tiers[name])
        absent = [f for f in required if f not in tier]
        if absent:
            raise ValueError('ARK_TEXT_AGENT_TIER_FIELD:' + name + '.' + ','.join(absent))
        if not isinstance(tier['model'], str) or not tier['model'].strip():
            raise ValueError('ARK_TEXT_AGENT_MODEL_EMPTY:' + name)
        if not (isinstance(tier['max_input_tokens'], int) and tier['max_input_tokens'] > 0):
            raise ValueError('ARK_TEXT_AGENT_INPUT_BOUND:' + name)
        if not (isinstance(tier['max_output_tokens'], int) and tier['max_output_tokens'] > 0):
            raise ValueError('ARK_TEXT_AGENT_OUTPUT_CAP:' + name)
        if not (isinstance(tier['reserve_input_tokens'], int)
                and 0 < tier['reserve_input_tokens'] <= tier['max_input_tokens']):
            raise ValueError('ARK_TEXT_AGENT_RESERVE_BOUND:' + name)
        if not (isinstance(tier['reasoning_headroom'], int) and tier['reasoning_headroom'] >= 0):
            raise ValueError('ARK_TEXT_AGENT_REASONING_HEADROOM:' + name)
        bound = tier['conservative_bound_per_million']
        if not isinstance(bound, dict) or set(bound) != {'input', 'output'}:
            raise ValueError('ARK_TEXT_AGENT_BOUND:' + name)
        rates = {}
        for key in ('input', 'output'):
            value = bound[key]
            if not (isinstance(value, (int, float, str)) and float(value) > 0):
                raise ValueError('ARK_TEXT_AGENT_RATE:' + name + '.' + key)
            rates[key] = float(value)
        tier['input_rate'] = rates['input']
        tier['output_rate'] = rates['output']
        out[name] = tier
    out['global_authorization'] = data.get('global_authorization') or {}
    return out


def tier_for(model, tiers=None):
    """Return the tier config for an Ark model id, or None when it is not a
    text-agent tier model (the legacy vision route then keeps its own behavior)."""
    tiers = tiers or load_config()
    for name in TIERS:
        if tiers[name]['model'] == model:
            return tiers[name]
    return None
