"""Real Chromium extension IPC and native browser behavior; no real application is sent."""
import base64
import hashlib
import os
from pathlib import Path
import time

import pytest
from playwright.sync_api import sync_playwright, expect

from test_extension_assistance import assistance as assistance  # shared isolated API fixture

pytestmark = [pytest.mark.integration, pytest.mark.slow]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def extension(tmp_path):
    stage = ROOT / "apps/chrome-extension"
    assert (stage / "dist/content.js").is_file(), "Build the extension before browser tests"
    with sync_playwright() as pw:
        executable = Path(pw.chromium.executable_path)
        if not executable.is_file() and os.name == "nt":
            executable = next(iter(sorted((Path(os.environ["LOCALAPPDATA"]) / "ms-playwright").glob(
                "chromium-*/chrome-win64/chrome.exe"), reverse=True)), executable)
        if not executable.is_file():
            pytest.skip("Install Playwright Chromium to run extension browser integration tests")
        browser = pw.chromium.launch_persistent_context(str(tmp_path / "chrome"),
            headless=True, executable_path=str(executable), viewport={"width": 430, "height": 920},
            args=[f"--disable-extensions-except={stage}", f"--load-extension={stage}"],
            ignore_default_args=["--disable-extensions"])
        worker = browser.service_workers[0] if browser.service_workers else browser.wait_for_event("serviceworker")
        extension_id = worker.url.split("/")[2]
        control = browser.new_page()
        control.goto(f"chrome-extension://{extension_id}/sidepanel.html")
        def make_page(html, url="http://localhost:8001/apply/test"):
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
            page.goto(url)
            tab_id = control.evaluate("async url => (await chrome.tabs.query({})).find(t=>t.url===url).id", url)
            control.evaluate("id => chrome.scripting.executeScript({target:{tabId:id},files:['dist/content.js']})", tab_id)
            return page, tab_id
        def message(tab_id, kind, **kwargs):
            return control.evaluate("async v => await chrome.tabs.sendMessage(v.id,v.message)",
                {"id": tab_id, "message": {"type": f"jobcockpit:{kind}", **kwargs}})
        yield browser, control, extension_id, make_page, message
        browser.close()


def plan(question, answer, **kwargs):
    return {"question": question, "answer": answer, "replace_existing": False,
        "expires_at": int(time.time()) + 300, **kwargs}


def test_native_labels_visibility_exact_options_and_readback(extension):
    _, _, _, page_for, message = extension
    page, tab = page_for('''<form><fieldset><legend>Contact</legend>
      <label>First name<input id="first"></label><span id="mail-label">Email</span>
      <input id="mail" aria-labelledby="mail-label" type="email">
      <label>Country<select id="country"><option value="">Choose</option><option value="outside">Outside Canada</option></select></label>
      <label>Graduation date<input id="date" type="date"></label>
      <label style="display:none">Hidden<input id="hidden"></label>
      <div style="opacity:0"><label>Invisible<input id="invisible"></label></div>
      <label>School<input id="school" role="combobox" aria-autocomplete="list"></label>
      </fieldset><button>Submit application</button></form>
      <script>window.submits=0;document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();window.submits++})</script>''')
    questions = message(tab, "scan")["questions"]
    assert [q["text"] for q in questions] == ["First name", "Email", "Country", "Graduation date", "School"]
    assert not questions[-1]["fillable"]
    page.evaluate("""() => { const field=document.querySelector('#first');let tracker='';
      Object.defineProperty(field,'value',{get(){return HTMLInputElement.prototype.__lookupGetter__('value').call(this)},set(v){tracker=v;HTMLInputElement.prototype.__lookupSetter__('value').call(this,v)}});
      field.addEventListener('input',()=>window.frameworkSawChange=tracker!==field.value); }""")
    result = message(tab, "fill", plans=[plan(questions[0], "Example"), plan(questions[1], "test" + "@example.invalid")])
    assert [r["status"] for r in result["results"]] == ["filled", "filled"]
    assert page.evaluate("window.frameworkSawChange") is True
    assert page.evaluate("window.submits") == 0
    # Following a page observes structure without replacing its scan or undo record.
    signature = message(tab, "form-state")["form_signature"]
    page.locator("#mail").fill("manual" + "@example.invalid")
    assert message(tab, "form-state")["form_signature"] == signature
    mismatch = message(tab, "fill", plans=[plan(questions[2], "Canada")])["results"][0]
    assert mismatch["status"] == "failed" and page.locator("#country").input_value() == ""
    invalid_date = message(tab, "fill", plans=[plan(questions[3], "May 2027")])["results"][0]
    assert invalid_date["status"] == "failed" and page.locator("#date").input_value() == ""
    page.evaluate("document.querySelector('button').click()")
    assert page.evaluate("window.submits") == 0
    page.get_by_role("button", name="Submit application").click()
    assert page.evaluate("window.submits") == 1


