# -*- coding: utf-8 -*-
from scheduler.output_catalog import publish
from workers.common import digest


def job(title='爸妈款'):
    return {'job_id': 'tyq-beat-01f', 'revision': 1, 'account': 'laozhou',
            'intent': {'brief': title + '：给爸妈买的一直放着。'},
            'product_documents': [{'path': 'config/accounts/laozhou/products/tianyiqing.md'}]}


def test_publish_keeps_one_status_folder(tmp_path):
    source = tmp_path / 'preview.mp4'
    source.write_bytes(b'video-bytes')
    sha = digest(source)
    root = tmp_path / 'catalog'
    pending = publish(job(), source, '待审核', sha, root)
    assert pending.parent.name == '待审核'
    assert pending.name == '爸妈款-tyq-beat-01f.mp4'
    assert digest(pending) == sha
    rejected = publish(job(), source, '拒绝', sha, root)
    assert rejected.parent.name == '拒绝'
    assert not pending.exists()
    accepted = publish(job(), source, '审核', sha, root)
    assert accepted.parent.name == '审核'
    assert not rejected.exists()
    assert list((root / 'output' / 'laozhou' / 'tianyiqing').rglob('*.mp4')) == [accepted]


def test_product_filename_does_not_open_another_folder(tmp_path):
    source = tmp_path / 'preview.mp4'
    source.write_bytes(b'video-bytes')
    payload = job()
    payload['job_id'] = 'tyq-beat-01i-ark'
    payload['product_documents'] = [{'path': 'state/deployment/tianyiqing-product.txt'}]
    pending = publish(payload, source, '待审核', digest(source), tmp_path / 'catalog')
    assert pending.parent.parent.name == 'tianyiqing'
    assert 'tianyiqing-product' not in pending.parts
