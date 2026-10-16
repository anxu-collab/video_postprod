import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

SKILLS = [
    {"id": "health-selling", "version": "1.0.0", "name": "保健品口播", "description": "五句结构"},
    {"id": "health-selling", "version": "2.0.0", "name": "保健品口播新版", "description": "新版结构"},
]
ACCOUNTS = [
    {"account": "laox", "display_name": "老X", "voice_id": "voice-a", "profiles": ["daily"]},
    {"account": "other", "display_name": "另一个账号", "voice_id": "voice-b", "profiles": ["review"]},
]


@pytest.fixture
def ui_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        submitted_jobs = []
        revision_jobs = []
        state = {"skills": SKILLS, "skill_error": False, "listed_jobs": [], "job_detail": None,
                 "review_data": {}}

        def route_request(route):
            request = route.request
            url = request.url
            if url.endswith("/") or url.endswith("/index.html"):
                route.fulfill(status=200, content_type="text/html", body=INDEX)
            elif url.endswith("/ui/style.css"):
                route.fulfill(status=200, content_type="text/css", body="")
            elif url.endswith("/ui/app.js"):
                route.fulfill(status=200, content_type="application/javascript", body=APP)
            elif "/v2/script-skills" in url:
                if state["skill_error"]:
                    route.fulfill(status=503, json={"detail": "skill registry unavailable"})
                else:
                    route.fulfill(status=200, json=state["skills"])
            elif "/v2/accounts" in url:
                route.fulfill(status=200, json=ACCOUNTS)
            elif request.method == "POST" and "/revisions" in url:
                revision_jobs.append(request.post_data_json)
                route.fulfill(status=200, json={"job_id": "edit-test", "revision": 2, "state": "READY"})
            elif "/review-data" in url:
                route.fulfill(status=200, json=state["review_data"])
            elif url.endswith("/v1/jobs/edit-test?revision=1"):
                route.fulfill(status=200, json=state["job_detail"])
            elif url.endswith("/v1/jobs"):
                route.fulfill(status=200, json=state["listed_jobs"])
            elif "/v1/capabilities" in url:
                route.fulfill(status=200, json={"paid_budget": {"requests": 0, "held_micros": 0}})
            elif "/v2/job-template" in url:
                route.fulfill(status=200, json={
                    "schema_version": "edit-job/v2",
                    "job_id": "", "revision": 1, "parent_revision": None,
                    "profile": "daily",
                    "intent": {"brief": "", "product_terms": [], "selection_mode": "whole_clip"},
                    "assets": [],
                    "script_input": {"mode": "pasted", "script_text": None, "script_sha256": None},
                    "narration_input": {"mode": "auto", "preset": "default"},
                    "cost": {"paid_enabled": False},
                })
            elif "/v1/imports?filename=" in url:
                route.fulfill(status=200, json={
                    "path": "C:/tmp/fake-clip.mp4", "sha256": "a" * 64,
                    "probe": {"duration_ticks": 48000, "stream_indices": {"audio": 1}},
                    "provenance_ref": "fake-provenance", "boundary_ref": "fake-boundary",
                })
            elif request.method == "POST" and "/v2/jobs" in url:
                submitted_jobs.append(request.post_data_json)
                route.fulfill(status=200, json={"job_id": "edit-test", "revision": 1, "state": "READY"})
            else:
                route.fulfill(status=200, json={})

        page.route("**/*", route_request)
        page.goto("http://127.0.0.1:8760/")
        page.wait_for_function("document.querySelectorAll('#account option').length > 0")
        page.wait_for_function("document.querySelector('#scriptSkill option[value*=version]') !== null")
        yield page, submitted_jobs, state, revision_jobs
        browser.close()


def add_fake_clip(page):
    page.locator("#files").set_input_files({
        "name": "clip.mp4", "mimeType": "video/mp4", "buffer": b"fake media bytes",
    })
    page.locator(".asset").wait_for()


def test_generated_submission_requires_explicit_skill(ui_page):
    page, submitted_jobs, _, _ = ui_page
    add_fake_clip(page)
    page.locator('input[name="scriptmode"][value="generated"]').check()
    page.locator("#submit").click()

    assert "先明确选择本次口播 Skill" in page.locator("#status").inner_text()
    assert submitted_jobs == []


def test_same_skill_id_selects_exact_version_and_survives_account_change(ui_page):
    page, submitted_jobs, _, _ = ui_page
    add_fake_clip(page)
    page.locator('input[name="scriptmode"][value="generated"]').check()
    page.locator("#scriptSkill").select_option(label="保健品口播新版 · 2.0.0")
    page.locator("#account").select_option("other")
    assert page.locator("#scriptSkill").input_value() == json.dumps(
        {"id": "health-selling", "version": "2.0.0"}, separators=(",", ":")
    )
    page.locator("#submit").click()
    page.wait_for_timeout(100)

    assert len(submitted_jobs) == 1, page.locator("#status").inner_text()
    assert submitted_jobs[0]["script_input"]["skill"] == {"id": "health-selling", "version": "2.0.0"}


