"""Reusable script artifacts and explicit Skill provenance for the v2 writing stage."""
import copy
import hashlib
import json

from scheduler.contracts import canonical_hash, validate_named
from scheduler.script_skill import load_skill, validate_script_skill_requirement
from workers.common import atomic_json


def source_hash(job, *, legacy_assets=False):
    # Picture edits / BGM / audio corrections do not silently rewrite the script.
    inputs = {k: job.get(k) for k in ('script_input', 'account', 'account_profile_sha256',
                                     'profile', 'product_documents', 'claims')}
    inputs['intent'] = {k: job.get('intent', {}).get(k) for k in
                        ('brief', 'target_duration_ticks', 'product_terms', 'language')}
    inputs['text_provider'] = job.get('providers', {}).get('text')
    # Read-only compatibility with context receipts written before footage was
    # removed from writing inputs. New receipts always use the decoupled hash.
    if legacy_assets:
        inputs['assets'] = job.get('assets')
    return canonical_hash(inputs)


def validate_legacy_generation(job, parent=None):
    """Legacy v1 has no skill field: retain timeline edits, not implicit writing."""
    if job.get('schema_version') != 'edit-job/v1': return
    if not any(a['mode'] == 'broll' for a in job['assets']): return
    overrides = job['overrides']
    reusable = (parent and all(parent[k] == job[k] for k in ('intent', 'product_documents', 'claims', 'providers'))
                and not overrides['speech_edits']
                and (parent['assets'] == job['assets'] or bool(overrides['shot_edits'])))
    if not reusable:
        raise ValueError('SCRIPT_SKILL_REQUIRED:旧版自动写稿请改用 edit-job/v2 并指定 Skill')


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError('SCRIPT_ARTIFACT_INVALID:' + path.name) from exc


def find_saved_script(root, job, store):
    """Reuse only a real, hash-checked artifact from an equivalent input revision."""
    expected = source_hash(job)
    revision, visited = job['revision'], set()
    while revision and revision not in visited:
        visited.add(revision)
        record = store.get_job(job['job_id'], revision)
        candidate = record['payload'] if record else job if revision == job['revision'] else None
        if candidate is None or source_hash(candidate) != expected:
            return None
        folder = root / 'jobs' / job['job_id'] / ('rev-%04d' % revision)
        plan = _read(folder / 'script-plan.json')
        if plan is not None:
            context = _read(folder / 'script-context.json')
            # Unbound historical artifacts predate context records. Never give
            # that exception to a new, explicitly bound generation.
            if ((context and context.get('source_hash') in {expected, source_hash(candidate, legacy_assets=True)})
                    or (not context and not job['script_input'].get('skill'))):
                validate_named(plan, 'script-plan-v1')
                if (plan['job_id'] != job['job_id'] or plan['revision'] != revision
                        or hashlib.sha256(plan['script_text'].encode('utf-8')).hexdigest() != plan['script_sha256']):
                    raise ValueError('SCRIPT_ARTIFACT_HASH_MISMATCH')
                generation = _read(folder / 'script-generation.json')
                if job['script_input'].get('skill'):
                    if not generation or generation.get('skill', {}).get('snapshot_sha256') != job['script_input']['skill'].get('snapshot_sha256'):
                        raise ValueError('SCRIPT_SKILL_ARTIFACT_MISMATCH')
                plan = copy.deepcopy(plan)
                plan.update(job_id=job['job_id'], revision=job['revision'])
                return {'plan': plan, 'generation': generation}
        revision = candidate.get('parent_revision')
    return None


def prepare_source(root, job, store):
    if job['script_input']['mode'] != 'generated':
        return {'snapshot': None, 'saved': None}
    binding = job['script_input'].get('skill')
    snapshot = load_skill(binding) if binding else None
    saved = find_saved_script(root, job, store)
    if saved is None:
        validate_script_skill_requirement(job)
        if snapshot is None: raise ValueError('SCRIPT_SKILL_SNAPSHOT_MISSING')
    return {'snapshot': snapshot, 'saved': saved}


def build_full_script(job, profile, evidence, library, prepared, work, chat):
    from scheduler.script_plan import (SCRIPT_INPUT_MAX_CHARS, build_script_plan,
                                       clean_generated_script, script_system_prompt)
    saved = prepared['saved']
    if saved is not None:
        plan = saved['plan']
        if saved['generation'] is not None:
            atomic_json(work / 'script-generation.json', saved['generation'])
    else:
        enriched = dict(evidence, generation_note=job['script_input'].get('generation_note'))
        def generate(brief, prompt):
            snapshot = prepared['snapshot']
            if snapshot is None: raise ValueError('SCRIPT_SKILL_REQUIRED')
            system = script_system_prompt(profile, snapshot)
            total_chars = len(system) + len(prompt)
            if total_chars > SCRIPT_INPUT_MAX_CHARS:
                raise ValueError('SCRIPT_INPUT_BUDGET_EXCEEDED:%d>%d;完整规则及资料未截断'
                                 % (total_chars, SCRIPT_INPUT_MAX_CHARS))
            binding = job['script_input']['skill']
            request_hash = canonical_hash({'system': system, 'prompt': prompt, 'skill': binding,
                                           'regeneration_id': job['script_input'].get('regeneration_id')})
            metadata = {k: snapshot[k] for k in ('id', 'version', 'name', 'description')}
            metadata['snapshot_sha256'] = binding['snapshot_sha256']
            atomic_json(work / 'script-generation.json', {
                'skill': metadata, 'system': system, 'prompt': prompt, 'request_hash': request_hash})
            return clean_generated_script(chat('script-full-v2:' + request_hash, system, prompt, max_tokens=4096)[1])
        plan = build_script_plan(job['script_input'], enriched, library, generate=generate,
                                 job_id=job['job_id'], revision=job['revision'], persona=profile['persona'],
                                 rules=profile['prompt_rules']['script'])
    atomic_json(work / 'script-plan.json', plan)
    atomic_json(work / 'script-context.json', {'source_hash': source_hash(job)})
    return plan
