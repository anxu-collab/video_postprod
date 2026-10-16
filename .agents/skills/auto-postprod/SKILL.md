---
name: auto-postprod
description: Call the local auto post-production engine (video_postprod) from any content project — register an account profile, build an edit-job/v2, submit it, and drive the staged confirmations to a reviewed MP4. Use when a project needs automatic editing of its own footage with its own voice, music and prompt rules.
compatibility: Windows host running the video_postprod scheduler on 127.0.0.1:8760. Needs the operator token (env POSTPROD_OPERATOR_TOKEN or the DPAPI-stored token).
---

# Auto Post-production

The engine is account-agnostic. It owns the generic editing skills; each calling project owns its account profile.

## What the engine always does (not configurable)

- Analyse every imported clip (Narrato vision on doubao), build one shot library.
- Accept a pasted full script or generate one with a locally selected writing Skill. Account fact-check rules are separate; their result is recorded and never stops the edit.
- Synthesize narration on the local Cosy model (CUDA only, loaded per call, unloaded after), align it with whisper, compare it with the script.
- Plan shots, optionally run Clef-Flash suggestions with a Claude Sonnet text review, compile the timeline, mix voice and music, burn subtitles, run technical QA.
- Stop at each human gate: script confirm, narration confirm, subtitle review, missing shots, redub, final review. Nothing is auto-accepted.

## What the account profile supplies

A JSON file kept in the calling project (`postprod-profile/v1`). Relative paths resolve against the profile's own folder.

```json
{
  "schema_version": "postprod-profile/v1",
  "account": "myAccount",
  "display_name": "我的账号",
  "persona": "我的账号",
  "profiles": ["daily"],
  "voice": {"tts_route": "cosy-myaccount", "voice_id": "<Cosy voice id>", "presets": ["<preset>"], "default_preset": "<preset>"},
  "music": {"template": {"template_id": "my-theme", "version": "1.0.0"}, "path": "../assets/music/theme.mp3",
            "sha256": "<sha256 of the file>", "license_ref": "../assets/music/license.json", "gain_db": -32},
  "asset_roots": ["../footage"],
  "prompt_rules": {"script": [], "fact_review": [], "shot_review": []}
}
```

- `persona` is who the narration is written for and who the shot review works for.
- `voice.voice_id` must already exist in the Cosy voice library (F:/AI/Fun-CosyVoice3). A preset name may belong to only one account.
- `music` may be `null`; jobs then need `templates.bgm: null` or a per-job `bgm_override`.
- `asset_roots` are extra folders the engine may import from (footage, provenance, locked evidence, music).
- `prompt_rules` add account rules on top of the engine's built-in guardrails; they never replace them.

## Onboarding a project

1. Write the profile file in the calling project.
2. Register it in `video_postprod/config/account_profiles.json` under `accounts` (path relative to `video_postprod/`).
3. Check it: `GET /v2/accounts` must list the account without an `error` field.

## Running a job

All calls go to `http://127.0.0.1:8760` with `Authorization: Bearer <operator token>`.

1. `GET /v2/job-template?account=<id>` returns a ready edit-job/v2 with that account's voice route, default preset and music template. `GET /v2/script-skills` lists the available writing Skills and their purpose.
2. Fill `job_id`, `intent` (brief, product_terms, target_duration_ticks), `assets` (imported via `POST /v1/imports`), `product_documents`/`claims`, `script_input`, `narration_input`, `cost.paid_enabled`. For `script_input.mode="generated"`, explicitly set `script_input.skill={"id":"...","version":"..."}` from the available list. The account, its `selling` label, profile, or product keywords never select a writing Skill. For `mode="pasted"`, no Skill selection is required and no writer is called.
3. `POST /v2/jobs` with an `Idempotency-Key`. The response carries `account_profile_sha256`: the profile is pinned, and later revisions keep exactly those settings even if the profile file changes. To use new profile settings, create a new job.
4. Poll `GET /v1/jobs/{id}?revision=n` and answer each gate with the matching `/v2/jobs/{id}/...` endpoint (`confirm-script`, `confirm-narration`, `subtitle-decisions`, `shot-replacement`, `whole-clip-replacement`).
5. Final review is human: `POST /v1/jobs/{id}/reviews` per scope. Technical PASS is not content acceptance.

`GET /v2/jobs/{id}/review-data?revision=n` includes `script_generation` when available. Its Skill binding records the name, version, purpose, snapshot SHA-256, and the prompt/request audit fields used for that generation. A missing binding on a historical failed job is not inferred from its account. Select an available Skill and create a new revision with the original `input_hash` in `If-Match` and a fresh `Idempotency-Key`; keep the other payload fields unchanged. Each deliberate rewrite gets a new `script_input.regeneration_id`; retries of the same request reuse that value. Updating a pasted script creates a pasted revision and does not call the writer; when the source revision has a Skill binding, retain it as provenance.

## Boundaries

- The engine never adds fixed corner labels or production notes to the picture; music attribution belongs in the publishing text.
- Generated or stock footage is never presented as real product testing; keep provenance files truthful.
- Preserve historical account snapshots and generated artifacts. Any request that actually generates a new script must explicitly select a Skill. Failed v1 jobs that need a new script must be recreated as a v2 job with a selected Skill; do not add v2 fields to an invalid v1 revision.