def test_stale_nodes_existing_values_and_undo(extension):
    _, _, _, page_for, message = extension
    page, tab = page_for('<label>Email<input id="field"></label><label>City<input id="city"></label>')
    questions = message(tab, "scan")["questions"]
    page.evaluate("document.querySelector('#field').outerHTML='<input id=field>'")
    result = message(tab, "fill", plans=[plan(questions[0], "Example"), plan(questions[1], "Test City")])
    assert [r["status"] for r in result["results"]] == ["stale", "skipped"]
    questions = message(tab, "scan")["questions"]
    page.locator("#field").fill("My own answer")
    assert message(tab, "fill", plans=[plan(questions[0], "Example")])["results"][0]["status"] == "stale"
    assert page.locator("#field").input_value() == "My own answer"
    assert message(tab, "fill", plans=[plan(questions[1], "Test City")])["results"][0]["status"] == "filled"
    assert message(tab, "undo")["results"][0]["status"] == "undone"
    assert page.locator("#city").input_value() == ""


def test_workday_labels_radio_shadow_frames_and_option_changes(extension):
    _, _, _, page_for, message = extension
    page, tab = page_for('''<div data-automation-id="formField-firstName">
      <label data-automation-id="formLabel">First name</label><input id="first"></div>
      <fieldset><legend>Preferred contact method</legend><label>Email<input name="contact" type="radio" value="email"></label>
      <label>Phone<input name="contact" type="radio" value="phone"></label></fieldset>
      <div id="shadow"></div><iframe srcdoc="<label>Nested<input></label>"></iframe>
      <script>document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<label>City<input id="city"></label>'</script>''')
    snapshot = message(tab, "scan")
    assert any("frames" in warning for warning in snapshot["warnings"])
    assert [q["text"] for q in snapshot["questions"]] == ["First name", "Preferred contact method", "City"]
    radio = snapshot["questions"][1]
    assert message(tab, "fill", plans=[plan(radio, "phone")])["results"][0]["status"] == "filled"
    assert page.locator('input[value="phone"]').is_checked()
    assert message(tab, "undo")["results"][0]["status"] == "skipped"
    page.locator('input[value="phone"]').evaluate("e=>e.value='different'")
    assert message(tab, "fill", plans=[plan(radio, "phone")])["results"][0]["status"] == "stale"


