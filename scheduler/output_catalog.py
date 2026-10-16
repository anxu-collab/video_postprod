# -*- coding: utf-8 -*-
"""One output folder per account and product, split by human review status.

The job folder under jobs/ stays the immutable render. This catalog is the
place to open the videos: 待审核, 审核, 拒绝.
"""
import os
import re
import shutil
from pathlib import Path

from workers.common import ROOT, atomic_json, digest

STATUSES = ('待审核', '审核', '拒绝')
_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n]+')
# 审核目录按产品定死，不跟着资料文件名走。天益清只使用 tianyiqing。
_PRODUCT_IDS = {'tianyiqing-product': 'tianyiqing'}


def product_of(job):
    pinned = (job.get('catalog_product') or '').strip()
    if pinned:
        return pinned
    docs = job.get('product_documents') or []
    if docs and docs[0].get('path'):
        stem = Path(docs[0]['path']).stem.strip()
        if stem:
            return _PRODUCT_IDS.get(stem, stem)
    return '未命名产品'


def title_of(job):
    brief = ((job.get('intent') or {}).get('brief') or '').strip()
    head = brief.split('：', 1)[0].split(':', 1)[0].strip()
    return head or job.get('job_id') or '未命名'


def catalog_base(job, root=None):
    account = (job.get('account') or 'unknown').strip() or 'unknown'
    return Path(root or ROOT) / 'output' / account / product_of(job)


def _safe(text):
    cleaned = _UNSAFE.sub('', text).strip().rstrip('.')
    return cleaned or '未命名'


def publish(job, source, status, file_sha, root=None):
    """Put this job's current video in exactly one status folder."""
    if status not in STATUSES:
        raise ValueError('OUTPUT_STATUS_UNKNOWN:' + str(status))
    source = Path(source)
    if not source.is_file() or digest(source) != file_sha:
        raise ValueError('OUTPUT_SOURCE_HASH_MISMATCH')
    base = catalog_base(job, root)
    filename = '%s-%s.mp4' % (_safe(title_of(job)), _safe(job['job_id']))
    for name in STATUSES:
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        for path in folder.glob('*-%s.mp4' % _safe(job['job_id'])):
            if name == status and path.name == filename:
                continue
            path.unlink()
            side = path.with_suffix('.json')
            if side.is_file():
                side.unlink()
    dest = base / status / filename
    if dest.exists() and digest(dest) != file_sha:
        dest.unlink()
    if not dest.exists():
        try:
            os.link(source, dest)
        except OSError:
            shutil.copy2(source, dest)
    atomic_json(dest.with_suffix('.json'), {
        'job_id': job['job_id'], 'revision': job.get('revision'),
        'account': job.get('account'), 'product': product_of(job),
        'title': title_of(job), 'status': status,
        'sha256': file_sha, 'source': str(source),
    })
    return dest