def test_pasted_script_does_not_require_skill(ui_page):
    page, submitted_jobs, _, _ = ui_page
    add_fake_clip(page)
    page.locator("#script").fill("这是一段人工提供的口播稿。")
    page.locator("#submit").click()
    page.wait_for_timeout(100)

    assert len(submitted_jobs) == 1, page.locator("#status").inner_text()
    assert submitted_jobs[0]["script_input"]["mode"] == "pasted"
    assert "skill" not in submitted_jobs[0]["script_input"]


def test_unavailable_skill_registry_is_explicit(ui_page):
    page, _, state, _ = ui_page
    state["skills"] = []
    page.reload()

    assert "当前没有可用口播 Skill" in page.locator("#scriptSkill").inner_text()
    page.locator('input[name="scriptmode"][value="generated"]').check()
    assert "当前没有可用口播 Skill" in page.locator("#scriptGenerationSummary").inner_text()


def test_failed_skill_registry_load_is_explicit(ui_page):
    page, _, state, _ = ui_page
    state["skill_error"] = True
    page.reload()

    assert "Skill 列表加载失败" in page.locator("#scriptSkill").inner_text()
    page.locator('input[name="scriptmode"][value="generated"]').check()
    assert "加载失败" in page.locator("#scriptGenerationSummary").inner_text()


@pytest.mark.parametrize(
    ("selected_label", "expected_version"),
    [("保健品口播 · 1.0.0", "1.0.0"), ("保健品口播新版 · 2.0.0", "2.0.0")],
)
def test_regenerating_with_selected_skill_creates_fresh_revision(ui_page, selected_label, expected_version):
    page, _, state, revision_jobs = ui_page
    payload = {
        "schema_version": "edit-job/v2", "job_id": "edit-test", "revision": 1,
        "parent_revision": None,
        "intent": {"brief": "sample", "product_terms": [], "selection_mode": "whole_clip"},
        "assets": [],
        "script_input": {"mode": "generated", "script_text": None, "skill": {"id": "health-selling", "version": "1.0.0"}},
        "narration_input": {"mode": "auto", "preset": "default"}, "cost": {"paid_enabled": False},
    }
    state["listed_jobs"] = [{"job_id": "edit-test", "revision": 1, "state": "AWAIT_SCRIPT_CONFIRM"}]
    state["job_detail"] = {
        "job_id": "edit-test", "revision": 1, "state": "AWAIT_SCRIPT_CONFIRM", "input_hash": "old-hash",
        "payload": payload, "nodes": [], "result": None, "errors": {},
    }
    state["review_data"] = {
        "script_plan": {"script_text": "待确认文本。", "sentences": [{"sentence_id": "s1"}]},
        "script_generation": {"skill": {"id": "health-selling", "version": "1.0.0", "name": "保健品口播", "description": "五句结构"}},
    }
    page.reload()
    page.get_by_role("button", name="查看").click()
    page.locator("#revisionScriptSkill").select_option(label=selected_label)
    page.get_by_role("button", name="按所选 Skill 重新写稿").click()
    page.wait_for_timeout(100)

    assert len(revision_jobs) == 1
    revision = revision_jobs[0]
    assert revision["revision"] == 2
    assert revision["parent_revision"] == 1
    assert revision["script_input"]["skill"] == {"id": "health-selling", "version": expected_version}
    assert revision["script_input"]["regeneration_id"]


def test_legacy_v1_failure_does_not_offer_invalid_revision(ui_page):
    page, _, state, revision_jobs = ui_page
    payload = {"schema_version": "edit-job/v1", "job_id": "edit-test", "revision": 1,
               "script_input": {"mode": "generated"}}
    state["listed_jobs"] = [{"job_id": "edit-test", "revision": 1, "state": "FAILED_FINAL"}]
    state["job_detail"] = {
        "job_id": "edit-test", "revision": 1, "state": "FAILED_FINAL", "input_hash": "old-hash",
        "payload": payload, "nodes": [], "result": None,
        "errors": {"code": "SCRIPT_SKILL_REQUIRED", "message": "SCRIPT_SKILL_REQUIRED"},
    }
    page.reload()
    page.get_by_role("button", name="查看").click()
    page.locator("#stagePanel").get_by_text("旧版任务重新写稿请在", exact=False).wait_for()

    assert "请在“新建视频”中明确选择 Skill，并使用新版任务" in page.locator("#stagePanel").inner_text()
    assert page.locator("#legacyScriptSkill").count() == 0
    assert revision_jobs == []