def test_react_controlled_text_checkbox_and_radio_state(extension):
    _, _, _, page_for, message = extension
    page, tab = page_for('<div id="root"></div>')
    react_dom = (ROOT / "apps/web/node_modules/react-dom").resolve()
    modules = {
        "react": ROOT / "apps/web/node_modules/react/cjs/react.development.js",
        "react-dom": react_dom / "cjs/react-dom.development.js",
        "react-dom/client": react_dom / "cjs/react-dom-client.development.js",
        "scheduler": react_dom.parent / "scheduler/cjs/scheduler.development.js",
    }
    page.evaluate("""sources => {
      const cache={}; const process={env:{NODE_ENV:'development'}};
      const require=name=>{if(!cache[name]){const module={exports:{}};cache[name]=module;
        new Function('module','exports','require','process',sources[name])(module,module.exports,require,process)}return cache[name].exports};
      const React=require('react'), create=React.createElement;
      function App(){const [name,setName]=React.useState('');const [checked,setChecked]=React.useState(false);const [contact,setContact]=React.useState('');
        window.reactValues={name,checked,contact};return create('form',{},
          create('label',{},'First name',create('input',{value:name,onChange:e=>setName(e.target.value)})),
          create('label',{},'Send job updates',create('input',{type:'checkbox',checked,onChange:e=>setChecked(e.target.checked)})),
          create('fieldset',{},create('legend',{},'Preferred contact method'),...['email','phone'].map(value=>create('label',{key:value},value,create('input',{type:'radio',name:'contact',value,checked:contact===value,onChange:e=>setContact(e.target.value)})))))
      }require('react-dom/client').createRoot(document.querySelector('#root')).render(create(App));
    }""", {name: file.read_text(encoding="utf-8") for name, file in modules.items()})
    expect(page.locator("input")).to_have_count(4)
    questions = message(tab, "scan")["questions"]
    result = message(tab, "fill", plans=[plan(questions[0], "Example"),
        plan(questions[1], "true", replace_existing=True), plan(questions[2], "phone")])
    assert [r["status"] for r in result["results"]] == ["filled"] * 3
    assert page.evaluate("window.reactValues") == {"name": "Example", "checked": True, "contact": "phone"}
    assert [r["status"] for r in message(tab, "undo")["results"]] == ["undone", "undone", "skipped"]
    assert page.evaluate("window.reactValues") == {"name": "", "checked": False, "contact": "phone"}


def test_capture_excludes_editable_secrets_and_unknown_site_is_copy_only(extension):
    _, _, _, page_for, message = extension
    page, tab = page_for('''<h1>Synthetic internship</h1><p>Build local software.</p>
      <input value="PRIVATE_FIELD"><textarea>PRIVATE_ANSWER</textarea>
      <form><input type="password" value="PRIVATE_CREDENTIAL"><p>PRIVATE_ACCOUNT</p></form>
      <script>const hiddenSecret='PRIVATE_SCRIPT'</script>''', url="http://localhost:8001/jobs/posting")
    snapshot = message(tab, "capture-page")
    assert "Synthetic internship" in snapshot["visible_text"]
    assert "PRIVATE_" not in snapshot["visible_text"] and "PRIVATE_" not in snapshot["html"]
    assert not message(tab, "scan")["fill_supported"]


def test_completed_answers_exclude_defaults_own_fills_and_changed_questions(extension):
    _, _, _, page_for, message = extension
    page, tab = page_for('''<label>First name<input id="first"></label>
      <label>Python experience<textarea id="experience"></textarea></label>
      <label>Send job updates<input id="updates" type="checkbox"></label>
      <label>Social insurance number<input id="secret"></label>
      <label id="degree-label">Degree<input id="degree"></label>''')
    questions = message(tab, "scan")["questions"]
    assert message(tab, "fill", plans=[plan(questions[0], "Example")])["results"][0]["status"] == "filled"
    page.locator("#experience").fill("Two years of Python")
    page.locator("#secret").fill("PRIVATE_CREDENTIAL")
    page.locator("#degree").fill("Computer science")
    page.locator("#degree-label").evaluate("e => e.firstChild.textContent='Unrelated changed question'")
    result = message(tab, "completed-answers")
    assert [o["answer"] for o in result["observations"]] == ["Two years of Python"]
    assert result["warnings"]
    page.locator("#degree").fill("")
    message(tab, "scan")
    assert [o["answer"] for o in message(tab, "completed-answers")["observations"]] == ["Two years of Python"]
    page.locator("#first").fill("My correction")
    page.locator("#updates").check()
    page.locator("#updates").uncheck()
    result = message(tab, "completed-answers")
    assert {o["answer"] for o in result["observations"]} == {"My correction", "Two years of Python", "false"}


