import contextlib
import copy

import pytest

from scheduler.clef_adapter import ClefDecisionAdapter


class Cache:
    def __init__(self): self.values = {}
    def cache_get(self, key): return copy.deepcopy(self.values.get(key))
    def cache_put(self, key, value): self.values[key] = copy.deepcopy(value)


def request(text='完整资料'):
    return {'state': {'text': text}, 'questions': {'q': {'type': 'choice', 'criteria': {'yes': '适用', 'no': '不适用'}}}}


def response(payload):
    return {'model': 'injected-test', 'answers': {qid: {'type': 'choice', 'choice': next(iter(q['criteria'])), 'scores': {}}
                                               for qid, q in payload['questions'].items()}}


def counted_session(monkeypatch, fail=False):
    events = []
    @contextlib.contextmanager
    def session(self):
        events.append('start')
        try:
            def send(payload):
                if fail: raise TimeoutError('test timeout')
                return response(payload)
            yield send
        finally: events.append('stop')
    monkeypatch.setattr(ClefDecisionAdapter, '_session', session)
    return events


def test_many_cache_is_lazy_and_misses_share_one_session(monkeypatch):
    events = counted_session(monkeypatch)
    cache = Cache()
    adapter = ClefDecisionAdapter(transport=response)
    first = list(adapter.classify_many([request('one'), request('two')], cache))
    assert events == ['start', 'stop']
    assert all(row['response']['answers']['q']['choice'] == 'yes' for row in first)
    events.clear()
    replay = list(ClefDecisionAdapter(transport=response).classify_many([request('one'), request('two')], cache))
    assert events == []
    assert all(row['cached'] for row in replay)


def test_many_timeout_exits_and_oversize_does_not_start(monkeypatch):
    events = counted_session(monkeypatch, fail=True)
    adapter = ClefDecisionAdapter(transport=response)
    rows = list(adapter.classify_many([request('x' * 5000)]))
    assert events == [] and rows[0]['error']
    rows = list(adapter.classify_many([request()]))
    assert events == ['start', 'stop'] and rows[0]['error']


@pytest.mark.parametrize('answer', [
    {'choice': 'unknown'}, {'choice': 'yes', 'scores': {'yes': float('nan'), 'no': 0}},
    {'choice': 'yes', 'probabilities': {'yes': float('inf'), 'no': 0}},
    {'choice': 'yes', 'scores': {'yes': .8, 'no': .8}},
    {'choice': 'yes', 'scores': {'yes': .8}},
    {'choice': 'yes', 'scores': {'yes': True, 'no': False}},
])
def test_classify_refuses_invalid_choices_and_probability_distributions(answer):
    adapter = ClefDecisionAdapter(transport=lambda payload: {'answers': {'q': answer}})
    with pytest.raises((ValueError, RuntimeError)):
        adapter.classify(**request())


def test_decide_full_cache_does_not_enter_model_session(monkeypatch):
    events = counted_session(monkeypatch)
    adapter = ClefDecisionAdapter({'backend': 'mock'})
    cache = Cache()
    sentences = [{'sentence_id': 's', 'text': 'fruit'}]
    pool = [{'candidate_id': 'c', 'selection_eligible': True, 'description': 'fruit'}]
    adapter.decide(sentences, pool, cache)
    events.clear()
    result = adapter.decide(sentences, pool, cache)
    assert result['sentences'][0]['ranked_ids'] == ['c']
    assert events == []


def test_closing_partially_consumed_batch_records_actual_context_exit(monkeypatch):
    events = counted_session(monkeypatch)
    adapter = ClefDecisionAdapter(transport=response)
    stream = adapter.classify_many([request(), request('another')])
    next(stream)
    stream.close()
    assert events == ['start', 'stop']
    assert adapter.last_lifecycle['session_exit_observed'] is True
    assert adapter.last_lifecycle['session_error'] is None


def test_session_exit_failure_does_not_publish_new_cache_entries(monkeypatch):
    @contextlib.contextmanager
    def broken(self):
        yield response
        raise RuntimeError('stop failed')
    monkeypatch.setattr(ClefDecisionAdapter, '_session', broken)
    cache = Cache()
    adapter = ClefDecisionAdapter(transport=response)
    with pytest.raises(RuntimeError, match='stop failed'):
        list(adapter.classify_many([request()], cache))
    assert cache.values == {}

