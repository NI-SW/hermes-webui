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
const sandbox = {{ window: {{}}, URL, URLSearchParams, console, document, setTimeout }};
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
    for section in ("knowledge", "reports", "history", "nodes", "logmonitor"):
        assert f'data-i2stream-section="{section}"' in INDEX
    assert 'id="i2streamKnowledgeUpload"' in INDEX
    assert 'id="i2streamMainContent"' in INDEX
    assert 'data-i2stream-scope="global"' in INDEX
    assert 'id="i2streamHistoryClientId"' in INDEX
    assert 'onsubmit="submitI2StreamHistoryClientId(event)"' in INDEX
    assert 'id="i2streamNodesList"' in INDEX
    assert 'id="i2streamNodesStatus"' in INDEX
    for section in ("Knowledge", "Reports", "History", "Nodes", "Logmonitor"):
        assert f'id="i2stream{section}Tab"' in INDEX
        assert f'aria-controls="i2stream{section}Page"' in INDEX
        assert f'id="i2stream{section}Page" role="tabpanel"' in INDEX
        assert f'aria-labelledby="i2stream{section}Tab"' in INDEX
    assert 'id="i2streamKnowledgeTab" role="tab" tabindex="0"' in INDEX
    for section in ("Reports", "History", "Nodes", "Logmonitor"):
        assert f'id="i2stream{section}Tab" role="tab" tabindex="-1"' in INDEX
    source = MODULE_PATH.read_text(encoding="utf-8")
    for key in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"):
        assert f"event.key === '{key}'" in source


def test_knowledge_view_exposes_batch_selection_controls():
    assert re.search(
        r'id="i2streamKnowledgeFile"[^>]*\bmultiple\b',
        INDEX,
    )
    assert 'id="i2streamKnowledgeBatchActions"' in INDEX
    assert 'id="i2streamKnowledgeSelectAll"' in INDEX
    assert 'onchange="toggleI2StreamKnowledgeAll(this.checked)"' in INDEX
    assert 'id="i2streamKnowledgeSelectionCount"' in INDEX
    assert 'id="i2streamKnowledgeDeleteSelected"' in INDEX
    assert 'onclick="deleteSelectedI2StreamKnowledge()"' in INDEX


def test_knowledge_view_exposes_service_configuration_before_upload():
    config_index = INDEX.index('id="i2streamKnowledgeConfig"')
    upload_index = INDEX.index('id="i2streamKnowledgeUpload"')
    assert config_index < upload_index
    assert 'id="i2streamKnowledgeVectorHost"' in INDEX
    assert 'name="vector_search_host"' in INDEX
    assert 'id="i2streamKnowledgeRagMcpUrl"' in INDEX
    assert 'name="rag_service_mcp_url"' in INDEX
    assert 'onclick="checkI2StreamKnowledgeConfig()"' in INDEX
    assert 'onsubmit="saveI2StreamKnowledgeConfig(event)"' in INDEX
    assert 'id="i2streamKnowledgeUnconfigured"' in INDEX


def test_knowledge_view_exposes_collection_selector_and_creator_before_upload():
    collection_index = INDEX.index('id="i2streamKnowledgeCollections"')
    upload_index = INDEX.index('id="i2streamKnowledgeUpload"')
    assert collection_index < upload_index
    assert 'id="i2streamKnowledgeCollectionSelect"' in INDEX
    assert 'onchange="selectI2StreamKnowledgeCollection(this.value)"' in INDEX
    assert 'id="i2streamKnowledgeCollectionCreate"' in INDEX
    assert 'onsubmit="createI2StreamKnowledgeCollection(event)"' in INDEX
    assert 'name="collection_name"' in INDEX


def test_knowledge_collection_contract_is_strict_and_selects_default_then_preferred():
    result = _run_contract_case(
        """
const collections = c.parseKnowledgeCollections({
  code: 0, status: 'success', total: 2,
  collections: [
    {name: 'archive', points_count: 4, vectors_count: 4, status: 'green', schema: 'v1', is_default: false},
    {name: 'documents', points_count: 8, vectors_count: 8, status: 'green', schema: 'v1', is_default: true}
  ]
});
let malformedRejected = false;
try {
  c.parseKnowledgeCollections({
    code: 0, status: 'success', total: 1,
    collections: [{name: 'broken', points_count: 0, vectors_count: 0, status: 'green', schema: 'v1'}]
  });
} catch (error) { malformedRejected = error.name === 'TypeError'; }
let emptyRejected = false;
try { c.chooseKnowledgeCollection([], null); }
catch (error) { emptyRejected = error.name === 'TypeError'; }
output = {
  collections,
  defaultName: c.chooseKnowledgeCollection(collections, null),
  preferredName: c.chooseKnowledgeCollection(collections, 'archive'),
  malformedRejected,
  emptyRejected
};
"""
    )
    assert result == {
        "collections": [
            {
                "name": "archive",
                "pointsCount": 4,
                "vectorsCount": 4,
                "status": "green",
                "schema": "v1",
                "isDefault": False,
            },
            {
                "name": "documents",
                "pointsCount": 8,
                "vectorsCount": 8,
                "status": "green",
                "schema": "v1",
                "isDefault": True,
            },
        ],
        "defaultName": "documents",
        "preferredName": "archive",
        "malformedRejected": True,
        "emptyRejected": True,
    }


def test_knowledge_collection_create_and_scoped_urls_keep_collection_identity():
    result = _run_contract_case(
        """
const calls = [];
sandbox.api = async (url, options) => {
  calls.push({url, method: options.method, body: JSON.parse(options.body)});
  return {
    code: 0, status: 'success',
    collection: {collection_name: 'team docs', message: 'created'}
  };
};
const created = await c.createKnowledgeCollectionRequest('  team docs  ');
output = {
  calls,
  created,
  filesUrl: c.knowledgeCollectionUrl('/knowledge/files', 'team docs'),
  taskUrl: c.knowledgeCollectionUrl('/knowledge/tasks/task/a', 'team docs')
};
"""
    )
    assert result == {
        "calls": [
            {
                "url": "/api/i2stream-console/knowledge/collections",
                "method": "POST",
                "body": {"collection_name": "team docs"},
            }
        ],
        "created": {"name": "team docs", "message": "created"},
        "filesUrl": "/api/i2stream-console/knowledge/files?collection_name=team+docs",
        "taskUrl": "/api/i2stream-console/knowledge/tasks/task/a?collection_name=team+docs",
    }


