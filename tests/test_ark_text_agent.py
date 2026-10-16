import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fake_store(payload):
    class Store:
        def get_job(self, job_id, revision=1):
            return {'payload': payload}
    return Store()


def test_config_loads_both_tiers_and_validates():
    from providers.ark_text_agent import load_config, tier_for
    tiers = load_config()
    assert {'script', 'review'} <= set(tiers)
    assert set(tiers) - {'script', 'review', 'global_authorization'} == set()
    assert tiers['global_authorization'] == {'total_micros': 20000000, 'requests': 200}
    script, review = tiers['script'], tiers['review']
    assert script['model'] == 'deepseek-v4-1-flash-260910' and script['thinking'] is True
    assert review['model'] == 'doubao-seed-2-1-lite-260915' and review['thinking'] is False
    assert 0 < script['reserve_input_tokens'] <= script['max_input_tokens']
    assert tier_for('doubao-seed-2-0-lite-260428') is None
    assert tier_for('not-a-model') is None


def test_config_fails_closed_on_broken_tier(tmp_path):
    from providers.ark_text_agent import load_config
    broken = tmp_path / 'broken.json'
    broken.write_text(json.dumps({'schema_version': 'ark-text-agent/v1', 'tiers': {'script': {'model': 'x'}}}), encoding='utf-8')
    with pytest.raises(ValueError):
        load_config(broken)
    bad_reserve = tmp_path / 'reserve.json'
    broken_tiers = {
        'schema_version': 'ark-text-agent/v1',
        'tiers': {
            'script': {'model': 'm', 'max_input_tokens': 100, 'max_output_tokens': 10, 'reserve_input_tokens': 999,
                       'reasoning_headroom': 0, 'thinking': True, 'conservative_bound_per_million': {'input': '1', 'output': '2'}},
            'review': {'model': 'r', 'max_input_tokens': 100, 'max_output_tokens': 10, 'reserve_input_tokens': 1,
                       'reasoning_headroom': 0, 'thinking': False, 'conservative_bound_per_million': {'input': '1', 'output': '2'}},
        },
    }
    bad_reserve.write_text(json.dumps(broken_tiers), encoding='utf-8')
    with pytest.raises(ValueError):
        load_config(bad_reserve)


def test_route_model_ark_text_agent_tiers():
    from providers.gateway import Gateway
    from providers.ark_text_agent import load_config
    tiers = load_config()
    payload = {'providers': {'text': 'ark-text-agent'}}
    g = Gateway(_fake_store(payload))
    assert g.route_model('job', 'vision-v1:abc') == g.model
    assert g.route_model('job', 'script-full-v2:abc') == tiers['script']['model']
    assert g.route_model('job', 'script-short-v1:abc') == tiers['script']['model']
    assert g.route_model('job', 'script-review:abc') == tiers['review']['model']
    assert g.route_model('job', 'clef-review:abc') == tiers['review']['model']
    assert g.route_model('job', 'locked-review:abc') == tiers['review']['model']


def test_route_model_legacy_routes_unchanged():
    from providers.gateway import Gateway, OPUS, SONNET
    from providers.claude_cli import MODELS
    assert OPUS in MODELS and SONNET in MODELS
    g = Gateway(_fake_store({'providers': {'text': 'claude-cli'}}))
    assert g.route_model('job', 'script-full-v2:abc') == OPUS
    assert g.route_model('job', 'clef-review:abc') == SONNET
    g2 = Gateway(_fake_store({'providers': {'text': 'ark-approved'}}))
    with pytest.raises(ValueError):
        g2.route_model('job', 'clef-review:abc')


