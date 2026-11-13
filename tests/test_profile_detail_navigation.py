"""Real Playwright interactions with locally routed HTML; no live requests."""
import json
from unittest.mock import patch

import pytest
from playwright.sync_api import sync_playwright
from scholar_scraper.browser import (
    ProfileDetailNavigator, UnresolvedPublicationError, citation_identifier,
    scrape_publications_details_batch,
)

PROFILE = "https://scholar.google.com/citations?user=A123&hl=en"
URL = "https://scholar.google.com/citations?view_op=view_citation&user=A123&citation_for_view=A123:one"


def profile_html(mode="dialog", wrong=False):
    key = "wrong" if wrong else "one"
    action = (f"event.preventDefault();document.querySelector('#gsc_oci').style.display='block';"
              f"document.querySelector('#identity').value='A123:{key}';window.clicked=true;")
    attrs = f'onclick="{action}"' if mode == "dialog" else ('target="_blank"' if mode == "popup" else '')
    return f'''<div id="gsc_prf_in">Researcher</div><table id="gsc_a_b">
      <tr class="gsc_a_tr"><td><a class="gsc_a_at" href="{URL.replace(':one', ':other')}">Same title</a></td></tr>
      </table><button id="gsc_bpf_more" onclick="document.querySelector('#extra').style.display='';this.disabled=true">Show more</button>
      <script>window.clicked=false;</script>
      <table><tbody id="extra" style="display:none"><tr><td></td></tr></tbody></table>
      <script>document.querySelector('#gsc_bpf_more').onclick=()=>{{
        document.querySelector('#gsc_a_b').insertAdjacentHTML('beforeend',document.querySelector('#template').innerHTML);
        document.querySelector('#gsc_bpf_more').disabled=true;
      }};</script>
      <template id="template"><tr class="gsc_a_tr"><td><a class="gsc_a_at" href="{URL}" {attrs}>Same title</a></td></tr></template>
      <div id="gsc_oci" style="display:none"><input id="identity" name="citation_for_view">
        <div id="gsc_oci_title">Exact publication</div>
        <button id="gs_md_cita-d-x" onclick="document.querySelector('#gsc_oci').style.display='none'">Close</button>
      </div>'''


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        yield page
        browser.close()


@pytest.mark.parametrize("mode", ["dialog", "page", "popup"])
def test_real_click_return_and_canonical_checkpoint(page, tmp_path, mode):
    requests = []
    def route(r):
        requests.append(r.request.url)
        body = '<div id="gsc_oci_title">Exact publication</div>' if 'view_citation' in r.request.url else profile_html(mode)
        r.fulfill(status=200, content_type="text/html", body=body)
    page.context.route("**/*", route)
    events = []
    queue = [{"publication_id": "41", "scholar_id": "9", "title": "Same title", "source_url": URL}]
    with patch("scholar_scraper.browser.time.sleep") as sleep, patch.object(page, "goto", wraps=page.goto) as goto, patch.object(page, "wait_for_timeout", wraps=page.wait_for_timeout) as pause:
        result = scrape_publications_details_batch(queue, tmp_path / 'checkpoint.json',
                    page_override=page, profile_url=PROFILE, delay=0, progress_callback=events.append)
    assert [call.args[0] for call in goto.call_args_list] == [PROFILE]
    pause.assert_any_call(1000.0)
    assert requests[0] == PROFILE
    record = result['publications']['41']
    assert record['status'] == 'SUCCESS', record
    assert citation_identifier(record['publication_url']) == 'A123:one'
    assert citation_identifier(record['metadata']['publication_url']) == 'A123:one'
    assert page.locator('#gsc_a_b').is_visible()
    assert any(c.args == (0.5,) for c in sleep.call_args_list)
    assert any(c.args == (1.0,) for c in sleep.call_args_list)
    assert any(e['status'] == 'Clicking publication title' for e in events)
    assert json.loads((tmp_path / 'checkpoint.json').read_text())['publications']['41']['status'] == 'SUCCESS'
    requests.clear()
    again = scrape_publications_details_batch(queue, tmp_path / 'checkpoint.json', page_override=page, profile_url=PROFILE)
    assert again['summary']['skipped_checkpoint'] == 1
    assert requests == []


@pytest.mark.parametrize("wrong,missing", [(True, False), (False, True)])
def test_unresolved_identity_never_saved_as_success(page, tmp_path, wrong, missing):
    page.route('**/*', lambda r: r.fulfill(body=profile_html(wrong=wrong), content_type='text/html'))
    url = URL.replace(':one', ':missing') if missing else URL
    with patch('scholar_scraper.browser.time.sleep'):
        result = scrape_publications_details_batch(
            [{'publication_id': '1', 'source_url': url}], tmp_path / 'checkpoint.json',
            page_override=page, profile_url=PROFILE, timeout_s=2)
    assert result['publications']['1']['status'] == 'UNRESOLVED'


@pytest.mark.parametrize("status,body", [(429, 'Restricted'), (200, '<form id="captcha-form">CAPTCHA</form>')])
def test_block_stops_after_first_request(page, tmp_path, status, body):
    requests = []
    def route(r):
        requests.append(r.request.url)
        r.fulfill(status=status, body=body, content_type='text/html')
    page.route('**/*', route)
    result = scrape_publications_details_batch(
        [{'publication_id': str(i), 'source_url': URL} for i in (1, 2)], tmp_path/'checkpoint.json',
        page_override=page, profile_url=PROFILE, timeout_s=1)
    assert result['publications']['1']['status'] == 'BLOCKED'
    assert len(requests) == 1


def test_identifier_is_decoded_but_case_sensitive():
    assert citation_identifier(URL.replace('A123:one', 'A123%3Aone')) == 'A123:one'
    assert citation_identifier(URL) != citation_identifier(URL.replace(':one', ':ONE'))


def test_empty_queue_needs_no_profile_or_navigation(tmp_path):
    from unittest.mock import MagicMock
    page = MagicMock()
    result = scrape_publications_details_batch([], tmp_path/'empty.json', page_override=page)
    assert result['summary']['attempted_this_run'] == 0
    page.goto.assert_not_called()


def test_invalid_queue_is_checkpointed_without_navigation(tmp_path):
    from unittest.mock import MagicMock
    page = MagicMock()
    result = scrape_publications_details_batch([{'publication_id': '1'}], tmp_path/'invalid.json', page_override=page)
    assert result['publications']['1']['status'] == 'FAILED'
    page.goto.assert_not_called()


def test_timing_options_cannot_disable_minimum_delays():
    nav = ProfileDetailNavigator(None, PROFILE, interaction_delay=0, post_click_delay=-1, return_delay=0)
    assert (nav.interaction_delay, nav.post_click_delay, nav.return_delay) == (0.5, 1.0, 1.0)


def test_dialog_can_be_opened_again_without_reloading_listing(page):
    requests = []
    def route(r):
        requests.append(r.request.url)
        r.fulfill(body=profile_html(), content_type='text/html')
    page.route('**/*', route)
    nav = ProfileDetailNavigator(page, PROFILE, timeout_ms=2000)
    with patch('scholar_scraper.browser.time.sleep'):
        nav.open(URL)
        nav.return_to_profile()
        nav.open(URL)
        nav.return_to_profile()
    assert requests == [PROFILE]
    assert page.locator('#gsc_a_b a.gsc_a_at').count() == 2