def test_knowledge_file_contract_rejects_collection_mismatch():
    result = _run_contract_case(
        """
const payload = {
  code: 0, status: 'success', files: [{
    file_id: 'file-a', display_name: 'A.pdf', file_type: 'pdf', file_size: 12,
    upload_time: '2026-09-16T10:00:00Z', total_chunks: 2, collection_name: 'documents'
  }]
};
const files = c.parseKnowledgeFiles(payload, 'documents');
let mismatchRejected = false;
try { c.parseKnowledgeFiles(payload, 'archive'); }
catch (error) { mismatchRejected = error.name === 'TypeError'; }
output = {files, mismatchRejected};
"""
    )
    assert result == {
        "files": [
            {
                "fileId": "file-a",
                "displayName": "A.pdf",
                "fileType": "pdf",
                "fileSize": 12,
                "uploadTime": "2026-09-16T10:00:00Z",
                "totalChunks": 2,
                "collectionName": "documents",
            }
        ],
        "mismatchRejected": True,
    }


def test_knowledge_configuration_contract_parses_both_service_urls():
    result = _run_contract_case(
        """
const configured = c.parseKnowledgeConfiguration({
  code: 0,
  status: 'success',
  configuration: {
    configured: true,
    vector_search_host: 'http://192.168.34.65:8900',
    rag_service_mcp_url: 'http://192.168.34.65:8900/mcp',
    updated_at: '2026-09-16T10:00:00Z'
  }
});
const empty = c.parseKnowledgeConfiguration({
  code: 0,
  status: 'success',
  configuration: {
    configured: false,
    vector_search_host: '',
    rag_service_mcp_url: '',
    updated_at: null
  }
});
output = {configured, empty};
"""
    )
    assert result == {
        "configured": {
            "configured": True,
            "vectorSearchHost": "http://192.168.34.65:8900",
            "ragServiceMcpUrl": "http://192.168.34.65:8900/mcp",
            "updatedAt": "2026-09-16T10:00:00Z",
        },
        "empty": {
            "configured": False,
            "vectorSearchHost": "",
            "ragServiceMcpUrl": "",
            "updatedAt": None,
        },
    }


def test_knowledge_connection_check_accepts_ephemeral_configuration_without_update_time():
    result = _run_contract_case(
        """
const checked = c.parseKnowledgeConnectionCheck({
  code: 0,
  status: 'success',
  configuration: {
    configured: true,
    vector_search_host: 'http://rag.test:8900',
    rag_service_mcp_url: 'http://rag.test:8900/mcp',
    updated_at: null
  },
  checks: {
    vector_search: {status: 'reachable', url: 'http://rag.test:8900/health'},
    rag_service_mcp: {status: 'reachable', url: 'http://rag.test:8900/mcp'}
  }
});
output = checked;
"""
    )
    assert result == {
        "configured": True,
        "vectorSearchHost": "http://rag.test:8900",
        "ragServiceMcpUrl": "http://rag.test:8900/mcp",
        "updatedAt": None,
    }


def test_unconfigured_knowledge_service_skips_file_request_and_disables_file_controls():
    result = _run_contract_case(
        """
const controls = {
  i2streamKnowledgeConfigStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeVectorHost: {value: 'stale'},
  i2streamKnowledgeRagMcpUrl: {value: 'stale'},
  i2streamKnowledgeConfigState: {className: '', textContent: '', dataset: {}},
  i2streamKnowledgeUnconfigured: {hidden: true},
  i2streamKnowledgeFile: {disabled: false},
  i2streamKnowledgeUploadBtn: {disabled: false},
  i2streamKnowledgeBatchActions: {hidden: false},
  i2streamKnowledgeSelectAll: {checked: false, indeterminate: false, disabled: false},
  i2streamKnowledgeSelectionCount: {textContent: ''},
  i2streamKnowledgeDeleteSelected: {disabled: false},
  i2streamKnowledgeList: {innerHTML: ''}
};
document.getElementById = id => controls[id] || null;
let calls = 0;
sandbox.api = async () => {
  calls += 1;
  return {
    code: 0, status: 'success',
    configuration: {configured: false, vector_search_host: '', rag_service_mcp_url: '', updated_at: null}
  };
};
const loaded = await c.loadI2StreamKnowledge();
output = {
  loaded,
  calls,
  uploadDisabled: controls.i2streamKnowledgeUploadBtn.disabled,
  fileDisabled: controls.i2streamKnowledgeFile.disabled,
  warningHidden: controls.i2streamKnowledgeUnconfigured.hidden,
  vectorValue: controls.i2streamKnowledgeVectorHost.value,
  mcpValue: controls.i2streamKnowledgeRagMcpUrl.value
};
"""
    )
    assert result == {
        "loaded": True,
        "calls": 1,
        "uploadDisabled": True,
        "fileDisabled": True,
        "warningHidden": False,
        "vectorValue": "",
        "mcpValue": "",
    }


def test_knowledge_save_persists_service_then_syncs_dedicated_hermes_mcp_endpoint():
    result = _run_contract_case(
        """
const calls = [];
sandbox.api = async (url, options) => {
  calls.push({url, method: options.method, body: JSON.parse(options.body)});
  if (url.endsWith('/knowledge/config')) {
    return {
      code: 0, status: 'success', mcp_reload_required: true,
      configuration: {
        configured: true,
        vector_search_host: 'http://rag.test:8900',
        rag_service_mcp_url: 'http://rag.test:8900/mcp',
        updated_at: '2026-09-16T10:00:00Z'
      }
    };
  }
  return {
    code: 0, status: 'success',
    mcp: {
      rag_service_mcp_url: 'http://rag.test:8900/mcp',
      configured_profiles: ['default'],
      missing_profiles: ['stream-qa'],
      reload_required: true
    }
  };
};
const result = await c.saveKnowledgeConfigurationRequest({
  vector_search_host: 'http://rag.test:8900',
  rag_service_mcp_url: 'http://rag.test:8900/mcp'
});
output = {calls, result};
"""
    )
    assert result == {
        "calls": [
            {
                "url": "/api/i2stream-console/knowledge/config",
                "method": "PUT",
                "body": {
                    "vector_search_host": "http://rag.test:8900",
                    "rag_service_mcp_url": "http://rag.test:8900/mcp",
                },
            },
            {
                "url": "/api/rag-service-mcp",
                "method": "PUT",
                "body": {"rag_service_mcp_url": "http://rag.test:8900/mcp"},
            },
        ],
        "result": {
            "configuration": {
                "configured": True,
                "vectorSearchHost": "http://rag.test:8900",
                "ragServiceMcpUrl": "http://rag.test:8900/mcp",
                "updatedAt": "2026-09-16T10:00:00Z",
            },
            "mcp": {
                "ragServiceMcpUrl": "http://rag.test:8900/mcp",
                "configuredProfiles": ["default"],
                "missingProfiles": ["stream-qa"],
                "reloadRequired": True,
            },
            "mcpSyncError": None,
        },
    }