def test_request_script_tier_caps_and_thinking(tmp_path):
    from scheduler.db import Store
    from providers.gateway import Gateway
    from providers.ark_text_agent import load_config
    seen = {}

    def transport(payload):
        seen['payload'] = payload
        return {'id': 't1', 'choices': [{'message': {'content': '稿'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}

    g = Gateway(Store(tmp_path / 'db.sqlite'), transport=transport)
    model = load_config()['script']['model']
    token = g.issue('deployment-qualification', 'script-full-v2:abc', model=model)
    result = g.request(token, {'model': model, 'messages': [{'role': 'user', 'content': '写'}]})
    assert seen['payload']['max_tokens'] == load_config()['script']['max_output_tokens']
    assert seen['payload']['thinking'] == {'type': 'enabled'}
    assert result['choices'][0]['message']['content'] == '稿'
    assert g.ledger.snapshot()['requests'] == 1


def test_request_review_tier_disables_thinking(tmp_path):
    from scheduler.db import Store
    from providers.gateway import Gateway
    from providers.ark_text_agent import load_config
    seen = {}

    def transport(payload):
        seen['payload'] = payload
        return {'id': 't2', 'choices': [{'message': {'content': '{"grounded":true}'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}

    g = Gateway(Store(tmp_path / 'db.sqlite'), transport=transport)
    model = load_config()['review']['model']
    token = g.issue('deployment-qualification', 'clef-review:abc', model=model)
    result = g.request(token, {'model': model, 'messages': [{'role': 'user', 'content': '审'}]})
    assert seen['payload']['max_tokens'] == 2048
    assert seen['payload']['thinking'] == {'type': 'disabled'}
    assert result['choices'][0]['message']['content'].startswith('{')
    assert g.ledger.snapshot()['requests'] == 1


def test_request_script_tier_allows_reasoning_overrun(tmp_path):
    from scheduler.db import Store
    from providers.gateway import Gateway
    from providers.ark_text_agent import load_config

    def transport(payload):
        return {'id': 't1b', 'choices': [{'message': {'content': '稿'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 5000}}

    g = Gateway(Store(tmp_path / 'db.sqlite'), transport=transport)
    model = load_config()['script']['model']
    token = g.issue('deployment-qualification', 'script-full-v2:abc', model=model)
    result = g.request(token, {'model': model, 'messages': [{'role': 'user', 'content': '写'}]})
    assert result['choices'][0]['message']['content'] == '稿'
    assert g.ledger.snapshot()['requests'] == 1


def test_request_usage_bound_uses_tier_max(tmp_path):
    from scheduler.db import Store
    from providers.gateway import Gateway
    from providers.ark_text_agent import load_config

    def transport(payload):
        return {'id': 't3', 'choices': [{'message': {'content': 'x'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens':
                          load_config()['script']['max_output_tokens'] + load_config()['script'].get('reasoning_headroom', 0) + 1}}

    g = Gateway(Store(tmp_path / 'db.sqlite'), transport=transport)
    model = load_config()['script']['model']
    token = g.issue('deployment-qualification', 'script-full-v2:abc', model=model)
    with pytest.raises(ValueError):
        g.request(token, {'model': model, 'messages': [{'role': 'user', 'content': '写'}]})


def test_service_accepts_route_and_defaults_new_jobs_to_it(tmp_path):
    from scheduler.service import create_app, TEXT_ROUTES
    from scheduler.db import Store
    from providers.gateway import Gateway
    assert 'ark-text-agent' in TEXT_ROUTES
    store = Store(tmp_path / 'db.sqlite')
    app = create_app(store, Gateway(store), 'operator-test', run_queue=False)

    import asyncio
    import types

    def call(method, path, query, payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else b''
        messages = []

        async def run():
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return {'type': 'http.disconnect'}

            async def send(message):
                messages.append(message)

            h = {'content-type': 'application/json', **(headers or {})}
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                       'scheme': 'http', 'method': method, 'path': path, 'raw_path': path.encode(),
                       'query_string': query, 'root_path': '', 'client': ('127.0.0.1', 1234),
                       'server': ('127.0.0.1', 8760),
                       'headers': [(k.lower().encode(), v.encode()) for k, v in h.items()]},
                      receive, send)

        asyncio.run(run())
        data = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        return types.SimpleNamespace(status_code=status, json=lambda: json.loads(data))

    h = {'Authorization': 'Bearer operator-test'}
    template = call('GET', '/v2/job-template', b'account=laozhou', headers=h)
    assert template.status_code == 200
    payload = template.json()
    assert payload['providers']['text'] == 'ark-text-agent'


def test_script_agent_role_in_system_prompt():
    from scheduler.script_plan import script_system_prompt, SCRIPT_AGENT_ROLE
    profile = {'persona': '老周', 'prompt_rules': {'script': ['规则一'], 'shot_review': [], 'fact_review': []}}
    prompt = script_system_prompt(profile, {'id': 'test-explicit', 'version': '1.0.0', 'rules': '按实际动作介绍。'})
    assert SCRIPT_AGENT_ROLE in prompt and '老周' in prompt and '规则一' in prompt
