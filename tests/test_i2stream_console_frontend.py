"""Frontend contract coverage for the native i2Stream Console panel."""

from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SW = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
MODULE_PATH = ROOT / "static" / "i2stream_console.js"


def _run_contract_case(case: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the JavaScript contract test")
    script = f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync({json.dumps(str(MODULE_PATH))}, 'utf8');
const document = {{
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {{}},
}};
const sandbox = {{ window: {{}}, URL, URLSearchParams, console, document }};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const c = sandbox.window.__i2streamConsoleTest;
if (!c) throw new Error('test contract export is missing');
(async () => {{
  let output;
  {case}
  process.stdout.write(JSON.stringify(output));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_i2stream_has_desktop_mobile_sidebar_and_main_surfaces():
    assert INDEX.count('data-panel="i2stream"') == 2
    assert INDEX.count('class="i2stream-nav-icon"') == 2
    assert INDEX.count('src="static/i2.ico?v=__WEBUI_VERSION__"') == 2
    assert 'id="panelI2stream"' in INDEX
    assert 'id="mainI2stream"' in INDEX
    for section in ("knowledge", "reports", "history"):
        assert f'data-i2stream-section="{section}"' in INDEX
    assert 'id="i2streamKnowledgeUpload"' in INDEX
    assert 'id="i2streamMainContent"' in INDEX
    assert 'data-i2stream-scope="global"' in INDEX
    assert 'id="i2streamHistoryClientId"' in INDEX
    assert 'onsubmit="submitI2StreamHistoryClientId(event)"' in INDEX


def test_knowledge_view_exposes_batch_selection_controls():
    assert 'id="i2streamKnowledgeBatchActions"' in INDEX
    assert 'id="i2streamKnowledgeSelectAll"' in INDEX
    assert 'onchange="toggleI2StreamKnowledgeAll(this.checked)"' in INDEX
    assert 'id="i2streamKnowledgeSelectionCount"' in INDEX
    assert 'id="i2streamKnowledgeDeleteSelected"' in INDEX
    assert 'onclick="deleteSelectedI2StreamKnowledge()"' in INDEX


def test_panel_switch_wires_i2stream_into_existing_navigation_lifecycle():
    assert "i2stream: 'tab_i2stream'" in PANELS
    main_panels = re.search(r"const MAIN_VIEW_PANELS = \[([^\]]+)\];", PANELS)
    assert main_panels and "'i2stream'" in main_panels.group(1)
    assert "if (nextPanel === 'i2stream') await loadI2StreamConsole();" in PANELS
    assert "main.main.showing-i2stream > #mainI2stream" in STYLE


def test_contract_parsers_keep_composite_identity_and_pagination_cursor():
    result = _run_contract_case(
        """
const page = c.parseConversationPage({
  code: 0,
  status: 'success',
  conversations: [
    {client_id: 'client-a-00000000', conversation_id: 'same', message_count: 2,
     latest_message_at: '2026-08-10T08:00:00Z', preview: 'Alpha'},
    {client_id: 'client-b-00000000', conversation_id: 'same', message_count: 4,
     latest_message_at: '2026-08-10T09:00:00Z', preview: 'Beta'}
  ],
  next_before: 1700000000
});
output = {
  keys: page.conversations.map(item => item.conversationKey),
  next: page.nextBefore,
  listUrl: c.conversationPageUrl('client-a-00000000', 25, 1700000000),
  detailUrl: c.conversationDetailUrl('conversation / one', 'client + one'),
  reportUrl: c.resolveI2StreamBrowserUrl(
    '/api/i2stream-console/reports/abcdefghijklmnop/content',
    'https://example.test/hermes/'
  )
};
"""
    )
    assert result == {
        "keys": ['["client-a-00000000","same"]', '["client-b-00000000","same"]'],
        "next": 1700000000,
        "listUrl": (
            "/api/i2stream-console/conversations?client_id=client-a-00000000"
            "&limit=25&before=1700000000"
        ),
        "detailUrl": (
            "/api/i2stream-console/conversations/conversation%20%2F%20one/messages"
            "?client_id=client+%2B+one"
        ),
        "reportUrl": (
            "https://example.test/hermes/api/i2stream-console/reports/"
            "abcdefghijklmnop/content"
        ),
    }


def test_knowledge_selection_is_reconciled_when_the_backend_list_shrinks():
    result = _run_contract_case(
        """
const selected = new Set(['file-a', 'file-removed']);
const reconciled = c.reconcileKnowledgeSelection(
  [{fileId: 'file-a'}, {fileId: 'file-b'}],
  selected
);
output = {
  reconciled: Array.from(reconciled),
  original: Array.from(selected),
};
"""
    )
    assert result == {
        "reconciled": ["file-a"],
        "original": ["file-a", "file-removed"],
    }


def test_knowledge_batch_delete_calls_every_selected_file_and_reports_partial_failure():
    result = _run_contract_case(
        """
const calls = [];
sandbox.api = async (url, options) => {
  calls.push({url, method: options.method});
  if (url.endsWith('/file-beta')) throw new Error('backend unavailable');
  return {code: 0, status: 'success'};
};
const deletion = await c.deleteKnowledgeFiles([
  {fileId: 'file-alpha', displayName: 'Alpha'},
  {fileId: 'file-beta', displayName: 'Beta'},
]);
output = {
  calls,
  deletedFileIds: deletion.deletedFileIds,
  failures: deletion.failures,
};
"""
    )
    assert result == {
        "calls": [
            {
                "url": "/api/i2stream-console/knowledge/files/file-alpha",
                "method": "DELETE",
            },
            {
                "url": "/api/i2stream-console/knowledge/files/file-beta",
                "method": "DELETE",
            },
        ],
        "deletedFileIds": ["file-alpha"],
        "failures": [
            {
                "fileId": "file-beta",
                "displayName": "Beta",
                "message": "backend unavailable",
            }
        ],
    }


def test_history_does_not_request_conversations_before_a_client_id_is_submitted():
    result = _run_contract_case(
        """
let calls = 0;
sandbox.api = async () => {
  calls += 1;
  return {code: 0, status: 'success', conversations: [], next_before: null};
};
await c.loadI2StreamHistory(true);
output = {calls};
"""
    )
    assert result == {"calls": 0}


@pytest.mark.parametrize(
    "expression",
    [
        "c.parseConversationPage({code:0,status:'success',conversations:[]})",
        "c.parseConversationPage({code:0,status:'success',conversations:[{client_id:'x'}],next_before:null})",
        "c.parseConversationDetail({code:0,status:'success',conversation_id:'x',messages:{}})",
        "c.parseConversationDetail({code:0,status:'success',conversation_id:'x',messages:[{id:0,role:'user',content:'x',created_at:'2026-01-01'}]})",
        "c.parseKnowledgeFiles({code:0,status:'success',files:{}})",
        "c.parseReports({code:0,status:'success',files:null,server_time:1})",
    ],
)
def test_malformed_backend_contract_fails_closed(expression):
    result = _run_contract_case(
        f"""
try {{ {expression}; output = {{threw: false}}; }}
catch (error) {{ output = {{threw: true, name: error.name}}; }}
"""
    )
    assert result == {"threw": True, "name": "TypeError"}


def test_i2stream_module_uses_only_same_origin_console_api_contract():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "http://" not in source and "https://" not in source
    assert "const I2STREAM_API = '/api/i2stream-console'" in source
    for route in (
        "/knowledge/files",
        "/knowledge/tasks/",
        "/reports",
        "/conversations",
    ):
        assert route in source
    assert "FormData" in source
    assert "method: 'DELETE'" in source
    assert "client_id:" in source
    assert "input.files[0].size > MAX_UPLOAD_BYTES" in source
    assert "requestGeneration: {knowledge: 0, reports: 0, history: 0}" in source
    assert "historyDetailGeneration" in source
    assert source.count("generation !== _i2streamState.historyDetailGeneration") == 2


def test_i2stream_deep_link_selects_valid_view_without_consuming_session_query():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "query.get('panel') !== 'i2stream'" in source
    assert "I2STREAM_SECTIONS.has(requestedView) ? requestedView : 'knowledge'" in source
    assert "switchPanel('i2stream')" in source
    assert "replaceState" not in source


def test_keyboard_theme_and_mobile_contracts_are_explicit():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "event.key === 'ArrowDown'" in source
    assert "event.key === 'ArrowUp'" in source
    assert "event.key === 'Enter'" in source
    assert "event.key === 'Escape'" in source
    assert "aria-selected" in INDEX and 'role="tablist"' in INDEX
    assert "var(--surface)" in STYLE
    assert "var(--border)" in STYLE
    assert "var(--accent" in STYLE
    mobile = STYLE[STYLE.index("/* i2Stream Console */") :]
    assert "@media(max-width:700px)" in mobile
    assert "min-height:44px" in mobile
    assert "grid-template-columns:44px minmax(0,1fr)" in mobile


def test_new_module_is_loaded_and_precached_with_the_static_shell():
    script = 'static/i2stream_console.js?v=__WEBUI_VERSION__'
    assert f'<script src="{script}" defer></script>' in INDEX
    assert "'./static/i2stream_console.js' + VQ" in SW
    assert "'./static/i2.ico' + VQ" in SW