def test_knowledge_save_reports_partial_success_when_hermes_mcp_sync_fails():
    result = _run_contract_case(
        """
let calls = 0;
sandbox.api = async (url) => {
  calls += 1;
  if (url === '/api/rag-service-mcp') throw new Error('profile write failed');
  return {
    code: 0, status: 'success', mcp_reload_required: true,
    configuration: {
      configured: true,
      vector_search_host: 'http://rag.test:8900',
      rag_service_mcp_url: 'http://rag.test:8900/mcp',
      updated_at: '2026-09-16T10:00:00Z'
    }
  };
};
const saved = await c.saveKnowledgeConfigurationRequest({
  vector_search_host: 'http://rag.test:8900',
  rag_service_mcp_url: 'http://rag.test:8900/mcp'
});
output = {
  calls,
  configured: saved.configuration.configured,
  mcp: saved.mcp,
  error: saved.mcpSyncError.message
};
"""
    )
    assert result == {
        "calls": 2,
        "configured": True,
        "mcp": None,
        "error": "profile write failed",
    }

    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "服务地址已保存，但 Hermes MCP 配置同步失败" in i18n


def test_knowledge_save_syncs_backend_normalized_mcp_url():
    result = _run_contract_case(
        """
const calls = [];
sandbox.api = async (url, options) => {
  calls.push({url, body: JSON.parse(options.body)});
  if (url.endsWith('/knowledge/config')) {
    return {
      code: 0, status: 'success',
      configuration: {
        configured: true,
        vector_search_host: 'http://rag.test:8900',
        rag_service_mcp_url: 'http://rag.test:8900/mcp',
        updated_at: '2026-09-16T10:00:00Z'
      }
    };
  }
  return {
    code: 0, status: 'success',
    mcp: {
      rag_service_mcp_url: 'http://rag.test:8900/mcp',
      configured_profiles: ['default', 'stream-qa'],
      missing_profiles: [],
      reload_required: true
    }
  };
};
await c.saveKnowledgeConfigurationRequest({
  vector_search_host: 'HTTP://RAG.TEST:8900/',
  rag_service_mcp_url: 'HTTP://RAG.TEST:8900/mcp/'
});
output = calls;
"""
    )
    assert result[1] == {
        "url": "/api/rag-service-mcp",
        "body": {"rag_service_mcp_url": "http://rag.test:8900/mcp"},
    }


def test_logmonitor_view_collects_only_the_installation_inputs_and_defaults():
    expected = {
        "target_ip": None,
        "ssh_port": "22",
        "ssh_username": None,
        "ssh_password": None,
        "STREAM_LOG_MONITOR": "1",
        "MCP_IADEBUG_USER": "root",
        "ACTIVE_HOME": "/root/ia",
        "STREAM_HOME": "/root/i2stream",
        "STREAM_DATA_HOME": "/var/iadata",
    }
    page = INDEX[INDEX.index('id="i2streamLogmonitorPage"') :]
    page = page[: page.index("</section>") + len("</section>")]
    for name, default in expected.items():
        field = re.search(rf'<(?:input|select)[^>]*name="{re.escape(name)}"[^>]*>', page)
        assert field, f"missing LogMonitor field {name}"
        if default is not None and name != "STREAM_LOG_MONITOR":
            assert f'value="{default}"' in field.group(0)
    assert '<option value="1"' in page
    assert 'id="i2streamLogmonitorSshPassword"' in page
    assert 'type="password"' in page
    assert 'autocomplete="off"' in page
    password = re.search(r'<input[^>]*id="i2streamLogmonitorSshPassword"[^>]*>', page)
    assert password and "required" not in password.group(0)
    private_key = re.search(r'<input[^>]*id="i2streamLogmonitorSshPrivateKey"[^>]*>', page)
    assert private_key
    assert 'type="file"' in private_key.group(0)
    assert "accept=" not in private_key.group(0)
    assert 'name="AGENT_BACK_URL"' not in page
    assert 'name="AGENT_BASE_URL"' not in page
    assert 'name="AGENT_API_KEY"' not in page
    assert 'id="i2streamLogmonitorInstallBtn"' in page
    assert "disabled data-i18n=\"i2stream_logmonitor_install\"" in page


def test_logmonitor_contract_parses_preflight_and_job_without_secret_fields():
    result = _run_contract_case(
        """
const preflight = c.parseLogmonitorPreflight({
  preflight_id: 'pf-123', target_ip: '10.1.2.3',
  agent_base_url: 'http://10.2.3.4:8642', agent_back_url: 'http://10.2.3.4:50091',
  expires_at: '2026-09-14T12:00:00Z',
  checks: [
    {name: 'ssh', status: 'passed', message: 'connected'},
    {name: 'docker', status: 'passed', message: 'available'}
  ]
});
const job = c.parseLogmonitorInstallation({
  job_id: 'job-123', target_ip: '10.1.2.3', status: 'running', stage: 'loading_image',
  message: 'Loading image', checks: [{name: 'sha256', status: 'passed', message: 'matched'}],
  created_at: '2026-09-14T11:00:00Z', updated_at: '2026-09-14T11:01:00Z'
}, true);
output = {preflight, job};
"""
    )
    assert result == {
        "preflight": {
            "preflightId": "pf-123",
            "targetIp": "10.1.2.3",
            "agentBaseUrl": "http://10.2.3.4:8642",
            "agentBackUrl": "http://10.2.3.4:50091",
            "checks": [
                {"name": "ssh", "status": "passed", "message": "connected"},
                {"name": "docker", "status": "passed", "message": "available"},
            ],
            "expiresAt": "2026-09-14T12:00:00Z",
            "passed": True,
        },
        "job": {
            "jobId": "job-123",
            "status": "running",
            "stage": "loading_image",
            "message": "Loading image",
            "checks": [{"name": "sha256", "status": "passed", "message": "matched"}],
            "targetIp": "10.1.2.3",
            "createdAt": "2026-09-14T11:00:00Z",
            "updatedAt": "2026-09-14T11:01:00Z",
        },
    }