def test_sidepanel_learns_selected_answers_confirms_corrects_and_deletes(extension, assistance, monkeypatch):
    client, _ = assistance
    browser, control, _, page_for, _ = extension
    def api_route(route):
        request = route.request
        response = client.request(request.method, request.url.replace("http://localhost:8000", ""),
            content=request.post_data, headers={"content-type": "application/json"})
        route.fulfill(status=response.status_code, body=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json")})
    browser.route("http://localhost:8000/api/**", api_route)
    def propose(settings, observations, *args):
        assert len(observations) == 1
        assert observations[0]["answer"] == "Three years of Python"  # Panel correction overrides page.
        return [{"observation_id": observations[0]["question_id"], "subject_key": "python_experience",
            "value": "Three years", "statement": "I have three years of Python experience.",
            "scope": "general", "country": "", "term": "", "year": None,
            "rationale": "Applicant's selected answer", "missing_information": []}]
    monkeypatch.setattr("jobcockpit.extension.memory.propose_facts", propose)
    page, _ = page_for('<label>Python experience<textarea id="experience"></textarea></label>')
    control.reload()
    expect(control.locator("#jobSelect option[value='job-one']")).to_have_count(1)
    control.locator("#jobSelect").select_option("job-one")
    expect(control.locator("#jobSummary")).to_contain_text("Software intern")
    control.locator("#jobConfirmed").check()
    control.locator("#scan").click()
    expect(control.locator("#questions article")).to_have_count(1)
    page.locator("#experience").fill("Two years of Python")
    control.get_by_label("Answer: Python experience").fill("Three years of Python")
    control.get_by_role("button", name="Approve", exact=True).click()
    control.locator("#learnAnswers").click()
    expect(control.locator("#observedAnswers input")).to_have_count(1)
    expect(control.locator("#observedAnswers input")).not_to_be_checked()
    control.locator("#observedAnswers input").check()
    control.locator("#suggestFacts").click()
    expect(control.get_by_role("button", name="Confirm and remember")).to_be_visible(timeout=10000)
    assert client.get("/api/extension/memory/facts").json() == []
    control.get_by_role("button", name="Confirm and remember").click()
    expect(control.locator("#savedFacts article")).to_have_count(1)
    assert client.get("/api/extension/context/job-two").json()["personal_facts"][0]["value"] == "Three years"
    expect(control.locator("#fill")).to_be_disabled()
    expect(control.locator("#contextFacts")).to_contain_text("I have three years of Python experience.")
    def draft(settings, *, questions, context, instructions):
        fact = context["personal_facts"][0]
        return [{"question_id": questions[0].id, "answer": fact["value"], "status": "draft",
            "source_ids": ["fact." + fact["fact_id"]], "rationale": "Confirmed experience",
            "missing_information": [], "approved": False, "skipped": False,
            "replace_existing": False, "requires_confirmation": False}]
    monkeypatch.setattr("jobcockpit.extension.sessions.generate_answers", draft)
    control.locator("#tabApplication").click()
    page.locator("#experience").fill("")
    control.locator("#scan").click()
    expect(control.locator("#questions article")).to_have_count(1)
    control.locator("#draft").click()
    expect(control.get_by_label("Answer: Python experience")).to_have_value("Three years", timeout=10000)
    expect(control.locator("#questions .source-note")).to_contain_text("I have three years of Python experience.")
    expect(control.locator("#questions .source-note")).not_to_contain_text("Personal fact changed")
    control.locator("#tabMemory").click()
    control.locator("#savedFacts").get_by_text("Edit fact or expiration", exact=True).click()
    control.locator("#savedFacts").get_by_label("Short factual value", exact=True).fill("Four years")
    control.locator("#savedFacts").get_by_label("Complete fact, including conditions").fill("I have four years of Python experience.")
    control.get_by_role("button", name="Save correction").click()
    expect(control.locator("#savedFacts h3")).to_have_text("I have four years of Python experience.")
    expect(control.locator("#contextFacts")).to_contain_text("I have four years of Python experience.")
    expect(control.locator("#contextFacts")).not_to_contain_text("three years")
    control.get_by_role("button", name="Delete fact", exact=True).click()
    expect(control.locator("#savedFacts article")).to_have_count(0)
    assert client.get("/api/extension/context/job-two").json()["personal_facts"] == []
    expect(control.locator("#contextFacts")).to_have_text("No confirmed facts match this job yet.")
    control.locator("#discardLearning").click()
    expect(control.locator("#learningWorkspace")).to_be_hidden()


def test_expired_approval_can_be_renewed_live_and_after_reopening(extension, assistance):
    from jobcockpit.db.models import ExtensionApplicationSession
    from jobcockpit.db.session import session_scope

    client, settings = assistance
    browser, control, _, page_for, _ = extension
    shorten_approval = True
    def api_route(route):
        request = route.request
        response = client.request(request.method, request.url.replace("http://localhost:8000", ""),
            content=request.post_data, headers={"content-type": "application/json"})
        body = response.content
        if shorten_approval and request.url.endswith("/review") and response.status_code == 200:
            review = response.json()
            # A short lifetime exercises the same persisted API contract without a 15-minute wait.
            with session_scope(settings) as db:
                row = db.get(ExtensionApplicationSession, review["session_id"])
                row.answers = [{**a, "approval_expires_at": int(time.time()) + 2} if a.get("approved") else a for a in row.answers]
            body = client.get(f"/api/extension/application-sessions/{review['session_id']}").content
        route.fulfill(status=response.status_code, body=body,
            headers={"content-type": response.headers.get("content-type", "application/json")})
    browser.route("http://localhost:8000/api/**", api_route)
    page, _ = page_for('<label>First name<input id="first"></label><label>City<input id="city"></label>')
    control.reload()
    expect(control.locator("#jobSelect option[value='job-one']")).to_have_count(1)
    control.locator("#jobSelect").select_option("job-one")
    expect(control.locator("#jobSummary")).to_contain_text("Software intern")
    control.locator("#jobConfirmed").check()
    control.locator("#scan").click()
    first = control.locator("#questions article").nth(0)
    expect(first).to_be_visible()
    first.get_by_role("button", name="Approve", exact=True).click()
    expect(control.locator("#fill")).to_be_enabled()
    # Expiry must update controls without rebuilding unrelated in-progress editors.
    city = control.get_by_label("Answer: City")
    city.fill("An unfinished answer")
    city.focus()
    expect(first.get_by_role("button", name="Approve again", exact=True)).to_be_enabled(timeout=5000)
    expect(first.locator(".badge")).to_have_text("Approval expired")
    expect(city).to_have_value("An unfinished answer")
    expect(city).to_be_focused()
    expect(control.locator("#fill")).to_be_disabled()
    expect(control.locator("#reviewCount")).to_contain_text("0 approved")
    control.reload()
    expect(first.get_by_role("button", name="Approve again", exact=True)).to_be_enabled()
    shorten_approval = False
    first.get_by_role("button", name="Approve again", exact=True).click()
    expect(control.locator("#fill")).to_be_enabled()
    control.locator("#fill").click()
    expect(page.locator("#first")).to_have_value("Example")


def test_panel_follows_application_steps_without_carrying_approvals(extension, assistance):
    browser, control, _, page_for, _ = extension
    client, _ = assistance
    created = []
    def api_route(route):
        request = route.request
        response = client.request(request.method, request.url.replace("http://localhost:8000", ""),
            content=request.post_data, headers={"content-type": "application/json"})
        if request.method == "POST" and request.url.endswith("/application-sessions"):
            created.append(response.json())
        route.fulfill(status=response.status_code, body=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json")})
    browser.route("http://localhost:8000/api/**", api_route)
    page, _ = page_for('<main><label>First name<input id="first"></label></main>')
    control.reload()
    expect(control.locator("#jobSelect option[value='job-one']")).to_have_count(1)
    control.locator("#jobSelect").select_option("job-one")
    expect(control.locator("#jobSummary")).to_contain_text("Software intern")
    control.locator("#jobConfirmed").check()
    control.locator("#scan").click()
    expect(control.get_by_label("Answer: First name")).to_be_visible()
    control.locator("#instructions").evaluate("e => e.closest('details').open = true")
    control.locator("#instructions").fill("Keep answers concise")
    control.get_by_role("button", name="Approve", exact=True).click()
    expect(control.locator("#fill")).to_be_enabled()
    page.locator("#first").fill("Manual answer")
    page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend','<p>Saved just now</p>')")
    control.wait_for_timeout(2500)
    assert len(created) == 1  # Typing and autosave decoration must preserve the review.
    page.evaluate("document.querySelector('main').innerHTML='<label>City<input id=city></label>'")
    expect(control.get_by_label("Answer: City")).to_be_visible(timeout=12000)
    expect(control.get_by_label("Answer: First name")).to_have_count(0)
    expect(control.locator("#fill")).to_be_disabled()
    expect(control.locator("#jobConfirmed")).to_be_checked()
    expect(control.locator("#instructions")).to_have_value("Keep answers concise")
    assert all(not answer["approved"] for answer in created[-1]["answers"])
    expect(page.locator("#city")).to_have_value("")
    # New fields in an accessible frame and temporary loading states must also be followed.
    page.evaluate("document.querySelector('main').innerHTML='<p>Loading next step</p>'")
    expect(control.locator("#questions article")).to_have_count(0, timeout=12000)
    page.route("**/apply/test/education-frame", lambda route: route.fulfill(
        content_type="text/html", body="<label>Degree<input></label>"))
    page.evaluate("document.querySelector('main').innerHTML='<iframe src=/apply/test/education-frame></iframe>'")
    expect(control.get_by_label("Answer: Degree")).to_be_visible(timeout=12000)
    # Full document navigation within this application keeps the job, with fresh questions.
    page.goto("http://localhost:8001/apply/test/experience?step=3")
    expect(control.get_by_label("Answer: First name")).to_be_visible(timeout=12000)
    expect(control.get_by_label("Answer: Degree")).to_have_count(0)
    expect(control.locator("#jobConfirmed")).to_be_checked()
    expect(control.locator("#fill")).to_be_disabled()
    # A same-document URL transition also invalidates the previous step.
    previous_count = len(created)
    page.evaluate("history.pushState({}, '', '?step=4')")
    control.wait_for_function("document.querySelector('#status').textContent.startsWith('Application step updated')")
    expect(control.get_by_label("Answer: First name")).to_be_visible(timeout=12000)
    control.wait_for_timeout(2500)
    assert len(created) == previous_count + 1
    control.reload()
    expect(control.get_by_label("Answer: First name")).to_be_visible()
    page.evaluate("document.querySelector('main').innerHTML='<label>Phone<input></label>'")
    expect(control.get_by_label("Answer: Phone")).to_be_visible(timeout=12000)
    # Reusing the same DOM nodes after a loading overlay must not leave an empty panel.
    page.locator("main").evaluate("e => e.hidden = true")
    expect(control.locator("#questions article")).to_have_count(0, timeout=12000)
    page.locator("main").evaluate("e => e.hidden = false")
    expect(control.get_by_label("Answer: Phone")).to_be_visible(timeout=12000)
    previous_count = len(created)
    page.goto("http://localhost:8001/apply/different-job")
    expect(control.locator("#jobConfirmed")).not_to_be_checked(timeout=12000)
    expect(control.locator("#questions article")).to_have_count(0)
    control.wait_for_timeout(2500)
    assert len(created) == previous_count
    control.locator("#jobConfirmed").check()
    control.locator("#scan").click()
    expect(control.get_by_label("Answer: First name")).to_be_visible()
    control.locator("#clearReview").click()
    expect(control.locator("#questions article")).to_have_count(0)
    previous_count = len(created)
    page.evaluate("document.querySelector('main').innerHTML='<label>City<input></label>'")
    control.wait_for_timeout(3000)
    assert len(created) == previous_count


@pytest.fixture
def followed_panel(extension, assistance):
    browser, control, _, page_for, _ = extension
    client, _ = assistance
    created = []
    def api_route(route):
        request = route.request
        response = client.request(request.method, request.url.replace("http://localhost:8000", ""),
            content=request.post_data, headers={"content-type": "application/json"})
        if request.method == "POST" and request.url.endswith("/application-sessions"):
            created.append(response.json())
        route.fulfill(status=response.status_code, body=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json")})
    browser.route("http://localhost:8000/api/**", api_route)
    page, _ = page_for('<label>First name<input></label><label>City<input></label>')
    control.reload()
    expect(control.locator("#jobSelect option[value='job-one']")).to_have_count(1)
    control.locator("#jobSelect").select_option("job-one")
    expect(control.locator("#jobSummary")).to_contain_text("Software intern")
    control.locator("#jobConfirmed").check()
    control.locator("#scan").click()
    expect(control.locator("#questions article")).to_have_count(2)
    return page, control, created


def test_empty_frames_preserve_review_and_new_frame_questions_are_discovered(followed_panel):
    page, control, created = followed_panel
    control.get_by_label("Answer: City").fill("Unfinished city answer")
    control.locator("#questions article").first.get_by_role("button", name="Approve", exact=True).click()
    page.route("**/background-frame*", lambda route: route.fulfill(content_type="text/html", body="<p>Telemetry</p>"))
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<iframe hidden src=/background-frame></iframe>')")
    control.wait_for_timeout(3000)
    assert len(created) == 1
    # Include the empty frame in a manual scan, then verify its later reload does
    # not leave a stale document route in the retained review's undo operations.
    control.locator("#scan").click()
    first_approve = control.locator("#questions article").first.get_by_role("button", name="Approve", exact=True)
    expect(first_approve).to_be_visible()
    control.get_by_label("Answer: City").fill("Unfinished city answer")
    first_approve.click()
    page.locator("iframe").evaluate("e => e.src='/background-frame?refresh=1'")
    control.wait_for_timeout(3000)
    assert len(created) == 2
    expect(control.get_by_label("Answer: City")).to_have_value("Unfinished city answer")
    expect(control.locator("#fill")).to_be_enabled()
    control.locator("#fill").click()
    expect(page.locator("input").first).to_have_value("Example")
    expect(control.get_by_label("Answer: City")).to_have_value("Unfinished city answer")
    control.locator("#undo").click()
    expect(control.locator("#status")).to_contain_text("Local undo completed")
    expect(page.locator("input").first).to_have_value("")
    # Still inspect previously empty frames so a form added later is discovered.
    page.locator("iframe").evaluate("e => { e.hidden=false; e.contentDocument.body.innerHTML='<label>Degree<input></label>'; }")
    expect(control.get_by_label("Answer: Degree")).to_be_visible(timeout=12000)
    expect(control.locator("#questions article")).to_have_count(3)
    expect(control.locator("#fill")).to_be_disabled()
    page.locator("iframe").evaluate("e => e.contentDocument.body.innerHTML='<p>Complete</p>'")
    expect(control.get_by_label("Answer: Degree")).to_have_count(0, timeout=12000)
    expect(control.locator("#questions article")).to_have_count(2)


@pytest.mark.parametrize("navigation", ["different_job", "same_url_reload", "same_application_spa"])
def test_paused_following_invalidates_review_on_navigation(followed_panel, navigation):
    page, control, created = followed_panel
    control.locator("#questions article").first.get_by_role("button", name="Approve", exact=True).click()
    control.locator("#jobConfirmed").uncheck()
    if navigation == "different_job":
        page.goto("http://localhost:8001/apply/another-job")
    elif navigation == "same_url_reload":
        page.reload()
    else:
        page.evaluate("history.pushState({}, '', '?step=2')")
    expect(control.locator("#questions article")).to_have_count(0, timeout=12000)
    expect(control.locator("#fill")).to_be_disabled()
    expect(control.locator("#jobConfirmed")).not_to_be_checked()
    control.wait_for_timeout(3000)
    assert len(created) == 1
    control.reload()
    expect(control.locator("#jobSelect")).to_have_value("job-one")
    expect(control.locator("#questions article")).to_have_count(0)
    control.wait_for_timeout(2500)
    assert len(created) == 1  # Reopening must not silently resume following.


def test_documents_require_exact_native_target_and_preserve_other_fields(extension):
    _, _, _, page_for, message = extension
    page, tab = page_for('''<label>Transcript<input id="transcript" type="file" accept=".pdf"></label>
      <label>Resume<input id="resume" type="file" accept=".pdf"></label>
      <div data-upload-target="resume">Drop resume here</div>''')
    questions = message(tab, "scan")["questions"]
    assert [q["purpose"] for q in questions] == ["transcript", "resume", "unknown"]
    assert not questions[2]["fillable"]
    data = b"%PDF-1.4\nsynthetic"
    file = {"name": "test.pdf", "base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
    result = message(tab, "attach", plan=plan(questions[1], "test.pdf"), file=file)
    assert result["results"][0]["status"] == "attached"
    assert page.locator("#transcript").evaluate("e=>e.files.length") == 0
    assert page.locator("#resume").evaluate("e=>e.files[0].name") == "test.pdf"
    assert message(tab, "undo")["results"][0]["status"] == "undone"


def test_live_extension_panel_scans_approves_fills_and_restores_review(extension, assistance):  # noqa: F811
    browser, control, _, page_for, _ = extension
    client, _ = assistance
    def api_route(route):
        request = route.request
        response = client.request(request.method, request.url.replace("http://localhost:8000", ""),
            content=request.post_data, headers={"content-type": "application/json"})
        route.fulfill(status=response.status_code, body=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json")})
    browser.route("http://localhost:8000/api/**", api_route)
    page, _ = page_for('<h1>Example application</h1><label>First name<input id="first"></label>')
    control.reload()
    expect(control.locator("#jobSelect option[value='job-one']")).to_have_count(1)
    control.locator("#jobSelect").select_option("job-one")
    expect(control.locator("#jobSummary")).to_contain_text("Software intern")
    control.locator("#jobConfirmed").check()
    control.locator("#scan").click()
    expect(control.locator("#questions article")).to_have_count(1)
    expect(control.get_by_label("Answer: First name")).to_have_value("Example")
    control.get_by_role("button", name="Approve", exact=True).click()
    expect(control.locator("#fill")).to_be_enabled()
    control.locator("#fill").click()
    expect(page.locator("#first")).to_have_value("Example")
    expect(control.locator("#status")).to_contain_text("1 verified filled")
    control.get_by_label("Answer: First name").fill("An edit needing review")
    expect(control.locator("#fill")).to_be_disabled()
    control.reload()
    expect(control.locator("#questions article")).to_have_count(1)
    expect(control.get_by_label("Answer: First name")).to_have_value("An edit needing review")
    expect(control.get_by_role("button", name="Approve", exact=True)).to_be_enabled()
    expect(control.locator("#fill")).to_be_disabled()
    control.locator("#undo").click()
    expect(page.locator("#first")).to_have_value("")