def test_logmonitor_payload_keeps_credentials_exact_omits_blanks_and_validates_runtime_paths():
    result = _run_contract_case(
        """
const raw = {
  target_ip: ' 10.1.2.3 ', ssh_port: '22', ssh_username: ' root ',
  ssh_password: ' password with spaces ', STREAM_LOG_MONITOR: '1',
  MCP_IADEBUG_USER: ' root ', ACTIVE_HOME: ' /root/ia ',
  STREAM_HOME: ' /root/i2stream ', STREAM_DATA_HOME: ' /var/iadata '
};
const payload = c.normalizeLogmonitorPayload(raw);
const keyOnly = c.normalizeLogmonitorPayload({
  ...raw, ssh_password: '', ssh_private_key: '-----BEGIN OPENSSH PRIVATE KEY-----\\nkey material\\n-----END OPENSSH PRIVATE KEY-----\\n'
});
let relativePathRejected = false;
try { c.normalizeLogmonitorPayload({...raw, ACTIVE_HOME: 'relative/path'}); }
catch (error) { relativePathRejected = error.name === 'TypeError'; }
let missingCredentialsRejected = false;
try { c.normalizeLogmonitorPayload({...raw, ssh_password: '', ssh_private_key: ''}); }
catch (error) { missingCredentialsRejected = error.name === 'TypeError'; }
output = {payload, keyOnly, relativePathRejected, missingCredentialsRejected};
"""
    )
    assert result == {
        "payload": {
            "target_ip": "10.1.2.3",
            "ssh_username": "root",
            "ssh_password": " password with spaces ",
            "MCP_IADEBUG_USER": "root",
            "ACTIVE_HOME": "/root/ia",
            "STREAM_HOME": "/root/i2stream",
            "STREAM_DATA_HOME": "/var/iadata",
            "ssh_port": 22,
            "STREAM_LOG_MONITOR": "1",
        },
        "keyOnly": {
            "target_ip": "10.1.2.3",
            "ssh_username": "root",
            "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nkey material\n-----END OPENSSH PRIVATE KEY-----\n",
            "MCP_IADEBUG_USER": "root",
            "ACTIVE_HOME": "/root/ia",
            "STREAM_HOME": "/root/i2stream",
            "STREAM_DATA_HOME": "/var/iadata",
            "ssh_port": 22,
            "STREAM_LOG_MONITOR": "1",
        },
        "relativePathRejected": True,
        "missingCredentialsRejected": True,
    }


def test_logmonitor_private_key_reader_preserves_text_and_enforces_16kib_limit():
    result = _run_contract_case(
        """
let reads = 0;
const content = '  -----BEGIN OPENSSH PRIVATE KEY-----\\nkey\\n-----END OPENSSH PRIVATE KEY-----\\n';
const privateKey = await c.readLogmonitorPrivateKey({
  size: 16384,
  text: async () => { reads += 1; return content; }
});
let oversizedRejected = false;
try {
  await c.readLogmonitorPrivateKey({
    size: 16385,
    text: async () => { reads += 1; return 'must not be read'; }
  });
} catch (error) {
  oversizedRejected = error.name === 'TypeError';
}
output = {privateKey, reads, oversizedRejected};
"""
    )
    assert result == {
        "privateKey": "  -----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
        "reads": 1,
        "oversizedRejected": True,
    }


def test_logmonitor_credentials_are_cleared_together():
    result = _run_contract_case(
        """
const controls = {
  i2streamLogmonitorSshPassword: {value: 'secret'},
  i2streamLogmonitorSshPrivateKey: {value: 'C:\\fakepath\\id_ed25519'}
};
document.getElementById = id => controls[id] || null;
c.clearLogmonitorCredentials();
output = {
  password: controls.i2streamLogmonitorSshPassword.value,
  privateKey: controls.i2streamLogmonitorSshPrivateKey.value
};
"""
    )
    assert result == {"password": "", "privateKey": ""}


def test_logmonitor_secret_is_redacted_and_never_persisted_or_put_in_url():
    result = _run_contract_case(
        """
output = {
  redacted: c.redactLogmonitorText('SSH failed for secret-value at target', 'secret-value'),
  unchanged: c.redactLogmonitorText('SSH failed at target', 'secret-value')
};
"""
    )
    assert result == {
        "redacted": "SSH failed for •••••• at target",
        "unchanged": "SSH failed at target",
    }
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "localStorage" not in source
    assert "console.log" not in source
    assert "logmonitor/preflight?" not in source
    assert "logmonitor/installations?" not in source
    assert "JSON.stringify(payload)" in source
    state_definition = source[source.index("const _i2streamState = {"):source.index("};", source.index("const _i2streamState = {"))]
    assert "privateKey" not in state_definition
    assert source.count("payload = await _i2LogmonitorFormPayload();") == 2
    install_start = source.index("async function installI2StreamLogmonitor()")
    install_source = source[install_start:source.index("function _i2RenderFailure", install_start)]
    assert install_source.index("await api(`${I2STREAM_API}/logmonitor/installations`") < install_source.index("_i2ClearLogmonitorCredentials();")
    assert "logmonitorForm.addEventListener('input', _i2InvalidateLogmonitorPreflight)" in source
    assert "logmonitorForm.addEventListener('change', _i2InvalidateLogmonitorPreflight)" in source


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
  if (url.includes('/file-beta?')) throw new Error('backend unavailable');
  return {code: 0, status: 'success'};
};
const deletion = await c.deleteKnowledgeFiles([
  {fileId: 'file-alpha', displayName: 'Alpha'},
  {fileId: 'file-beta', displayName: 'Beta'},
], 'team docs');
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
                "url": "/api/i2stream-console/knowledge/files/file-alpha?collection_name=team+docs",
                "method": "DELETE",
            },
            {
                "url": "/api/i2stream-console/knowledge/files/file-beta?collection_name=team+docs",
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


def test_configured_knowledge_loads_default_collection_then_its_files():
    result = _run_contract_case(
        """
const controls = {
  i2streamKnowledgeConfigStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeVectorHost: {value: ''},
  i2streamKnowledgeRagMcpUrl: {value: ''},
  i2streamKnowledgeConfigState: {className: '', textContent: '', dataset: {}},
  i2streamKnowledgeUnconfigured: {hidden: true},
  i2streamKnowledgeConfig: {querySelectorAll: () => []},
  i2streamKnowledgeCollections: {hidden: true},
  i2streamKnowledgeCollectionSelect: {disabled: false, innerHTML: '', value: ''},
  i2streamKnowledgeCollectionName: {disabled: false, value: ''},
  i2streamKnowledgeCollectionCreateBtn: {disabled: false},
  i2streamKnowledgeFile: {disabled: false},
  i2streamKnowledgeUploadBtn: {disabled: false},
  i2streamKnowledgeBatchActions: {hidden: false},
  i2streamKnowledgeSelectAll: {checked: false, indeterminate: false, disabled: false},
  i2streamKnowledgeSelectionCount: {textContent: ''},
  i2streamKnowledgeDeleteSelected: {disabled: false},
  i2streamKnowledgeList: {innerHTML: ''}
};
document.getElementById = id => controls[id] || null;
const calls = [];
sandbox.api = async url => {
  calls.push(url);
  if (url.endsWith('/knowledge/config')) return {
    code: 0, status: 'success', configuration: {
      configured: true, vector_search_host: 'http://rag.test:8900',
      rag_service_mcp_url: 'http://rag.test:8900/mcp', updated_at: '2026-09-16T10:00:00Z'
    }
  };
  if (url.endsWith('/knowledge/collections')) return {
    code: 0, status: 'success', total: 2, collections: [
      {name: 'archive', points_count: 1, vectors_count: 1, status: 'green', schema: 'v1', is_default: false},
      {name: 'documents', points_count: 2, vectors_count: 2, status: 'green', schema: 'v1', is_default: true}
    ]
  };
  return {code: 0, status: 'success', files: []};
};
const loaded = await c.loadI2StreamKnowledge();
output = {loaded, calls, selected: controls.i2streamKnowledgeCollectionSelect.value};
"""
    )
    assert result == {
        "loaded": True,
        "calls": [
            "/api/i2stream-console/knowledge/config",
            "/api/i2stream-console/knowledge/collections",
            "/api/i2stream-console/knowledge/files?collection_name=documents",
        ],
        "selected": "documents",
    }


def test_upload_polling_and_delete_keep_the_selected_collection():
    result = _run_contract_case(
        """
const files = [
  {name: 'guide.md', size: 12},
  {name: 'manual.pdf', size: 24},
];
const controls = {
  i2streamKnowledgeConfigStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeVectorHost: {value: ''},
  i2streamKnowledgeRagMcpUrl: {value: ''},
  i2streamKnowledgeConfigState: {className: '', textContent: '', dataset: {}},
  i2streamKnowledgeUnconfigured: {hidden: true},
  i2streamKnowledgeConfig: {querySelectorAll: () => []},
  i2streamKnowledgeCollections: {hidden: true},
  i2streamKnowledgeCollectionSelect: {disabled: false, innerHTML: '', value: ''},
  i2streamKnowledgeCollectionName: {disabled: false, value: ''},
  i2streamKnowledgeCollectionCreateBtn: {disabled: false},
  i2streamKnowledgeFile: {disabled: false, files, value: 'selected-files'},
  i2streamKnowledgeFileLabel: {textContent: ''},
  i2streamKnowledgeUploadBtn: {disabled: false},
  i2streamKnowledgeBatchActions: {hidden: false},
  i2streamKnowledgeSelectAll: {checked: false, indeterminate: false, disabled: false},
  i2streamKnowledgeSelectionCount: {textContent: ''},
  i2streamKnowledgeDeleteSelected: {disabled: false},
  i2streamKnowledgeList: {innerHTML: ''}
};
document.getElementById = id => controls[id] || null;
sandbox.MAX_UPLOAD_BYTES = 1024;
sandbox.FormData = class {
  constructor() { this.values = []; }
  append(name, value) {
    this.values.push([name, value && value.name ? value.name : value]);
  }
};
sandbox.showConfirmDialog = async () => true;
const operationCalls = [];
let switchWhileUploading;
let uploadCount = 0;
sandbox.api = async (url, options = {}) => {
  if (url.endsWith('/knowledge/config')) return {
    code: 0, status: 'success', configuration: {
      configured: true, vector_search_host: 'http://rag.test:8900',
      rag_service_mcp_url: 'http://rag.test:8900/mcp', updated_at: '2026-09-16T10:00:00Z'
    }
  };
  if (url.endsWith('/knowledge/collections')) return {
    code: 0, status: 'success', total: 2, collections: [
      {name: 'archive', points_count: 1, vectors_count: 1, status: 'green', schema: 'v1', is_default: false},
      {name: 'documents', points_count: 2, vectors_count: 2, status: 'green', schema: 'v1', is_default: true}
    ]
  };
  if (options.method === 'POST') {
    uploadCount += 1;
    operationCalls.push({url, method: options.method, form: options.body.values});
    switchWhileUploading = await sandbox.selectI2StreamKnowledgeCollection('archive');
    return {code: 0, status: 'success', file: {task_id: `task-${uploadCount}`}};
  }
  if (url.includes('/knowledge/tasks/')) {
    operationCalls.push({url, method: 'GET'});
    return {code: 0, status: 'success', task: {status: 'completed', terminal: true}};
  }
  if (options.method === 'DELETE') {
    operationCalls.push({url, method: options.method});
    return {code: 0, status: 'success'};
  }
  return {code: 0, status: 'success', files: []};
};
await c.loadI2StreamKnowledge();
await sandbox.uploadI2StreamKnowledge({preventDefault() {}});
await sandbox.deleteI2StreamKnowledge('file/a', 'Guide');
output = {
  operationCalls,
  switchWhileUploading,
  inputValue: controls.i2streamKnowledgeFile.value,
  inputLabel: controls.i2streamKnowledgeFileLabel.textContent,
};
"""
    )
    assert result == {
        "operationCalls": [
            {
                "url": "/api/i2stream-console/knowledge/files",
                "method": "POST",
                "form": [["file", "guide.md"], ["collection_name", "documents"]],
            },
            {
                "url": "/api/i2stream-console/knowledge/tasks/task-1?collection_name=documents",
                "method": "GET",
            },
            {
                "url": "/api/i2stream-console/knowledge/files",
                "method": "POST",
                "form": [["file", "manual.pdf"], ["collection_name", "documents"]],
            },
            {
                "url": "/api/i2stream-console/knowledge/tasks/task-2?collection_name=documents",
                "method": "GET",
            },
            {
                "url": "/api/i2stream-console/knowledge/files/file%2Fa?collection_name=documents",
                "method": "DELETE",
            },
        ],
        "switchWhileUploading": False,
        "inputValue": "",
        "inputLabel": "i2stream_choose_file",
    }


def test_multi_file_upload_continues_after_one_file_fails_and_reports_summary():
    result = _run_contract_case(
        """
const files = [
  {name: 'broken.md', size: 12},
  {name: 'guide.pdf', size: 24},
];
const controls = {
  i2streamKnowledgeConfigStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeVectorHost: {value: ''},
  i2streamKnowledgeRagMcpUrl: {value: ''},
  i2streamKnowledgeConfigState: {className: '', textContent: '', dataset: {}},
  i2streamKnowledgeUnconfigured: {hidden: true},
  i2streamKnowledgeConfig: {querySelectorAll: () => []},
  i2streamKnowledgeCollections: {hidden: true},
  i2streamKnowledgeCollectionSelect: {disabled: false, innerHTML: '', value: ''},
  i2streamKnowledgeCollectionName: {disabled: false, value: ''},
  i2streamKnowledgeCollectionCreateBtn: {disabled: false},
  i2streamKnowledgeFile: {disabled: false, files, value: 'selected-files'},
  i2streamKnowledgeFileLabel: {textContent: ''},
  i2streamKnowledgeUploadBtn: {disabled: false},
  i2streamKnowledgeBatchActions: {hidden: false},
  i2streamKnowledgeSelectAll: {checked: false, indeterminate: false, disabled: false},
  i2streamKnowledgeSelectionCount: {textContent: ''},
  i2streamKnowledgeDeleteSelected: {disabled: false},
  i2streamKnowledgeList: {innerHTML: ''}
};
document.getElementById = id => controls[id] || null;
sandbox.MAX_UPLOAD_BYTES = 1024;
sandbox.FormData = class {
  constructor() { this.values = []; }
  append(name, value) {
    this.values.push([name, value && value.name ? value.name : value]);
  }
};
sandbox.t = key => ({
  uploading: 'Uploading', uploaded: 'Uploaded', upload_failed: 'Upload failed: ',
  i2stream_choose_file: 'Choose files'
})[key] || key;
const calls = [];
let listLoads = 0;
sandbox.api = async (url, options = {}) => {
  if (url.endsWith('/knowledge/config')) return {
    code: 0, status: 'success', configuration: {
      configured: true, vector_search_host: 'http://rag.test:8900',
      rag_service_mcp_url: 'http://rag.test:8900/mcp', updated_at: '2026-09-16T10:00:00Z'
    }
  };
  if (url.endsWith('/knowledge/collections')) return {
    code: 0, status: 'success', total: 1, collections: [
      {name: 'documents', points_count: 2, vectors_count: 2, status: 'green', schema: 'v1', is_default: true}
    ]
  };
  if (options.method === 'POST') {
    calls.push(options.body.values);
    if (options.body.values[0][1] === 'broken.md') throw new Error('index rejected');
    return {code: 0, status: 'success', file: {task_id: 'task-guide'}};
  }
  if (url.includes('/knowledge/tasks/')) {
    return {code: 0, status: 'success', task: {status: 'completed', terminal: true}};
  }
  if (url.includes('/knowledge/files?')) listLoads += 1;
  return {code: 0, status: 'success', files: []};
};
await c.loadI2StreamKnowledge();
listLoads = 0;
await sandbox.uploadI2StreamKnowledge({preventDefault() {}});
output = {
  calls,
  listLoads,
  status: controls.i2streamKnowledgeStatus.textContent,
  statusState: controls.i2streamKnowledgeStatus.dataset.kind,
  inputValue: controls.i2streamKnowledgeFile.value,
};
"""
    )
    assert result == {
        "calls": [
            [["file", "broken.md"], ["collection_name", "documents"]],
            [["file", "guide.pdf"], ["collection_name", "documents"]],
        ],
        "listLoads": 1,
        "status": "Uploaded 1/2; Upload failed: broken.md: index rejected",
        "statusState": "error",
        "inputValue": "",
    }


def test_multi_file_upload_rejects_oversized_batch_before_first_request():
    result = _run_contract_case(
        """
const controls = {
  i2streamKnowledgeStatus: {textContent: '', dataset: {}},
  i2streamKnowledgeFile: {
    disabled: false,
    files: [
      {name: 'small.md', size: 12},
      {name: 'large.pdf', size: 2048},
    ],
    value: 'selected-files'
  }
};
document.getElementById = id => controls[id] || null;
sandbox.MAX_UPLOAD_BYTES = 1024;
sandbox._uploadTooLargeMessage = file => `${file.name} exceeds the upload limit.`;
let calls = 0;
sandbox.api = async (url, options = {}) => {
  if (url.endsWith('/knowledge/config')) return {
    code: 0, status: 'success', configuration: {
      configured: true, vector_search_host: 'http://rag.test:8900',
      rag_service_mcp_url: 'http://rag.test:8900/mcp', updated_at: '2026-09-16T10:00:00Z'
    }
  };
  if (url.endsWith('/knowledge/collections')) return {
    code: 0, status: 'success', total: 1, collections: [
      {name: 'documents', points_count: 0, vectors_count: 0, status: 'green', schema: 'v1', is_default: true}
    ]
  };
  if (options.method === 'POST') calls += 1;
  return {code: 0, status: 'success', files: []};
};
await c.loadI2StreamKnowledge();
await sandbox.uploadI2StreamKnowledge({preventDefault() {}});
output = {
  calls,
  status: controls.i2streamKnowledgeStatus.textContent,
  inputValue: controls.i2streamKnowledgeFile.value,
};
"""
    )
    assert result == {
        "calls": 0,
        "status": "upload_failedlarge.pdf exceeds the upload limit.",
        "inputValue": "selected-files",
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
        "c.parseKnowledgeConfiguration({code:0,status:'success',configuration:{configured:false,vector_search_host:null,rag_service_mcp_url:'',updated_at:null}})",
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
        "/nodes",
    ):
        assert route in source
    assert "FormData" in source
    assert "method: 'DELETE'" in source
    assert "client_id:" in source
    assert "files.some(file => file.size > MAX_UPLOAD_BYTES)" in source
    assert "requestGeneration: {knowledge: 0, reports: 0, history: 0, nodes: 0}" in source
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


def test_online_nodes_control_uses_an_external_accessible_surface():
    toolsets = INDEX.index('id="composerToolsetsWrap"')
    trigger = INDEX.index('id="onlineNodesButton"')
    composer_left_end = INDEX.index('</div>', INDEX.index('</div>', toolsets) + 6)
    surface = INDEX.index('id="onlineNodesDropdown"')
    toast = INDEX.index('id="toast"')

    assert toolsets < trigger < composer_left_end < toast < surface
    assert 'aria-haspopup="dialog"' in INDEX[trigger : trigger + 500]
    assert 'aria-expanded="false"' in INDEX[trigger : trigger + 500]
    assert 'aria-controls="onlineNodesDropdown"' in INDEX[trigger : trigger + 500]
    assert 'role="dialog"' in INDEX[surface : surface + 500]
    assert 'aria-live="polite"' in INDEX[surface : surface + 500]


def test_online_nodes_contract_parser_preserves_online_and_offline_records():
    result = _run_contract_case(
        """
const snapshot = c.parseNodes({
  offline_after_seconds: 90,
  nodes: [
    {ip: '10.1.1.10', online: true, first_seen_at: '2026-08-01T08:00:00Z', last_seen_at: '2026-08-31T10:20:30Z'},
    {ip: '10.1.1.11', online: false, first_seen_at: '2026-08-02T09:00:00Z', last_seen_at: '2026-08-31T10:18:00Z'}
  ]
});
output = snapshot;
"""
    )
    assert result == {
        "offlineAfterSeconds": 90,
        "nodes": [
            {
                "ip": "10.1.1.10",
                "online": True,
                "firstSeenAt": "2026-08-01T08:00:00Z",
                "lastSeenAt": "2026-08-31T10:20:30Z",
            },
            {
                "ip": "10.1.1.11",
                "online": False,
                "firstSeenAt": "2026-08-02T09:00:00Z",
                "lastSeenAt": "2026-08-31T10:18:00Z",
            },
        ],
    }


@pytest.mark.parametrize(
    "expression",
    [
        "c.parseNodes({offline_after_seconds:90,nodes:{}})",
        "c.parseNodes({offline_after_seconds:0,nodes:[]})",
        "c.parseNodes({offline_after_seconds:90,nodes:[{ip:'',online:true,first_seen_at:'2026-08-01T10:20:30Z',last_seen_at:'2026-08-31T10:20:30Z'}]})",
        "c.parseNodes({offline_after_seconds:90,nodes:[{ip:'10.1.1.10',online:'yes',first_seen_at:'2026-08-01T10:20:30Z',last_seen_at:'2026-08-31T10:20:30Z'}]})",
        "c.parseNodes({offline_after_seconds:90,nodes:[{ip:'10.1.1.10',online:true,first_seen_at:'2026-08-01T10:20:30Z',last_seen_at:'not-a-date'}]})",
        "c.parseNodes({offline_after_seconds:90,nodes:[{ip:'10.1.1.10',online:true,first_seen_at:'not-a-date',last_seen_at:'2026-08-31T10:20:30Z'}]})",
        "c.parseNodes({offline_after_seconds:90,nodes:[{ip:'10.1.1.10',online:true,last_seen_at:'2026-08-31T10:20:30Z'}]})",
        "c.parseNodes({offline_after_seconds:90,nodes:[{ip:'10.1.1.10',online:true,first_seen_at:'2026-09-01T10:20:30Z',last_seen_at:'2026-08-31T10:20:30Z'}]})",
    ],
)
def test_online_nodes_contract_rejects_malformed_payloads(expression):
    result = _run_contract_case(
        f"""
try {{ {expression}; output = {{threw: false}}; }}
catch (error) {{ output = {{threw: true, name: error.name}}; }}
"""
    )
    assert result == {"threw": True, "name": "TypeError"}


def test_online_nodes_request_failure_keeps_last_successful_node_statuses():
    result = _run_contract_case(
        """
let fail = false;
sandbox.api = async () => {
  if (fail) throw new Error('backend unavailable');
  return {
    offline_after_seconds: 90,
    nodes: [
      {ip: '10.1.1.10', online: true, first_seen_at: '2026-08-01T08:00:00Z', last_seen_at: '2026-08-31T10:20:30Z'},
      {ip: '10.1.1.11', online: false, first_seen_at: '2026-08-02T09:00:00Z', last_seen_at: '2026-08-31T10:18:00Z'}
    ]
  };
};
await c.loadOnlineNodes();
fail = true;
await c.loadOnlineNodes();
output = c.getOnlineNodesState();
"""
    )
    assert result == {
        "available": False,
        "nodes": [
            {"ip": "10.1.1.10", "online": True, "firstSeenAt": "2026-08-01T08:00:00Z", "lastSeenAt": "2026-08-31T10:20:30Z"},
            {"ip": "10.1.1.11", "online": False, "firstSeenAt": "2026-08-02T09:00:00Z", "lastSeenAt": "2026-08-31T10:18:00Z"},
        ],
    }


def test_online_nodes_reuses_the_inflight_request_instead_of_stacking_polls():
    result = _run_contract_case(
        """
let calls = 0;
let resolveRequest;
sandbox.api = async () => {
  calls += 1;
  return await new Promise(resolve => { resolveRequest = resolve; });
};
const first = c.loadOnlineNodes();
const second = c.loadOnlineNodes();
await Promise.resolve();
const callsWhilePending = calls;
resolveRequest({
  offline_after_seconds: 90,
  nodes: [{ip: '10.1.1.10', online: true, first_seen_at: '2026-08-01T08:00:00Z', last_seen_at: '2026-08-31T10:20:30Z'}]
});
await Promise.all([first, second]);
output = {callsWhilePending, calls, state: c.getOnlineNodesState()};
"""
    )
    assert result == {
        "callsWhilePending": 1,
        "calls": 1,
        "state": {
            "available": True,
            "nodes": [
                {"ip": "10.1.1.10", "online": True, "firstSeenAt": "2026-08-01T08:00:00Z", "lastSeenAt": "2026-08-31T10:20:30Z"}
            ],
        },
    }


def test_node_deletion_request_encodes_ipv6_and_uses_delete():
    result = _run_contract_case(
        """
const calls = [];
sandbox.api = async (url, options) => { calls.push({url, options}); };
await c.deleteNodeRequest('2001:db8::1');
output = calls;
"""
    )
    assert result == [
        {
            "url": "/api/i2stream-console/nodes/2001%3Adb8%3A%3A1",
            "options": {"method": "DELETE"},
        }
    ]


def test_confirmed_node_deletion_refreshes_and_removes_the_node_snapshot():
    result = _run_contract_case(
        """
const calls = [];
let deleted = false;
let confirmations = 0;
sandbox.showConfirmDialog = async () => { confirmations += 1; return true; };
sandbox.api = async (url, options = {}) => {
  calls.push({url, method: options.method || 'GET'});
  if (options.method === 'DELETE') { deleted = true; return null; }
  return {
    offline_after_seconds: 90,
    nodes: deleted ? [] : [
      {ip: '10.1.1.10', online: true, first_seen_at: '2026-08-01T08:00:00Z', last_seen_at: '2026-08-31T10:20:30Z'}
    ]
  };
};
await c.loadOnlineNodes();
await c.deleteI2StreamNode('10.1.1.10');
output = {confirmations, calls, state: c.getOnlineNodesState()};
"""
    )
    assert result == {
        "confirmations": 1,
        "calls": [
            {"url": "/api/i2stream-console/nodes", "method": "GET"},
            {"url": "/api/i2stream-console/nodes/10.1.1.10", "method": "DELETE"},
            {"url": "/api/i2stream-console/nodes", "method": "GET"},
        ],
        "state": {"available": True, "nodes": []},
    }


def test_concurrent_node_poll_does_not_report_successful_delete_as_refresh_failure():
    result = _run_contract_case(
        """
const status = {textContent: '', dataset: {}, focus: () => {}};
sandbox.document.getElementById = id => id === 'i2streamNodesStatus' ? status : null;
let deleted = false;
let signalRefreshStarted;
let finishRefresh;
const refreshStarted = new Promise(resolve => { signalRefreshStarted = resolve; });
const refreshResponse = new Promise(resolve => { finishRefresh = resolve; });
sandbox.showConfirmDialog = async () => true;
sandbox.api = async (url, options = {}) => {
  if (options.method === 'DELETE') { deleted = true; return null; }
  if (deleted) { signalRefreshStarted(); return refreshResponse; }
  return {
    offline_after_seconds: 90,
    nodes: [
      {ip: '10.1.1.10', online: true, first_seen_at: '2026-08-01T08:00:00Z', last_seen_at: '2026-08-31T10:20:30Z'}
    ]
  };
};
await c.loadOnlineNodes();
const deletion = c.deleteI2StreamNode('10.1.1.10');
await refreshStarted;
const poll = c.loadI2StreamNodes(false);
finishRefresh({offline_after_seconds: 90, nodes: []});
await Promise.all([deletion, poll]);
output = {message: status.textContent, state: c.getOnlineNodesState()};
"""
    )
    assert result == {
        "message": "i2stream_node_deleted",
        "state": {"available": True, "nodes": []},
    }


@pytest.mark.parametrize(
    ("confirmation", "delete_fails", "refresh_fails", "expected_calls", "available"),
    [
        (False, False, False, 1, True),
        (True, True, False, 2, True),
        (True, False, True, 3, False),
    ],
)
def test_node_deletion_cancel_and_failure_paths_preserve_truthful_state(
    confirmation, delete_fails, refresh_fails, expected_calls, available
):
    result = _run_contract_case(
        f"""
let calls = 0;
let deleted = false;
sandbox.showConfirmDialog = async () => {str(confirmation).lower()};
sandbox.api = async (url, options = {{}}) => {{
  calls += 1;
  if (options.method === 'DELETE') {{
    if ({str(delete_fails).lower()}) throw new Error('delete failed');
    deleted = true;
    return null;
  }}
  if (deleted && {str(refresh_fails).lower()}) throw new Error('refresh failed');
  return {{
    offline_after_seconds: 90,
    nodes: deleted ? [] : [
      {{ip: '10.1.1.10', online: true, first_seen_at: '2026-08-01T08:00:00Z', last_seen_at: '2026-08-31T10:20:30Z'}}
    ]
  }};
}};
await c.loadOnlineNodes();
await c.deleteI2StreamNode('10.1.1.10');
output = {{calls, state: c.getOnlineNodesState()}};
"""
    )
    expected_nodes = [] if confirmation and not delete_fails else [
        {
            "ip": "10.1.1.10",
            "online": True,
            "firstSeenAt": "2026-08-01T08:00:00Z",
            "lastSeenAt": "2026-08-31T10:20:30Z",
        }
    ]
    assert result == {
        "calls": expected_calls,
        "state": {"available": available, "nodes": expected_nodes},
    }


def test_online_nodes_lifecycle_has_visibility_polling_and_accessible_close_paths():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "const I2STREAM_NODES_POLL_INTERVAL_MS = 30_000" in source
    assert "visibilitychange" in source
    assert "document.visibilityState === 'visible'" in source
    assert "event.key === 'Escape'" in source
    assert "onlineNodesDropdown.contains(event.target)" in source
    assert "button.offsetParent === null" in source
    assert "closeModelDropdown" in source
    assert "closeReasoningDropdown" in source
    assert "closeToolsetsDropdown" in source
    assert "closeProfileDropdown" in source
    assert "closeWsDropdown" in source
    assert "MutationObserver" in source
    assert ".online-nodes-list{max-height:" in STYLE
    assert ".composer-footer.cf-burger .composer-left > .online-nodes-wrap" in STYLE
    assert ".composer-left > .online-nodes-wrap{display:none!important;}" in STYLE
