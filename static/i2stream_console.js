// Native i2Stream Console surface. The backend remains the only owner of
// global knowledge, reports, node status, and browser-plugin conversation history.

const I2STREAM_API = '/api/i2stream-console';
const I2STREAM_HISTORY_PAGE_SIZE = 30;
const I2STREAM_NODES_POLL_INTERVAL_MS = 30_000;
const I2STREAM_INSTALL_POLL_INTERVAL_MS = 2_000;
const I2STREAM_PRIVATE_KEY_MAX_BYTES = 16 * 1024;
const I2STREAM_SECTIONS = new Set(['knowledge', 'reports', 'history', 'nodes', 'logmonitor']);
const I2STREAM_CLIENT_ID_RE = /^[A-Za-z0-9_-]{16,128}$/;
const _i2streamState = {
  section: 'knowledge',
  loaded: {knowledge: false, reports: false, history: false, nodes: false, logmonitor: true},
  knowledgeFiles: [],
  knowledgeCollections: [],
  selectedKnowledgeCollection: null,
  knowledgeConfiguration: null,
  knowledgeConfigBusy: false,
  knowledgeCollectionBusy: false,
  knowledgeUploadInFlight: false,
  selectedKnowledgeFileIds: new Set(),
  knowledgeDeleteInFlight: false,
  reports: [],
  conversations: [],
  historyClientId: null,
  nextBefore: null,
  selectedConversationKey: null,
  selectedReportToken: null,
  knowledgeTaskGeneration: 0,
  requestGeneration: {knowledge: 0, reports: 0, history: 0, nodes: 0},
  historyDetailGeneration: 0,
  bindingsReady: false,
  nodes: [],
  nodesAvailable: null,
  nodesOfflineAfterSeconds: null,
  nodesRequestInFlight: null,
  nodesPollTimer: null,
  nodesFooterObserver: null,
  nodesBindingsReady: false,
  nodeDeleteInFlight: false,
  logmonitorPreflight: null,
  logmonitorRequestGeneration: 0,
  logmonitorJobId: null,
  logmonitorPollTimer: null,
};

function _i2ContractObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value;
}

function _i2ContractString(value, label, allowEmpty = false) {
  if (typeof value !== 'string' || (!allowEmpty && !value)) {
    throw new TypeError(`${label} must be ${allowEmpty ? 'a string' : 'a non-empty string'}`);
  }
  return value;
}

function _i2Success(value, label) {
  const payload = _i2ContractObject(value, label);
  if (payload.code !== 0 || payload.status !== 'success') {
    throw new TypeError(`${label} must be a successful i2Stream response`);
  }
  return payload;
}

function _i2ConversationKey(clientId, conversationId) {
  return JSON.stringify([clientId, conversationId]);
}

function parseConversationPage(value) {
  const payload = _i2Success(value, 'conversation page');
  if (!Array.isArray(payload.conversations)) {
    throw new TypeError('conversation page conversations must be an array');
  }
  if (!Object.prototype.hasOwnProperty.call(payload, 'next_before') ||
      !(payload.next_before === null || (Number.isSafeInteger(payload.next_before) && payload.next_before > 0))) {
    throw new TypeError('conversation page next_before must be a positive safe integer or null');
  }
  const conversations = payload.conversations.map((raw, index) => {
    const item = _i2ContractObject(raw, `conversation ${index}`);
    const clientId = _i2ContractString(item.client_id, `conversation ${index} client_id`);
    const conversationId = _i2ContractString(item.conversation_id, `conversation ${index} conversation_id`);
    if (!Number.isInteger(item.message_count) || item.message_count < 0) {
      throw new TypeError(`conversation ${index} message_count must be a non-negative integer`);
    }
    return {
      clientId,
      conversationId,
      conversationKey: _i2ConversationKey(clientId, conversationId),
      messageCount: item.message_count,
      latestMessageAt: _i2ContractString(item.latest_message_at, `conversation ${index} latest_message_at`),
      preview: _i2ContractString(item.preview, `conversation ${index} preview`, true),
    };
  });
  return {conversations, nextBefore: payload.next_before};
}

function parseConversationDetail(value) {
  const payload = _i2Success(value, 'conversation detail');
  const conversationId = _i2ContractString(payload.conversation_id, 'conversation detail conversation_id');
  if (!Array.isArray(payload.messages)) {
    throw new TypeError('conversation detail messages must be an array');
  }
  const messages = payload.messages.map((raw, index) => {
    const item = _i2ContractObject(raw, `message ${index}`);
    if (!Number.isSafeInteger(item.id) || item.id < 1) {
      throw new TypeError(`message ${index} id must be a positive safe integer`);
    }
    return {
      id: item.id,
      role: _i2ContractString(item.role, `message ${index} role`),
      content: _i2ContractString(item.content, `message ${index} content`, true),
      createdAt: _i2ContractString(item.created_at, `message ${index} created_at`),
    };
  });
  return {conversationId, messages};
}

function parseKnowledgeFiles(value, expectedCollectionName) {
  const payload = _i2Success(value, 'knowledge files');
  const collectionName = _i2ContractString(expectedCollectionName, 'selected knowledge collection');
  if (!Array.isArray(payload.files)) throw new TypeError('knowledge files must be an array');
  return payload.files.map((raw, index) => {
    const item = _i2ContractObject(raw, `knowledge file ${index}`);
    const itemCollectionName = _i2ContractString(
      item.collection_name,
      `knowledge file ${index} collection_name`,
    );
    if (itemCollectionName !== collectionName) {
      throw new TypeError(`knowledge file ${index} belongs to a different collection`);
    }
    if (!(item.file_size === null || (Number.isSafeInteger(item.file_size) && item.file_size >= 0))) {
      throw new TypeError(`knowledge file ${index} file_size must be a non-negative safe integer or null`);
    }
    if (!(item.total_chunks === null || (Number.isSafeInteger(item.total_chunks) && item.total_chunks >= 0))) {
      throw new TypeError(`knowledge file ${index} total_chunks must be a non-negative safe integer or null`);
    }
    return {
      fileId: _i2ContractString(item.file_id, `knowledge file ${index} file_id`),
      displayName: _i2ContractString(item.display_name, `knowledge file ${index} display_name`),
      fileType: _i2ContractString(item.file_type, `knowledge file ${index} file_type`, true),
      fileSize: item.file_size,
      uploadTime: _i2ContractString(item.upload_time, `knowledge file ${index} upload_time`, true),
      totalChunks: item.total_chunks,
      collectionName: itemCollectionName,
    };
  });
}

function _i2NonNegativeSafeInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative safe integer`);
  }
  return value;
}

function parseKnowledgeCollections(value) {
  const payload = _i2Success(value, 'knowledge collections');
  if (!Array.isArray(payload.collections)) {
    throw new TypeError('knowledge collections must be an array');
  }
  const total = _i2NonNegativeSafeInteger(payload.total, 'knowledge collections total');
  if (total !== payload.collections.length) {
    throw new TypeError('knowledge collections total must match the collection count');
  }
  const names = new Set();
  let defaultCount = 0;
  const collections = payload.collections.map((raw, index) => {
    const item = _i2ContractObject(raw, `knowledge collection ${index}`);
    const name = _i2ContractString(item.name, `knowledge collection ${index} name`);
    if (names.has(name)) throw new TypeError(`knowledge collection ${index} name must be unique`);
    names.add(name);
    if (typeof item.is_default !== 'boolean') {
      throw new TypeError(`knowledge collection ${index} is_default must be a boolean`);
    }
    if (item.is_default) defaultCount += 1;
    return {
      name,
      pointsCount: _i2NonNegativeSafeInteger(
        item.points_count,
        `knowledge collection ${index} points_count`,
      ),
      vectorsCount: _i2NonNegativeSafeInteger(
        item.vectors_count,
        `knowledge collection ${index} vectors_count`,
      ),
      status: _i2ContractString(item.status, `knowledge collection ${index} status`),
      schema: _i2ContractString(item.schema, `knowledge collection ${index} schema`),
      isDefault: item.is_default,
    };
  });
  if (defaultCount > 1) throw new TypeError('knowledge collections must not contain multiple defaults');
  return collections;
}

function chooseKnowledgeCollection(collections, preferredName) {
  if (!Array.isArray(collections)) throw new TypeError('knowledge collections must be an array');
  if (collections.length === 0) throw new TypeError(_i2Text('i2stream_knowledge_no_collections'));
  if (preferredName !== null) {
    const preferred = _i2ContractString(preferredName, 'preferred knowledge collection');
    if (collections.some(collection => collection.name === preferred)) return preferred;
  }
  const defaultCollection = collections.find(collection => collection.isDefault);
  return defaultCollection ? defaultCollection.name : collections[0].name;
}

function knowledgeCollectionUrl(path, collectionName) {
  const query = new URLSearchParams({
    collection_name: _i2ContractString(collectionName, 'knowledge collection'),
  });
  return `${I2STREAM_API}${_i2ContractString(path, 'knowledge API path')}?${query.toString()}`;
}

function parseCreatedKnowledgeCollection(value) {
  const payload = _i2Success(value, 'created knowledge collection');
  const collection = _i2ContractObject(payload.collection, 'created knowledge collection payload');
  return {
    name: _i2ContractString(collection.collection_name, 'created knowledge collection name'),
    message: _i2ContractString(collection.message, 'created knowledge collection message'),
  };
}

async function createKnowledgeCollectionRequest(rawName) {
  const name = _i2ContractString(rawName, 'knowledge collection name').trim();
  if (!name) throw new TypeError(_i2Text('i2stream_knowledge_collection_name_required'));
  const created = parseCreatedKnowledgeCollection(await api(`${I2STREAM_API}/knowledge/collections`, {
    method: 'POST',
    body: JSON.stringify({collection_name: name}),
  }));
  if (created.name !== name) {
    throw new TypeError('created knowledge collection name does not match the request');
  }
  return created;
}

function parseKnowledgeConfiguration(value) {
  const payload = _i2Success(value, 'knowledge configuration');
  const raw = _i2ContractObject(payload.configuration, 'knowledge configuration payload');
  if (typeof raw.configured !== 'boolean') {
    throw new TypeError('knowledge configuration configured must be a boolean');
  }
  const vectorSearchHost = _i2ContractString(
    raw.vector_search_host,
    'knowledge configuration vector_search_host',
    !raw.configured,
  );
  const ragServiceMcpUrl = _i2ContractString(
    raw.rag_service_mcp_url,
    'knowledge configuration rag_service_mcp_url',
    !raw.configured,
  );
  const updatedAt = raw.updated_at === null
    ? null
    : _i2ContractString(raw.updated_at, 'knowledge configuration updated_at');
  if (raw.configured && (!vectorSearchHost || !ragServiceMcpUrl)) {
    throw new TypeError('configured knowledge service requires both URLs');
  }
  return {configured: raw.configured, vectorSearchHost, ragServiceMcpUrl, updatedAt};
}

function normalizeKnowledgeConfiguration(raw) {
  const values = _i2ContractObject(raw, 'knowledge configuration form');
  const vectorSearchHost = _i2ContractString(
    values.vector_search_host,
    'knowledge configuration vector_search_host',
  ).trim();
  const ragServiceMcpUrl = _i2ContractString(
    values.rag_service_mcp_url,
    'knowledge configuration rag_service_mcp_url',
  ).trim();
  if (!vectorSearchHost || !ragServiceMcpUrl) {
    throw new TypeError(_i2Text('i2stream_knowledge_config_invalid'));
  }
  return {
    vector_search_host: vectorSearchHost,
    rag_service_mcp_url: ragServiceMcpUrl,
  };
}

function parseKnowledgeConnectionCheck(value) {
  const configuration = parseKnowledgeConfiguration(value);
  const payload = _i2ContractObject(value, 'knowledge connection check');
  const checks = _i2ContractObject(payload.checks, 'knowledge connection checks');
  ['vector_search', 'rag_service_mcp'].forEach(name => {
    const check = _i2ContractObject(checks[name], `knowledge connection check ${name}`);
    if (check.status !== 'reachable') {
      throw new TypeError(`knowledge connection check ${name} is not reachable`);
    }
    _i2ContractString(check.url, `knowledge connection check ${name} url`);
  });
  return configuration;
}

function _i2KnowledgeProfileNames(value, label) {
  if (!Array.isArray(value)) throw new TypeError(`${label} must be an array`);
  return value.map((name, index) => _i2ContractString(name, `${label} ${index}`));
}

function parseKnowledgeMcpSync(value) {
  const payload = _i2Success(value, 'knowledge MCP sync');
  const mcp = _i2ContractObject(payload.mcp, 'knowledge MCP sync payload');
  if (typeof mcp.reload_required !== 'boolean') {
    throw new TypeError('knowledge MCP sync reload_required must be a boolean');
  }
  return {
    ragServiceMcpUrl: _i2ContractString(mcp.rag_service_mcp_url, 'knowledge MCP sync URL'),
    configuredProfiles: _i2KnowledgeProfileNames(
      mcp.configured_profiles,
      'knowledge MCP sync configured_profiles',
    ),
    missingProfiles: _i2KnowledgeProfileNames(
      mcp.missing_profiles,
      'knowledge MCP sync missing_profiles',
    ),
    reloadRequired: mcp.reload_required,
  };
}

async function saveKnowledgeConfigurationRequest(payload) {
  const values = normalizeKnowledgeConfiguration(payload);
  const configuration = parseKnowledgeConfiguration(await api(`${I2STREAM_API}/knowledge/config`, {
    method: 'PUT',
    body: JSON.stringify(values),
  }));
  if (!configuration.configured) {
    throw new TypeError('saved knowledge configuration must be configured');
  }
  try {
    const mcp = parseKnowledgeMcpSync(await api('/api/rag-service-mcp', {
      method: 'PUT',
      body: JSON.stringify({rag_service_mcp_url: configuration.ragServiceMcpUrl}),
    }));
    if (mcp.ragServiceMcpUrl !== configuration.ragServiceMcpUrl) {
      throw new TypeError('Hermes MCP sync returned a different RAG service URL');
    }
    return {configuration, mcp, mcpSyncError: null};
  } catch (error) {
    const mcpSyncError = error && typeof error.message === 'string'
      ? error
      : new Error(String(error));
    return {configuration, mcp: null, mcpSyncError};
  }
}

function parseNodes(value) {
  const payload = _i2ContractObject(value, 'nodes');
  if (!Number.isInteger(payload.offline_after_seconds) || payload.offline_after_seconds < 1) {
    throw new TypeError('nodes offline_after_seconds must be a positive integer');
  }
  if (!Array.isArray(payload.nodes)) throw new TypeError('nodes must be an array');
  const nodes = payload.nodes.map((raw, index) => {
    const item = _i2ContractObject(raw, `node ${index}`);
    const ip = _i2ContractString(item.ip, `node ${index} ip`);
    if (typeof item.online !== 'boolean') {
      throw new TypeError(`node ${index} online must be a boolean`);
    }
    const firstSeenAt = _i2ContractString(item.first_seen_at, `node ${index} first_seen_at`);
    const lastSeenAt = _i2ContractString(item.last_seen_at, `node ${index} last_seen_at`);
    if (!/(?:Z|\+00:00)$/.test(firstSeenAt) || Number.isNaN(Date.parse(firstSeenAt))) {
      throw new TypeError(`node ${index} first_seen_at must be an ISO 8601 UTC timestamp`);
    }
    if (!/(?:Z|\+00:00)$/.test(lastSeenAt) || Number.isNaN(Date.parse(lastSeenAt))) {
      throw new TypeError(`node ${index} last_seen_at must be an ISO 8601 UTC timestamp`);
    }
    if (Date.parse(firstSeenAt) > Date.parse(lastSeenAt)) {
      throw new TypeError(`node ${index} first_seen_at must not be later than last_seen_at`);
    }
    return {ip, online: item.online, firstSeenAt, lastSeenAt};
  });
  return {offlineAfterSeconds: payload.offline_after_seconds, nodes};
}

function _i2LogmonitorCheck(raw, index, label) {
  const item = _i2ContractObject(raw, `${label} check ${index}`);
  const status = _i2ContractString(item.status, `${label} check ${index} status`);
  if (!['passed', 'failed', 'pending', 'running', 'skipped'].includes(status)) {
    throw new TypeError(`${label} check ${index} status is invalid`);
  }
  return {
    name: _i2ContractString(item.name, `${label} check ${index} name`),
    status,
    message: _i2ContractString(item.message, `${label} check ${index} message`, true),
  };
}

function parseLogmonitorPreflight(value) {
  const payload = _i2ContractObject(value, 'LogMonitor preflight');
  if (!Array.isArray(payload.checks) || payload.checks.length === 0) {
    throw new TypeError('LogMonitor preflight checks must be a non-empty array');
  }
  const checks = payload.checks.map((item, index) => _i2LogmonitorCheck(item, index, 'LogMonitor preflight'));
  return {
    preflightId: _i2ContractString(payload.preflight_id, 'LogMonitor preflight id'),
    targetIp: _i2ContractString(payload.target_ip, 'LogMonitor preflight target IP'),
    agentBaseUrl: _i2ContractString(payload.agent_base_url, 'LogMonitor preflight AGENT_BASE_URL'),
    agentBackUrl: _i2ContractString(payload.agent_back_url, 'LogMonitor preflight AGENT_BACK_URL'),
    checks,
    expiresAt: _i2ContractString(payload.expires_at, 'LogMonitor preflight expiry'),
    passed: checks.every(check => check.status === 'passed'),
  };
}

function parseLogmonitorInstallation(value, requireDetails = false) {
  const payload = _i2ContractObject(value, 'LogMonitor installation');
  const status = _i2ContractString(payload.status, 'LogMonitor installation status');
  if (!['queued', 'running', 'completed', 'failed', 'cancelled'].includes(status)) {
    throw new TypeError('LogMonitor installation status is invalid');
  }
  const result = {
    jobId: _i2ContractString(payload.job_id, 'LogMonitor installation job id'),
    status,
    stage: _i2ContractString(payload.stage, 'LogMonitor installation stage'),
    message: _i2ContractString(payload.message, 'LogMonitor installation message', true),
    checks: [],
  };
  if (Object.prototype.hasOwnProperty.call(payload, 'checks')) {
    if (!Array.isArray(payload.checks)) throw new TypeError('LogMonitor installation checks must be an array');
    result.checks = payload.checks.map((item, index) => _i2LogmonitorCheck(item, index, 'LogMonitor installation'));
  }
  if (requireDetails) {
    result.targetIp = _i2ContractString(payload.target_ip, 'LogMonitor installation target IP');
    result.createdAt = _i2ContractString(payload.created_at, 'LogMonitor installation created time');
    result.updatedAt = _i2ContractString(payload.updated_at, 'LogMonitor installation updated time');
  }
  return result;
}

function normalizeLogmonitorPayload(raw) {
  const values = _i2ContractObject(raw, 'LogMonitor form');
  const required = ['target_ip', 'ssh_username', 'MCP_IADEBUG_USER', 'ACTIVE_HOME', 'STREAM_HOME', 'STREAM_DATA_HOME'];
  const payload = {};
  required.forEach(name => {
    payload[name] = _i2ContractString(values[name], `LogMonitor ${name}`).trim();
    if (!payload[name]) throw new TypeError(`LogMonitor ${name} must not be blank`);
  });
  ['ssh_password', 'ssh_private_key'].forEach(name => {
    if (!Object.prototype.hasOwnProperty.call(values, name)) return;
    const value = _i2ContractString(values[name], `LogMonitor ${name}`, true);
    if (value) payload[name] = value;
  });
  if (!payload.ssh_password && !payload.ssh_private_key) {
    throw new TypeError('LogMonitor requires an SSH password or private key');
  }
  const port = Number(values.ssh_port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new TypeError('LogMonitor ssh_port must be an integer from 1 to 65535');
  }
  if (String(values.STREAM_LOG_MONITOR) !== '1') {
    throw new TypeError('LogMonitor STREAM_LOG_MONITOR must be 1');
  }
  ['ACTIVE_HOME', 'STREAM_HOME', 'STREAM_DATA_HOME'].forEach(name => {
    if (!payload[name].startsWith('/')) throw new TypeError(`LogMonitor ${name} must be an absolute path`);
  });
  payload.ssh_port = port;
  payload.STREAM_LOG_MONITOR = String(values.STREAM_LOG_MONITOR);
  return payload;
}

async function deleteNodeRequest(ip) {
  const nodeIp = _i2ContractString(ip, 'node deletion IP');
  await api(`${I2STREAM_API}/nodes/${encodeURIComponent(nodeIp)}`, {method: 'DELETE'});
}

function reconcileKnowledgeSelection(files, selectedFileIds) {
  if (!Array.isArray(files)) throw new TypeError('knowledge selection files must be an array');
  if (Object.prototype.toString.call(selectedFileIds) !== '[object Set]') {
    throw new TypeError('knowledge selection must be a Set');
  }
  return new Set(files.map(file => _i2ContractString(file.fileId, 'knowledge selection file id'))
    .filter(fileId => selectedFileIds.has(fileId)));
}

async function _i2DeleteKnowledgeFiles(files, collectionName) {
  if (!Array.isArray(files) || files.length === 0) {
    throw new TypeError('knowledge deletion requires at least one file');
  }
  const capturedCollectionName = _i2ContractString(
    collectionName,
    'knowledge deletion collection',
  );
  const targets = files.map((raw, index) => {
    const file = _i2ContractObject(raw, `knowledge deletion file ${index}`);
    return {
      fileId: _i2ContractString(file.fileId, `knowledge deletion file ${index} id`),
      displayName: _i2ContractString(file.displayName, `knowledge deletion file ${index} display name`),
    };
  });
  const results = await Promise.allSettled(targets.map(async file => {
    _i2Success(
      await api(knowledgeCollectionUrl(
        `/knowledge/files/${encodeURIComponent(file.fileId)}`,
        capturedCollectionName,
      ), {method: 'DELETE'}),
      `knowledge delete ${file.fileId}`,
    );
    return file.fileId;
  }));
  const deletedFileIds = [];
  const failures = [];
  results.forEach((result, index) => {
    if (result.status === 'fulfilled') {
      deletedFileIds.push(result.value);
      return;
    }
    const reason = result.reason;
    failures.push({
      fileId: targets[index].fileId,
      displayName: targets[index].displayName,
      message: reason && typeof reason.message === 'string' ? reason.message : String(reason),
    });
  });
  return {deletedFileIds, failures};
}

function parseReports(value) {
  const payload = _i2Success(value, 'reports');
  if (!Array.isArray(payload.files)) throw new TypeError('reports files must be an array');
  if (typeof payload.server_time !== 'number' || !Number.isFinite(payload.server_time)) {
    throw new TypeError('reports server_time must be a finite number');
  }
  return payload.files.map((raw, index) => {
    const item = _i2ContractObject(raw, `report ${index}`);
    if (typeof item.size !== 'number' || !Number.isFinite(item.size) || item.size < 0) {
      throw new TypeError(`report ${index} size must be a non-negative number`);
    }
    if (typeof item.created_at !== 'number' || !Number.isFinite(item.created_at)) {
      throw new TypeError(`report ${index} created_at must be a finite number`);
    }
    return {
      token: _i2ContractString(item.token, `report ${index} token`),
      name: _i2ContractString(item.name, `report ${index} name`),
      mediaType: _i2ContractString(item.media_type, `report ${index} media_type`),
      size: item.size,
      createdAt: item.created_at,
      description: _i2ContractString(item.description, `report ${index} description`, true),
    };
  });
}

function conversationPageUrl(clientId, limit, before) {
  if (!I2STREAM_CLIENT_ID_RE.test(clientId)) throw new TypeError('client id is invalid');
  if (!Number.isInteger(limit) || limit < 1) throw new TypeError('conversation page limit must be a positive integer');
  const query = new URLSearchParams({client_id: clientId, limit: String(limit)});
  if (before !== null) {
    if (!Number.isSafeInteger(before) || before < 1) throw new TypeError('conversation cursor must be a positive safe integer');
    query.set('before', String(before));
  }
  return `${I2STREAM_API}/conversations?${query.toString()}`;
}

function conversationDetailUrl(conversationId, clientId) {
  const query = new URLSearchParams({client_id: _i2ContractString(clientId, 'client id')});
  return `${I2STREAM_API}/conversations/${encodeURIComponent(_i2ContractString(conversationId, 'conversation id'))}/messages?${query.toString()}`;
}

function _i2Text(key, ...args) {
  return typeof t === 'function' ? t(key, ...args) : key;
}

function _i2Escape(value) {
  return String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function _i2FormatBytes(value) {
  if (value === null) return '—';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function _i2FormatDate(value, seconds = false) {
  const date = new Date(seconds ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function _onlineNodesStateForTest() {
  return {
    available: _i2streamState.nodesAvailable,
    nodes: _i2streamState.nodes.map(node => ({...node})),
  };
}

function _renderOnlineNodes() {
  const button = document.getElementById('onlineNodesButton');
  const label = document.getElementById('onlineNodesLabel');
  const threshold = document.getElementById('onlineNodesThreshold');
  const body = document.getElementById('onlineNodesBody');
  const available = _i2streamState.nodesAvailable;
  const nodes = _i2streamState.nodes;
  const onlineCount = nodes.filter(node => node.online).length;

  if (label) {
    label.textContent = available === false
      ? _i2Text('online_nodes_unavailable_label')
      : (available === true
        ? _i2Text('online_nodes_count', onlineCount, nodes.length)
        : _i2Text('online_nodes'));
  }
  if (button) {
    button.classList.toggle('unavailable', available === false);
    button.setAttribute('aria-label', label ? label.textContent : _i2Text('online_nodes'));
  }
  if (threshold) {
    threshold.textContent = _i2streamState.nodesOfflineAfterSeconds === null
      ? ''
      : _i2Text('online_nodes_threshold', _i2streamState.nodesOfflineAfterSeconds);
  }
  if (!body) return;

  body.replaceChildren();
  if (available === false) {
    const error = document.createElement('div');
    error.className = 'online-nodes-error';
    error.textContent = _i2Text('online_nodes_unavailable');
    if (nodes.length) {
      const stale = document.createElement('small');
      stale.textContent = _i2Text('online_nodes_stale');
      error.appendChild(stale);
    }
    body.appendChild(error);
  }
  if (available === null) {
    const loading = document.createElement('div');
    loading.className = 'online-nodes-message';
    loading.textContent = _i2Text('online_nodes_loading');
    body.appendChild(loading);
    return;
  }
  if (!nodes.length) {
    if (available === true) {
      const empty = document.createElement('div');
      empty.className = 'online-nodes-message';
      empty.textContent = _i2Text('online_nodes_empty');
      body.appendChild(empty);
    }
    return;
  }

  const list = document.createElement('div');
  list.className = 'online-nodes-list';
  list.setAttribute('role', 'list');
  nodes.forEach(node => {
    const row = document.createElement('div');
    row.className = `online-node-row ${node.online ? 'online' : 'offline'}`;
    row.setAttribute('role', 'listitem');

    const dot = document.createElement('span');
    dot.className = 'online-node-dot';
    dot.setAttribute('aria-hidden', 'true');

    const main = document.createElement('div');
    main.className = 'online-node-main';
    const ip = document.createElement('div');
    ip.className = 'online-node-ip';
    ip.textContent = node.ip;
    ip.title = node.ip;
    const seen = document.createElement('div');
    seen.className = 'online-node-seen';
    seen.textContent = _i2Text('online_nodes_last_seen', _i2FormatDate(node.lastSeenAt));
    main.append(ip, seen);

    const state = document.createElement('span');
    state.className = 'online-node-state';
    state.textContent = _i2Text(node.online ? 'online_nodes_online' : 'online_nodes_offline');
    row.append(dot, main, state);
    list.appendChild(row);
  });
  body.appendChild(list);
}

async function loadOnlineNodes() {
  if (_i2streamState.nodesRequestInFlight !== null) {
    return _i2streamState.nodesRequestInFlight;
  }
  if (_i2streamState.nodesAvailable === null) _renderOnlineNodes();
  const request = (async () => {
    try {
      const snapshot = parseNodes(await api(`${I2STREAM_API}/nodes`));
      _i2streamState.nodes = snapshot.nodes;
      _i2streamState.nodesOfflineAfterSeconds = snapshot.offlineAfterSeconds;
      _i2streamState.nodesAvailable = true;
      _renderOnlineNodes();
      return true;
    } catch {
      // Availability belongs to this request. The last successful node snapshot
      // remains untouched so a transport failure cannot manufacture offline nodes.
      _i2streamState.nodesAvailable = false;
      _renderOnlineNodes();
      return false;
    }
  })();
  _i2streamState.nodesRequestInFlight = request;
  try {
    return await request;
  } finally {
    if (_i2streamState.nodesRequestInFlight === request) {
      _i2streamState.nodesRequestInFlight = null;
    }
  }
}

function _positionOnlineNodesDropdown() {
  const button = document.getElementById('onlineNodesButton');
  const dropdown = document.getElementById('onlineNodesDropdown');
  if (!button || !dropdown || dropdown.hidden) return;
  if (button.offsetParent === null) {
    closeOnlineNodesDropdown();
    return;
  }
  const anchor = button.getBoundingClientRect();
  const width = dropdown.offsetWidth;
  const height = dropdown.offsetHeight;
  const left = Math.min(
    Math.max(8, anchor.left),
    Math.max(8, window.innerWidth - width - 8),
  );
  const top = height + 6 <= anchor.top
    ? anchor.top - height - 6
    : Math.min(window.innerHeight - height - 8, anchor.bottom + 6);
  dropdown.style.left = `${left}px`;
  dropdown.style.top = `${Math.max(8, top)}px`;
}

function closeOnlineNodesDropdown(restoreFocus = false) {
  const button = document.getElementById('onlineNodesButton');
  const dropdown = document.getElementById('onlineNodesDropdown');
  if (!button || !dropdown || dropdown.hidden) return;
  dropdown.hidden = true;
  button.setAttribute('aria-expanded', 'false');
  if (restoreFocus) button.focus();
}

function toggleOnlineNodesDropdown() {
  const button = document.getElementById('onlineNodesButton');
  const dropdown = document.getElementById('onlineNodesDropdown');
  if (!button || !dropdown) return;
  if (button.offsetParent === null) {
    closeOnlineNodesDropdown();
    return;
  }
  if (!dropdown.hidden) {
    closeOnlineNodesDropdown();
    return;
  }
  if (typeof closeProfileDropdown === 'function') closeProfileDropdown();
  if (typeof closeWsDropdown === 'function') closeWsDropdown();
  if (typeof closeModelDropdown === 'function') closeModelDropdown();
  if (typeof closeReasoningDropdown === 'function') closeReasoningDropdown();
  if (typeof closeToolsetsDropdown === 'function') closeToolsetsDropdown();
  dropdown.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  _renderOnlineNodes();
  _positionOnlineNodesDropdown();
  void loadOnlineNodes();
}

function _stopOnlineNodesPolling() {
  if (_i2streamState.nodesPollTimer === null) return;
  clearInterval(_i2streamState.nodesPollTimer);
  _i2streamState.nodesPollTimer = null;
}

function startOnlineNodesPolling() {
  _stopOnlineNodesPolling();
  if (document.visibilityState !== 'visible') return;
  void (_i2streamState.section === 'nodes' ? loadI2StreamNodes(false) : loadOnlineNodes());
  _i2streamState.nodesPollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') {
      void (_i2streamState.section === 'nodes' ? loadI2StreamNodes(false) : loadOnlineNodes());
    }
  }, I2STREAM_NODES_POLL_INTERVAL_MS);
}

function _bindOnlineNodes() {
  if (_i2streamState.nodesBindingsReady) return;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') startOnlineNodesPolling();
    else _stopOnlineNodesPolling();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeOnlineNodesDropdown(true);
  });
  document.addEventListener('click', event => {
    const button = document.getElementById('onlineNodesButton');
    const onlineNodesDropdown = document.getElementById('onlineNodesDropdown');
    if (!button || !onlineNodesDropdown || onlineNodesDropdown.hidden) return;
    if (button.contains(event.target) || onlineNodesDropdown.contains(event.target)) return;
    closeOnlineNodesDropdown();
  });
  window.addEventListener('resize', _positionOnlineNodesDropdown);
  const footer = document.querySelector('.composer-footer');
  if (footer && typeof MutationObserver === 'function') {
    _i2streamState.nodesFooterObserver = new MutationObserver(_positionOnlineNodesDropdown);
    _i2streamState.nodesFooterObserver.observe(footer, {attributes: true, attributeFilter: ['class']});
  }
  _i2streamState.nodesBindingsReady = true;
}

function _i2SetStatus(id, message, kind = '') {
  const element = document.getElementById(id);
  if (!element) return;
  element.textContent = message;
  element.dataset.kind = kind;
}

async function readLogmonitorPrivateKey(file) {
  if (!Number.isSafeInteger(file.size) || file.size < 0 || typeof file.text !== 'function') {
    throw new TypeError('LogMonitor SSH private key file is invalid');
  }
  if (file.size > I2STREAM_PRIVATE_KEY_MAX_BYTES) {
    throw new TypeError(_i2Text('i2stream_logmonitor_ssh_private_key_too_large'));
  }
  return file.text();
}

async function _i2LogmonitorFormPayload() {
  const form = document.getElementById('i2streamLogmonitorForm');
  if (!form) throw new Error('LogMonitor form is unavailable');
  if (!form.reportValidity()) throw new TypeError(_i2Text('i2stream_logmonitor_invalid_form'));
  const values = {};
  new FormData(form).forEach((value, key) => { values[key] = value; });
  const privateKeyInput = document.getElementById('i2streamLogmonitorSshPrivateKey');
  if (privateKeyInput?.files.length === 1) {
    values.ssh_private_key = await readLogmonitorPrivateKey(privateKeyInput.files[0]);
  }
  if (!values.ssh_password && !values.ssh_private_key) {
    throw new TypeError(_i2Text('i2stream_logmonitor_ssh_credentials_required'));
  }
  return normalizeLogmonitorPayload(values);
}

function _i2ClearLogmonitorCredentials() {
  const password = document.getElementById('i2streamLogmonitorSshPassword');
  if (password) password.value = '';
  const privateKey = document.getElementById('i2streamLogmonitorSshPrivateKey');
  if (privateKey) privateKey.value = '';
}

function redactLogmonitorText(value, password) {
  const message = String(value);
  return password ? message.split(password).join('••••••') : message;
}

function _i2LogmonitorRedact(value) {
  const password = document.getElementById('i2streamLogmonitorSshPassword')?.value;
  return redactLogmonitorText(value, password);
}

function _i2InvalidateLogmonitorPreflight(preserveResult = false) {
  _i2streamState.logmonitorRequestGeneration += 1;
  _i2streamState.logmonitorPreflight = null;
  const install = document.getElementById('i2streamLogmonitorInstallBtn');
  if (install) install.disabled = true;
  const validity = document.getElementById('i2streamLogmonitorValidity');
  if (validity) validity.textContent = _i2Text('i2stream_logmonitor_preflight_required');
  if (preserveResult !== true) {
    const result = document.getElementById('i2streamLogmonitorResult');
    if (result) result.hidden = true;
  }
}

function _i2SetLogmonitorBusy(busy) {
  const form = document.getElementById('i2streamLogmonitorForm');
  if (form) form.querySelectorAll('input, select, button').forEach(control => { control.disabled = busy; });
  const install = document.getElementById('i2streamLogmonitorInstallBtn');
  if (install && !busy) install.disabled = !_i2streamState.logmonitorPreflight?.passed;
}

function _i2RenderLogmonitorChecks(checks) {
  const container = document.getElementById('i2streamLogmonitorChecks');
  if (!container) return;
  container.replaceChildren();
  checks.forEach(check => {
    const row = document.createElement('div');
    row.className = `i2stream-install-check ${check.status}`;
    row.setAttribute('role', 'listitem');
    const mark = document.createElement('span');
    mark.className = 'i2stream-install-check-mark';
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = check.status === 'passed' ? '✓' : (check.status === 'failed' ? '×' : '•');
    const body = document.createElement('span');
    const name = document.createElement('strong');
    name.textContent = _i2LogmonitorRedact(check.name);
    const message = document.createElement('small');
    message.textContent = _i2LogmonitorRedact(check.message);
    body.append(name, message);
    row.append(mark, body);
    container.appendChild(row);
  });
}

function _i2ShowLogmonitorResult() {
  const result = document.getElementById('i2streamLogmonitorResult');
  if (result) result.hidden = false;
}

function _i2RenderLogmonitorPreflight(preflight) {
  _i2ShowLogmonitorResult();
  const urls = document.getElementById('i2streamLogmonitorUrls');
  if (urls) urls.hidden = false;
  const baseUrl = document.getElementById('i2streamLogmonitorBaseUrl');
  const backUrl = document.getElementById('i2streamLogmonitorBackUrl');
  if (baseUrl) baseUrl.textContent = preflight.agentBaseUrl;
  if (backUrl) backUrl.textContent = preflight.agentBackUrl;
  _i2RenderLogmonitorChecks(preflight.checks);
  const state = document.getElementById('i2streamLogmonitorState');
  if (state) {
    state.className = `i2stream-install-state ${preflight.passed ? 'passed' : 'failed'}`;
    state.textContent = _i2Text(preflight.passed ? 'i2stream_logmonitor_preflight_passed' : 'i2stream_logmonitor_preflight_failed');
  }
  const status = preflight.passed
    ? _i2Text('i2stream_logmonitor_preflight_ready')
    : _i2Text('i2stream_logmonitor_preflight_fix');
  _i2SetStatus('i2streamLogmonitorStatus', status, preflight.passed ? '' : 'error');
  const validity = document.getElementById('i2streamLogmonitorValidity');
  if (validity) validity.textContent = preflight.passed ? _i2Text('i2stream_logmonitor_preflight_valid') : '';
}

async function preflightI2StreamLogmonitor(event) {
  event.preventDefault();
  if (_i2streamState.logmonitorJobId !== null) return;
  const generation = ++_i2streamState.logmonitorRequestGeneration;
  let payload;
  try {
    payload = await _i2LogmonitorFormPayload();
  } catch (error) {
    _i2SetStatus('i2streamLogmonitorStatus', error.message, 'error');
    return;
  }
  _i2streamState.logmonitorPreflight = null;
  _i2SetLogmonitorBusy(true);
  _i2ShowLogmonitorResult();
  _i2SetStatus('i2streamLogmonitorStatus', _i2Text('i2stream_logmonitor_preflight_running'));
  try {
    const preflight = parseLogmonitorPreflight(await api(`${I2STREAM_API}/logmonitor/preflight`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }));
    if (generation !== _i2streamState.logmonitorRequestGeneration) return;
    _i2streamState.logmonitorPreflight = preflight;
    _i2RenderLogmonitorPreflight(preflight);
  } catch (error) {
    if (generation !== _i2streamState.logmonitorRequestGeneration) return;
    _i2SetStatus('i2streamLogmonitorStatus', `${_i2Text('error_prefix')}${_i2LogmonitorRedact(error.message)}`, 'error');
    const state = document.getElementById('i2streamLogmonitorState');
    if (state) {
      state.className = 'i2stream-install-state failed';
      state.textContent = _i2Text('i2stream_logmonitor_preflight_failed');
    }
  } finally {
    if (generation === _i2streamState.logmonitorRequestGeneration) _i2SetLogmonitorBusy(false);
  }
}

function _i2RenderLogmonitorJob(job) {
  _i2ShowLogmonitorResult();
  const state = document.getElementById('i2streamLogmonitorState');
  if (state) {
    state.className = `i2stream-install-state ${job.status}`;
    state.textContent = _i2Text(`i2stream_logmonitor_job_${job.status}`);
  }
  _i2SetStatus(
    'i2streamLogmonitorStatus',
    _i2LogmonitorRedact(job.message || _i2Text('i2stream_logmonitor_install_running')),
    job.status === 'failed' || job.status === 'cancelled' ? 'error' : '',
  );
  if (job.checks.length) _i2RenderLogmonitorChecks(job.checks);
  const progress = document.getElementById('i2streamLogmonitorProgress');
  if (!progress) return;
  progress.hidden = false;
  progress.replaceChildren();
  const item = document.createElement('li');
  item.className = `i2stream-install-stage ${job.status}`;
  const stage = document.createElement('strong');
  stage.textContent = _i2LogmonitorRedact(job.stage);
  const jobId = document.createElement('small');
  jobId.textContent = _i2Text('i2stream_logmonitor_job_id', job.jobId);
  item.append(stage, jobId);
  progress.appendChild(item);
}

function _i2StopLogmonitorPolling() {
  if (_i2streamState.logmonitorPollTimer === null) return;
  clearTimeout(_i2streamState.logmonitorPollTimer);
  _i2streamState.logmonitorPollTimer = null;
}

async function _i2PollLogmonitorJob(jobId, generation) {
  if (generation !== _i2streamState.logmonitorRequestGeneration) return;
  try {
    const job = parseLogmonitorInstallation(
      await api(`${I2STREAM_API}/logmonitor/installations/${encodeURIComponent(jobId)}`),
      true,
    );
    if (generation !== _i2streamState.logmonitorRequestGeneration) return;
    _i2RenderLogmonitorJob(job);
    if (['completed', 'failed', 'cancelled'].includes(job.status)) {
      _i2streamState.logmonitorJobId = null;
      _i2StopLogmonitorPolling();
      _i2SetLogmonitorBusy(false);
      _i2ClearLogmonitorCredentials();
      _i2InvalidateLogmonitorPreflight(true);
      return;
    }
    _i2streamState.logmonitorPollTimer = setTimeout(
      () => void _i2PollLogmonitorJob(jobId, generation),
      I2STREAM_INSTALL_POLL_INTERVAL_MS,
    );
  } catch (error) {
    if (generation !== _i2streamState.logmonitorRequestGeneration) return;
    _i2SetStatus('i2streamLogmonitorStatus', `${_i2Text('error_prefix')}${_i2LogmonitorRedact(error.message)}`, 'error');
    if (Number.isInteger(error.status) && error.status < 500) {
      _i2streamState.logmonitorJobId = null;
      _i2StopLogmonitorPolling();
      _i2SetLogmonitorBusy(false);
      _i2InvalidateLogmonitorPreflight(true);
      return;
    }
    _i2streamState.logmonitorPollTimer = setTimeout(
      () => void _i2PollLogmonitorJob(jobId, generation),
      I2STREAM_INSTALL_POLL_INTERVAL_MS,
    );
  }
}

async function installI2StreamLogmonitor() {
  const preflight = _i2streamState.logmonitorPreflight;
  if (!preflight?.passed || _i2streamState.logmonitorJobId !== null) return;
  let payload;
  try {
    payload = await _i2LogmonitorFormPayload();
  } catch (error) {
    _i2InvalidateLogmonitorPreflight();
    _i2SetStatus('i2streamLogmonitorStatus', error.message, 'error');
    return;
  }
  payload.preflight_id = preflight.preflightId;
  const generation = ++_i2streamState.logmonitorRequestGeneration;
  _i2SetLogmonitorBusy(true);
  _i2SetStatus('i2streamLogmonitorStatus', _i2Text('i2stream_logmonitor_install_starting'));
  try {
    const job = parseLogmonitorInstallation(await api(`${I2STREAM_API}/logmonitor/installations`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }));
    if (generation !== _i2streamState.logmonitorRequestGeneration) return;
    _i2streamState.logmonitorJobId = job.jobId;
    _i2RenderLogmonitorJob(job);
    _i2ClearLogmonitorCredentials();
    await _i2PollLogmonitorJob(job.jobId, generation);
  } catch (error) {
    if (generation !== _i2streamState.logmonitorRequestGeneration) return;
    _i2SetStatus('i2streamLogmonitorStatus', `${_i2Text('error_prefix')}${_i2LogmonitorRedact(error.message)}`, 'error');
    _i2SetLogmonitorBusy(false);
    _i2InvalidateLogmonitorPreflight(true);
  }
}

function _i2RenderFailure(containerId, statusId, error) {
  const message = error instanceof Error ? error.message : String(error);
  _i2SetStatus(statusId, `${_i2Text('error_prefix')}${message}`, 'error');
  const container = document.getElementById(containerId);
  if (container) container.innerHTML = `<div class="i2stream-empty i2stream-error">${_i2Escape(message)}</div>`;
}

function _i2EnsureBindings() {
  if (_i2streamState.bindingsReady) return;
  const fileInput = document.getElementById('i2streamKnowledgeFile');
  if (fileInput) {
    fileInput.addEventListener('change', () => {
      const label = document.getElementById('i2streamKnowledgeFileLabel');
      if (label) label.textContent = fileInput.files.length === 1 ? fileInput.files[0].name : _i2Text('i2stream_choose_file');
    });
  }
  const history = document.getElementById('i2streamHistoryList');
  if (history) history.addEventListener('keydown', _i2HistoryKeydown);
  const sectionTabs = document.querySelector('.i2stream-side-menu');
  if (sectionTabs) sectionTabs.addEventListener('keydown', _i2SectionKeydown);
  const logmonitorForm = document.getElementById('i2streamLogmonitorForm');
  if (logmonitorForm) {
    logmonitorForm.addEventListener('input', _i2InvalidateLogmonitorPreflight);
    logmonitorForm.addEventListener('change', _i2InvalidateLogmonitorPreflight);
    _i2InvalidateLogmonitorPreflight();
  }
  _i2streamState.bindingsReady = true;
}

function _i2SectionKeydown(event) {
  const current = event.target.closest?.('[data-i2stream-section]');
  if (!current) return;
  const tabs = Array.from(document.querySelectorAll('[data-i2stream-section]'));
  const currentIndex = tabs.indexOf(current);
  if (currentIndex < 0) return;
  let nextIndex;
  if (event.key === 'Home') nextIndex = 0;
  else if (event.key === 'End') nextIndex = tabs.length - 1;
  else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (currentIndex + 1) % tabs.length;
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
  else return;
  event.preventDefault();
  const next = tabs[nextIndex];
  next.focus();
  void switchI2StreamSection(next.dataset.i2streamSection);
}

async function loadI2StreamConsole(force = false) {
  _i2EnsureBindings();
  return switchI2StreamSection(_i2streamState.section, force);
}

async function switchI2StreamSection(section, force = false) {
  if (!I2STREAM_SECTIONS.has(section)) throw new TypeError(`Unsupported i2Stream section: ${section}`);
  _i2streamState.section = section;
  document.querySelectorAll('[data-i2stream-section]').forEach(button => {
    const active = button.dataset.i2streamSection === section;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('[data-i2stream-page]').forEach(page => {
    page.hidden = page.dataset.i2streamPage !== section;
  });
  const title = document.getElementById('i2streamMainTitle');
  if (title) {
    const key = {knowledge:'i2stream_knowledge',reports:'i2stream_reports',history:'i2stream_history',nodes:'i2stream_nodes',logmonitor:'i2stream_logmonitor'}[section];
    title.dataset.i18n = key;
    title.textContent = _i2Text(key);
  }
  const scoped = section === 'history';
  const scopeExplainer = document.getElementById('i2streamScopeExplainer');
  const scopeBadge = document.getElementById('i2streamScopeBadge');
  const sideScope = document.getElementById('i2streamSideScope');
  const sideScopeText = document.getElementById('i2streamSideScopeText');
  if (scopeExplainer) {
    const key = section === 'nodes'
      ? 'i2stream_nodes_explainer'
      : (section === 'logmonitor'
        ? 'i2stream_logmonitor_explainer'
        : (scoped ? 'i2stream_client_explainer' : 'i2stream_global_explainer'));
    scopeExplainer.dataset.i18n = key;
    scopeExplainer.textContent = _i2Text(key);
  }
  if (scopeBadge) {
    const key = scoped ? 'i2stream_client_scoped' : 'i2stream_global_shared';
    scopeBadge.dataset.i18n = key;
    scopeBadge.dataset.i2streamScope = scoped ? 'client' : 'global';
    scopeBadge.textContent = _i2Text(key);
  }
  if (sideScope) sideScope.dataset.i2streamScope = scoped ? 'client' : 'global';
  if (sideScopeText) {
    const key = scoped ? 'i2stream_client_scoped' : 'i2stream_global_shared';
    sideScopeText.dataset.i18n = key;
    sideScopeText.textContent = _i2Text(key);
  }
  if (typeof _isDesktopWidth === 'function' && !_isDesktopWidth() && typeof closeMobileSidebar === 'function') {
    closeMobileSidebar();
  }
  if (section === 'knowledge' && (force || !_i2streamState.loaded.knowledge)) await loadI2StreamKnowledge();
  if (section === 'reports' && (force || !_i2streamState.loaded.reports)) await loadI2StreamReports();
  if (section === 'nodes') await loadI2StreamNodes(true);
  if (section === 'history') {
    if (_i2streamState.historyClientId && (force || !_i2streamState.loaded.history)) {
      await loadI2StreamHistory(true);
    } else if (!_i2streamState.historyClientId) {
      _i2RenderHistoryPrompt();
    }
  }
}

async function refreshI2StreamConsole() {
  _i2streamState.knowledgeTaskGeneration += 1;
  _i2streamState.loaded[_i2streamState.section] = false;
  await switchI2StreamSection(_i2streamState.section, true);
}

async function loadI2StreamNodes(announce = true) {
  const generation = ++_i2streamState.requestGeneration.nodes;
  if (announce) _i2SetStatus('i2streamNodesStatus', _i2Text('loading'));
  const loaded = await loadOnlineNodes();
  if (generation !== _i2streamState.requestGeneration.nodes) return 'superseded';
  _i2streamState.loaded.nodes = loaded;
  _i2RenderNodeManagement();
  if (announce || !loaded) {
    _i2SetStatus(
      'i2streamNodesStatus',
      loaded ? '' : _i2Text('online_nodes_unavailable'),
      loaded ? '' : 'error',
    );
  }
  return loaded ? 'success' : 'failed';
}

function _i2RenderNodeManagement() {
  const summary = document.getElementById('i2streamNodesSummary');
  const container = document.getElementById('i2streamNodesList');
  const nodes = _i2streamState.nodes;
  const onlineCount = nodes.filter(node => node.online).length;
  if (summary) {
    summary.textContent = _i2streamState.nodesOfflineAfterSeconds === null
      ? ''
      : _i2Text(
        'i2stream_nodes_summary',
        onlineCount,
        nodes.length,
        _i2streamState.nodesOfflineAfterSeconds,
      );
  }
  if (!container) return;
  container.replaceChildren();
  if (!nodes.length) {
    const empty = document.createElement('div');
    empty.className = 'i2stream-empty';
    empty.textContent = _i2Text('i2stream_nodes_empty');
    container.appendChild(empty);
    return;
  }

  nodes.forEach(node => {
    const row = document.createElement('div');
    row.className = `i2stream-row i2stream-node-management-row ${node.online ? 'online' : 'offline'}`;

    const statusRail = document.createElement('span');
    statusRail.className = 'i2stream-node-status-rail';
    statusRail.setAttribute('aria-hidden', 'true');

    const main = document.createElement('div');
    main.className = 'i2stream-row-main';
    const titleLine = document.createElement('div');
    titleLine.className = 'i2stream-node-title-line';
    const title = document.createElement('div');
    title.className = 'i2stream-row-title i2stream-node-address';
    title.textContent = node.ip;
    const state = document.createElement('span');
    state.className = `i2stream-node-state ${node.online ? 'online' : 'offline'}`;
    state.textContent = _i2Text(node.online ? 'online_nodes_online' : 'online_nodes_offline');
    titleLine.append(title, state);

    const meta = document.createElement('div');
    meta.className = 'i2stream-row-meta i2stream-node-times';
    const firstSeen = document.createElement('span');
    firstSeen.textContent = _i2Text('i2stream_node_first_seen', _i2FormatDate(node.firstSeenAt));
    const lastSeen = document.createElement('span');
    lastSeen.textContent = _i2Text('i2stream_node_last_seen', _i2FormatDate(node.lastSeenAt));
    meta.append(firstSeen, lastSeen);
    main.append(titleLine, meta);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'i2stream-action danger';
    remove.disabled = _i2streamState.nodeDeleteInFlight;
    remove.textContent = _i2Text('delete_title');
    remove.setAttribute('aria-label', _i2Text('i2stream_node_delete_confirm', node.ip));
    remove.addEventListener('click', () => deleteI2StreamNode(node.ip));
    row.append(statusRail, main, remove);
    container.appendChild(row);
  });
}

async function deleteI2StreamNode(ip) {
  if (_i2streamState.nodeDeleteInFlight) return;
  const confirmed = await showConfirmDialog({
    title: _i2Text('i2stream_node_delete_confirm', ip),
    message: _i2Text('i2stream_node_delete_warning'),
    confirmLabel: _i2Text('delete_title'),
    danger: true,
    focusCancel: true,
  });
  if (!confirmed) return;

  // showConfirmDialog restores focus on a timer after resolving. Let that run
  // before a successful refresh replaces the triggering row.
  await new Promise(resolve => setTimeout(resolve, 0));
  _i2streamState.nodeDeleteInFlight = true;
  document.querySelectorAll('#i2streamNodesList .i2stream-action.danger').forEach(button => {
    button.disabled = true;
  });
  _i2SetStatus('i2streamNodesStatus', _i2Text('i2stream_node_deleting', ip));
  try {
    await deleteNodeRequest(ip);
    if (_i2streamState.nodesRequestInFlight !== null) {
      await _i2streamState.nodesRequestInFlight;
    }
    _i2streamState.nodes = _i2streamState.nodes.filter(node => node.ip !== ip);
    _renderOnlineNodes();
    _i2RenderNodeManagement();
    const refreshResult = await loadI2StreamNodes(false);
    const refreshed = refreshResult !== 'failed' && _i2streamState.nodesAvailable === true;
    _i2SetStatus(
      'i2streamNodesStatus',
      _i2Text(refreshed ? 'i2stream_node_deleted' : 'i2stream_node_deleted_refresh_failed', ip),
      refreshed ? '' : 'error',
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    _i2SetStatus('i2streamNodesStatus', `${_i2Text('error_prefix')}${message}`, 'error');
  } finally {
    _i2streamState.nodeDeleteInFlight = false;
    _i2RenderNodeManagement();
    const status = document.getElementById('i2streamNodesStatus');
    if (status) status.focus();
  }
}

function _i2KnowledgeConfigPayload() {
  const form = document.getElementById('i2streamKnowledgeConfig');
  if (!form || !form.reportValidity()) {
    throw new TypeError(_i2Text('i2stream_knowledge_config_invalid'));
  }
  const values = {};
  new FormData(form).forEach((value, key) => { values[key] = value; });
  return normalizeKnowledgeConfiguration(values);
}

function _i2SetKnowledgeConfigBusy(busy) {
  _i2streamState.knowledgeConfigBusy = busy;
  const form = document.getElementById('i2streamKnowledgeConfig');
  if (form) form.querySelectorAll('input, button').forEach(control => { control.disabled = busy; });
  _i2RenderKnowledgeSelectionControls();
}

function _i2RenderKnowledgeConfiguration(configuration) {
  _i2streamState.knowledgeConfiguration = configuration;
  const vectorHost = document.getElementById('i2streamKnowledgeVectorHost');
  const ragMcpUrl = document.getElementById('i2streamKnowledgeRagMcpUrl');
  if (vectorHost) vectorHost.value = configuration.vectorSearchHost || '';
  if (ragMcpUrl) ragMcpUrl.value = configuration.ragServiceMcpUrl || '';
  const state = document.getElementById('i2streamKnowledgeConfigState');
  if (state) {
    const stateKey = configuration.configured
      ? 'i2stream_knowledge_config_configured'
      : 'i2stream_knowledge_config_not_configured';
    state.className = `i2stream-knowledge-config-state${configuration.configured ? ' configured' : ''}`;
    state.dataset.i18n = stateKey;
    state.textContent = _i2Text(stateKey);
  }
  const unconfigured = document.getElementById('i2streamKnowledgeUnconfigured');
  if (unconfigured) unconfigured.hidden = configuration.configured;
  const form = document.getElementById('i2streamKnowledgeConfig');
  if (form) form.querySelectorAll('input, button').forEach(control => {
    control.disabled = _i2streamState.knowledgeConfigBusy;
  });
  _i2RenderKnowledgeSelectionControls();
}

async function checkI2StreamKnowledgeConfig() {
  let payload;
  try {
    payload = _i2KnowledgeConfigPayload();
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeConfigStatus', error.message, 'error');
    return;
  }
  _i2SetKnowledgeConfigBusy(true);
  _i2SetStatus('i2streamKnowledgeConfigStatus', _i2Text('i2stream_knowledge_config_checking'));
  try {
    parseKnowledgeConnectionCheck(await api(`${I2STREAM_API}/knowledge/config/check`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }));
    _i2SetStatus('i2streamKnowledgeConfigStatus', _i2Text('i2stream_knowledge_config_check_passed'));
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeConfigStatus', `${_i2Text('error_prefix')}${error.message}`, 'error');
  } finally {
    _i2SetKnowledgeConfigBusy(false);
  }
}

async function saveI2StreamKnowledgeConfig(event) {
  event.preventDefault();
  let payload;
  try {
    payload = _i2KnowledgeConfigPayload();
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeConfigStatus', error.message, 'error');
    return;
  }
  _i2streamState.requestGeneration.knowledge += 1;
  _i2SetKnowledgeConfigBusy(true);
  _i2SetStatus('i2streamKnowledgeConfigStatus', _i2Text('i2stream_knowledge_config_saving'));
  try {
    const saved = await saveKnowledgeConfigurationRequest(payload);
    _i2RenderKnowledgeConfiguration(saved.configuration);
    await loadI2StreamKnowledge();
    if (saved.mcpSyncError) {
      _i2SetStatus(
        'i2streamKnowledgeConfigStatus',
        _i2Text('i2stream_knowledge_mcp_sync_failed', saved.mcpSyncError.message),
        'error',
      );
    } else if (_i2streamState.knowledgeConfiguration?.configured === true) {
      let message = _i2Text(
        saved.mcp.reloadRequired
          ? 'i2stream_knowledge_mcp_reload_required'
          : 'i2stream_knowledge_config_saved',
      );
      if (saved.mcp.missingProfiles.length) {
        message += ` ${_i2Text('i2stream_knowledge_mcp_missing_profiles', saved.mcp.missingProfiles.join(', '))}`;
      }
      _i2SetStatus(
        'i2streamKnowledgeConfigStatus',
        message,
        saved.mcp.missingProfiles.length ? 'warning' : '',
      );
    }
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeConfigStatus', `${_i2Text('error_prefix')}${error.message}`, 'error');
  } finally {
    _i2SetKnowledgeConfigBusy(false);
  }
}

async function loadI2StreamKnowledge() {
  const generation = ++_i2streamState.requestGeneration.knowledge;
  _i2SetStatus('i2streamKnowledgeConfigStatus', _i2Text('i2stream_knowledge_config_loading'));
  _i2SetStatus('i2streamKnowledgeStatus', _i2Text('loading'));
  let configuration;
  try {
    configuration = parseKnowledgeConfiguration(
      await api(`${I2STREAM_API}/knowledge/config`),
    );
    if (generation !== _i2streamState.requestGeneration.knowledge) return false;
    _i2RenderKnowledgeConfiguration(configuration);
    _i2SetStatus('i2streamKnowledgeConfigStatus', '');
    if (!configuration.configured) {
      _i2streamState.knowledgeFiles = [];
      _i2streamState.knowledgeCollections = [];
      _i2streamState.selectedKnowledgeCollection = null;
      _i2streamState.selectedKnowledgeFileIds = new Set();
      _i2streamState.loaded.knowledge = true;
      _i2RenderKnowledge();
      _i2SetStatus('i2streamKnowledgeStatus', '');
      return true;
    }
  } catch (error) {
    if (generation !== _i2streamState.requestGeneration.knowledge) return false;
    _i2streamState.knowledgeFiles = [];
    _i2streamState.knowledgeCollections = [];
    _i2streamState.selectedKnowledgeCollection = null;
    _i2streamState.selectedKnowledgeFileIds = new Set();
    _i2streamState.loaded.knowledge = false;
    _i2streamState.knowledgeConfiguration = null;
    const unconfigured = document.getElementById('i2streamKnowledgeUnconfigured');
    if (unconfigured) unconfigured.hidden = false;
    const configState = document.getElementById('i2streamKnowledgeConfigState');
    if (configState) {
      configState.className = 'i2stream-knowledge-config-state';
      configState.dataset.i18n = 'i2stream_knowledge_config_not_configured';
      configState.textContent = _i2Text('i2stream_knowledge_config_not_configured');
    }
    _i2SetStatus('i2streamKnowledgeConfigStatus', `${_i2Text('error_prefix')}${error.message}`, 'error');
    _i2RenderFailure('i2streamKnowledgeList', 'i2streamKnowledgeStatus', error);
    _i2SetKnowledgeConfigBusy(false);
    return false;
  }

  try {
    const collections = parseKnowledgeCollections(
      await api(`${I2STREAM_API}/knowledge/collections`),
    );
    const collectionName = chooseKnowledgeCollection(
      collections,
      _i2streamState.selectedKnowledgeCollection,
    );
    if (generation !== _i2streamState.requestGeneration.knowledge) return false;
    _i2streamState.knowledgeCollections = collections;
    _i2streamState.selectedKnowledgeCollection = collectionName;
    _i2RenderKnowledgeCollectionControls();
    const files = parseKnowledgeFiles(
      await api(knowledgeCollectionUrl('/knowledge/files', collectionName)),
      collectionName,
    );
    if (generation !== _i2streamState.requestGeneration.knowledge) return false;
    _i2streamState.knowledgeFiles = files;
    _i2streamState.selectedKnowledgeFileIds = reconcileKnowledgeSelection(
      files,
      _i2streamState.selectedKnowledgeFileIds,
    );
    _i2streamState.loaded.knowledge = true;
    _i2RenderKnowledge();
    _i2SetStatus('i2streamKnowledgeStatus', '');
    return true;
  } catch (error) {
    if (generation !== _i2streamState.requestGeneration.knowledge) return false;
    _i2streamState.knowledgeFiles = [];
    _i2streamState.knowledgeCollections = [];
    _i2streamState.selectedKnowledgeCollection = null;
    _i2streamState.selectedKnowledgeFileIds = new Set();
    _i2streamState.loaded.knowledge = false;
    _i2RenderFailure('i2streamKnowledgeList', 'i2streamKnowledgeStatus', error);
    _i2RenderKnowledgeSelectionControls();
    return false;
  }
}

function _i2SelectedKnowledgeCollection() {
  const collectionName = _i2streamState.selectedKnowledgeCollection;
  if (collectionName === null) throw new TypeError(_i2Text('i2stream_knowledge_no_collections'));
  return _i2ContractString(collectionName, 'selected knowledge collection');
}

function _i2KnowledgeOperationLocked() {
  return _i2streamState.knowledgeConfigBusy ||
    _i2streamState.knowledgeCollectionBusy ||
    _i2streamState.knowledgeUploadInFlight ||
    _i2streamState.knowledgeDeleteInFlight;
}

function _i2RenderKnowledgeCollectionControls() {
  const configured = _i2streamState.knowledgeConfiguration?.configured === true;
  const container = document.getElementById('i2streamKnowledgeCollections');
  const select = document.getElementById('i2streamKnowledgeCollectionSelect');
  const nameInput = document.getElementById('i2streamKnowledgeCollectionName');
  const createButton = document.getElementById('i2streamKnowledgeCollectionCreateBtn');
  const locked = !configured || _i2KnowledgeOperationLocked();
  if (container) container.hidden = !configured;
  if (select) {
    select.innerHTML = _i2streamState.knowledgeCollections.map(collection => {
      const suffix = collection.isDefault ? ` · ${_i2Text('i2stream_knowledge_collection_default')}` : '';
      return `<option value="${_i2Escape(collection.name)}">${_i2Escape(collection.name)}${_i2Escape(suffix)}</option>`;
    }).join('');
    select.value = _i2streamState.selectedKnowledgeCollection || '';
    select.disabled = locked || _i2streamState.knowledgeCollections.length === 0;
  }
  if (nameInput) nameInput.disabled = locked;
  if (createButton) createButton.disabled = locked;
}

async function _i2LoadSelectedKnowledgeFiles(collectionName) {
  const capturedCollectionName = _i2ContractString(collectionName, 'selected knowledge collection');
  const generation = ++_i2streamState.requestGeneration.knowledge;
  _i2SetStatus('i2streamKnowledgeStatus', _i2Text('loading'));
  try {
    const files = parseKnowledgeFiles(
      await api(knowledgeCollectionUrl('/knowledge/files', capturedCollectionName)),
      capturedCollectionName,
    );
    if (generation !== _i2streamState.requestGeneration.knowledge ||
        capturedCollectionName !== _i2streamState.selectedKnowledgeCollection) return false;
    _i2streamState.knowledgeFiles = files;
    _i2streamState.selectedKnowledgeFileIds = new Set();
    _i2streamState.loaded.knowledge = true;
    _i2RenderKnowledge();
    _i2SetStatus('i2streamKnowledgeStatus', '');
    return true;
  } catch (error) {
    if (generation !== _i2streamState.requestGeneration.knowledge) return false;
    _i2streamState.knowledgeFiles = [];
    _i2streamState.loaded.knowledge = false;
    _i2RenderFailure('i2streamKnowledgeList', 'i2streamKnowledgeStatus', error);
    return false;
  }
}

async function selectI2StreamKnowledgeCollection(collectionName) {
  if (_i2KnowledgeOperationLocked()) return false;
  const selected = _i2ContractString(collectionName, 'selected knowledge collection');
  if (!_i2streamState.knowledgeCollections.some(collection => collection.name === selected)) {
    throw new TypeError('selected knowledge collection is unavailable');
  }
  _i2streamState.selectedKnowledgeCollection = selected;
  _i2streamState.knowledgeFiles = [];
  _i2streamState.selectedKnowledgeFileIds = new Set();
  _i2RenderKnowledge();
  return _i2LoadSelectedKnowledgeFiles(selected);
}

async function createI2StreamKnowledgeCollection(event) {
  event.preventDefault();
  if (_i2streamState.knowledgeConfiguration?.configured !== true || _i2KnowledgeOperationLocked()) return;
  const nameInput = document.getElementById('i2streamKnowledgeCollectionName');
  if (!nameInput) return;
  _i2streamState.knowledgeCollectionBusy = true;
  _i2RenderKnowledgeSelectionControls();
  _i2SetStatus('i2streamKnowledgeStatus', _i2Text('i2stream_knowledge_collection_creating'));
  try {
    const created = await createKnowledgeCollectionRequest(nameInput.value);
    _i2streamState.selectedKnowledgeCollection = created.name;
    nameInput.value = '';
    const loaded = await loadI2StreamKnowledge();
    if (loaded) {
      _i2SetStatus('i2streamKnowledgeStatus', _i2Text('i2stream_knowledge_collection_created', created.name));
    }
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeStatus', `${_i2Text('error_prefix')}${error.message}`, 'error');
  } finally {
    _i2streamState.knowledgeCollectionBusy = false;
    _i2RenderKnowledgeSelectionControls();
  }
}

function _i2RenderKnowledgeSelectionControls() {
  const files = _i2streamState.knowledgeFiles;
  const selectedCount = _i2streamState.selectedKnowledgeFileIds.size;
  const actions = document.getElementById('i2streamKnowledgeBatchActions');
  const selectAll = document.getElementById('i2streamKnowledgeSelectAll');
  const count = document.getElementById('i2streamKnowledgeSelectionCount');
  const deleteSelected = document.getElementById('i2streamKnowledgeDeleteSelected');
  const configured = _i2streamState.knowledgeConfiguration?.configured === true;
  const locked = !configured || !_i2streamState.selectedKnowledgeCollection || _i2KnowledgeOperationLocked();
  const uploadFile = document.getElementById('i2streamKnowledgeFile');
  const uploadButton = document.getElementById('i2streamKnowledgeUploadBtn');
  if (uploadFile) uploadFile.disabled = locked;
  if (uploadButton) uploadButton.disabled = locked;
  if (actions) actions.hidden = !configured || files.length === 0;
  if (selectAll) {
    selectAll.checked = files.length > 0 && selectedCount === files.length;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < files.length;
    selectAll.disabled = files.length === 0 || locked;
  }
  if (count) count.textContent = _i2Text('i2stream_selected_count', selectedCount, files.length);
  if (deleteSelected) {
    deleteSelected.disabled = selectedCount === 0 || locked;
  }
  document.querySelectorAll('[data-i2stream-knowledge-control]').forEach(control => {
    control.disabled = locked;
  });
  _i2RenderKnowledgeCollectionControls();
}

function _i2RenderKnowledge() {
  const container = document.getElementById('i2streamKnowledgeList');
  if (!container) return;
  if (!_i2streamState.knowledgeFiles.length) {
    container.innerHTML = `<div class="i2stream-empty">${_i2Escape(_i2Text('i2stream_empty_knowledge'))}</div>`;
    _i2RenderKnowledgeSelectionControls();
    return;
  }
  container.innerHTML = '';
  _i2streamState.knowledgeFiles.forEach(file => {
    const selected = _i2streamState.selectedKnowledgeFileIds.has(file.fileId);
    const row = document.createElement('article');
    row.className = `i2stream-row i2stream-knowledge-row${selected ? ' selected' : ''}`;
    row.innerHTML = `<label class="i2stream-row-select"><input type="checkbox" data-i2stream-knowledge-control aria-label="${_i2Escape(_i2Text('i2stream_select_file', file.displayName))}"${selected ? ' checked' : ''}></label><div class="i2stream-row-main"><div class="i2stream-row-title">${_i2Escape(file.displayName)}</div><div class="i2stream-row-meta"><span>${_i2Escape(file.fileType || 'file')}</span><span>${_i2Escape(_i2FormatBytes(file.fileSize))}</span><span>${file.totalChunks === null ? '—' : `${file.totalChunks} chunks`}</span><span>${_i2Escape(_i2FormatDate(file.uploadTime))}</span></div></div><button type="button" class="i2stream-action danger" data-i2stream-knowledge-control>${_i2Escape(_i2Text('delete_title'))}</button>`;
    const checkbox = row.querySelector('input');
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) _i2streamState.selectedKnowledgeFileIds.add(file.fileId);
      else _i2streamState.selectedKnowledgeFileIds.delete(file.fileId);
      row.classList.toggle('selected', checkbox.checked);
      _i2RenderKnowledgeSelectionControls();
    });
    row.querySelector('button').addEventListener('click', () => deleteI2StreamKnowledge(file.fileId, file.displayName));
    container.appendChild(row);
  });
  _i2RenderKnowledgeSelectionControls();
}

function toggleI2StreamKnowledgeAll(selected) {
  if (_i2KnowledgeOperationLocked() || !_i2streamState.loaded.knowledge) return;
  _i2streamState.selectedKnowledgeFileIds = selected
    ? new Set(_i2streamState.knowledgeFiles.map(file => file.fileId))
    : new Set();
  _i2RenderKnowledge();
}

async function uploadI2StreamKnowledge(event) {
  event.preventDefault();
  if (_i2streamState.knowledgeConfiguration?.configured !== true) {
    _i2SetStatus('i2streamKnowledgeStatus', _i2Text('i2stream_knowledge_unconfigured'), 'error');
    return;
  }
  const input = document.getElementById('i2streamKnowledgeFile');
  let generation = null;
  try {
    if (!input || input.files.length !== 1) throw new TypeError('Select exactly one knowledge file');
    if (input.files[0].size > MAX_UPLOAD_BYTES) throw new Error(_uploadTooLargeMessage(input.files[0]));
    const collectionName = _i2SelectedKnowledgeCollection();
    generation = ++_i2streamState.knowledgeTaskGeneration;
    _i2streamState.knowledgeUploadInFlight = true;
    _i2RenderKnowledgeSelectionControls();
    _i2SetStatus('i2streamKnowledgeStatus', _i2Text('uploading'));
    const form = new FormData();
    form.append('file', input.files[0], input.files[0].name);
    form.append('collection_name', collectionName);
    const payload = _i2Success(await api(`${I2STREAM_API}/knowledge/files`, {method:'POST', headers:{}, body:form, retries:0}), 'knowledge upload');
    const file = _i2ContractObject(payload.file, 'knowledge upload file');
    const taskId = _i2ContractString(file.task_id, 'knowledge upload task_id');
    await _i2WaitForKnowledgeTask(taskId, generation, collectionName);
    if (generation !== _i2streamState.knowledgeTaskGeneration) return;
    input.value = '';
    const label = document.getElementById('i2streamKnowledgeFileLabel');
    if (label) label.textContent = _i2Text('i2stream_choose_file');
    await loadI2StreamKnowledge();
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeStatus', `${_i2Text('upload_failed')}${error.message}`, 'error');
  } finally {
    _i2streamState.knowledgeUploadInFlight = false;
    _i2RenderKnowledgeSelectionControls();
  }
}

async function _i2WaitForKnowledgeTask(taskId, generation, collectionName) {
  const capturedCollectionName = _i2ContractString(collectionName, 'knowledge task collection');
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (generation !== _i2streamState.knowledgeTaskGeneration) return;
    const payload = _i2Success(await api(knowledgeCollectionUrl(
      `/knowledge/tasks/${encodeURIComponent(taskId)}`,
      capturedCollectionName,
    )), 'knowledge task');
    const task = _i2ContractObject(payload.task, 'knowledge task payload');
    _i2ContractString(task.status, 'knowledge task status');
    if (typeof task.terminal !== 'boolean') throw new TypeError('knowledge task terminal must be a boolean');
    _i2SetStatus('i2streamKnowledgeStatus', task.message && typeof task.message === 'string' ? task.message : task.status);
    if (task.terminal) {
      if (task.status !== 'completed') throw new Error(typeof task.error === 'string' && task.error ? task.error : 'Knowledge indexing failed');
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('Knowledge indexing timed out');
}

async function deleteI2StreamKnowledge(fileId, displayName) {
  if (_i2streamState.knowledgeConfiguration?.configured !== true) return;
  const collectionName = _i2SelectedKnowledgeCollection();
  const confirmed = await showConfirmDialog({title:`${_i2Text('delete_title')} ${displayName}?`,message:'',confirmLabel:_i2Text('delete_title'),danger:true,focusCancel:true});
  if (!confirmed) return;
  _i2streamState.knowledgeDeleteInFlight = true;
  _i2RenderKnowledgeSelectionControls();
  try {
    const deletion = await _i2DeleteKnowledgeFiles([{fileId, displayName}], collectionName);
    if (deletion.failures.length) throw new Error(deletion.failures[0].message);
    _i2streamState.selectedKnowledgeFileIds.delete(fileId);
    await loadI2StreamKnowledge();
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeStatus', `${_i2Text('error_prefix')}${error.message}`, 'error');
  } finally {
    _i2streamState.knowledgeDeleteInFlight = false;
    _i2RenderKnowledgeSelectionControls();
  }
}

async function deleteSelectedI2StreamKnowledge() {
  if (_i2streamState.knowledgeConfiguration?.configured !== true) return;
  const collectionName = _i2SelectedKnowledgeCollection();
  const selectedFiles = _i2streamState.knowledgeFiles.filter(file =>
    _i2streamState.selectedKnowledgeFileIds.has(file.fileId));
  if (!selectedFiles.length) {
    _i2SetStatus('i2streamKnowledgeStatus', _i2Text('i2stream_no_selection'), 'error');
    return;
  }
  const confirmed = await showConfirmDialog({
    title: _i2Text('i2stream_delete_selected_confirm', selectedFiles.length),
    message: _i2Text('i2stream_delete_selected_warning'),
    confirmLabel: _i2Text('i2stream_delete_selected'),
    danger: true,
    focusCancel: true,
  });
  if (!confirmed) return;
  _i2streamState.knowledgeDeleteInFlight = true;
  _i2RenderKnowledgeSelectionControls();
  _i2SetStatus(
    'i2streamKnowledgeStatus',
    _i2Text('i2stream_deleting_selected', selectedFiles.length),
  );
  try {
    const deletion = await _i2DeleteKnowledgeFiles(selectedFiles, collectionName);
    deletion.deletedFileIds.forEach(fileId => _i2streamState.selectedKnowledgeFileIds.delete(fileId));
    const refreshed = await loadI2StreamKnowledge();
    if (!refreshed) return;
    if (deletion.failures.length) {
      _i2SetStatus(
        'i2streamKnowledgeStatus',
        _i2Text(
          'i2stream_delete_selected_failed',
          deletion.failures.length,
          selectedFiles.length,
          deletion.failures.map(failure => `${failure.displayName}: ${failure.message}`).join('; '),
        ),
        'error',
      );
      return;
    }
    _i2SetStatus(
      'i2streamKnowledgeStatus',
      _i2Text('i2stream_deleted_selected', deletion.deletedFileIds.length),
    );
  } catch (error) {
    _i2SetStatus('i2streamKnowledgeStatus', `${_i2Text('error_prefix')}${error.message}`, 'error');
  } finally {
    _i2streamState.knowledgeDeleteInFlight = false;
    _i2RenderKnowledgeSelectionControls();
  }
}

async function loadI2StreamReports() {
  const generation = ++_i2streamState.requestGeneration.reports;
  _i2SetStatus('i2streamReportsStatus', _i2Text('loading'));
  try {
    const reports = parseReports(await api(`${I2STREAM_API}/reports`));
    if (generation !== _i2streamState.requestGeneration.reports) return;
    _i2streamState.reports = reports;
    _i2streamState.loaded.reports = true;
    _i2RenderReports();
    _i2SetStatus('i2streamReportsStatus', '');
  } catch (error) {
    if (generation !== _i2streamState.requestGeneration.reports) return;
    _i2RenderFailure('i2streamReportsList', 'i2streamReportsStatus', error);
  }
}

function _i2ReportContentUrl(token) {
  const path = `${I2STREAM_API}/reports/${encodeURIComponent(_i2ContractString(token, 'report token'))}/content`;
  return resolveI2StreamBrowserUrl(path, document.baseURI || window.location.href);
}

function resolveI2StreamBrowserUrl(path, baseUri) {
  const normalizedPath = _i2ContractString(path, 'browser path').replace(/^\/+/, '');
  return new URL(normalizedPath, _i2ContractString(baseUri, 'browser base URI')).href;
}

function _i2RenderReports() {
  const container = document.getElementById('i2streamReportsList');
  if (!container) return;
  if (!_i2streamState.reports.length) {
    container.innerHTML = `<div class="i2stream-empty">${_i2Escape(_i2Text('i2stream_empty_reports'))}</div>`;
    return;
  }
  container.innerHTML = '';
  _i2streamState.reports.forEach(report => {
    const row = document.createElement('article');
    row.className = `i2stream-row selectable${report.token === _i2streamState.selectedReportToken ? ' selected' : ''}`;
    row.innerHTML = `<div class="i2stream-row-main"><div class="i2stream-row-title">${_i2Escape(report.name)}</div><div class="i2stream-row-desc">${_i2Escape(report.description)}</div><div class="i2stream-row-meta"><span>${_i2Escape(_i2FormatBytes(report.size))}</span><span>${_i2Escape(_i2FormatDate(report.createdAt, true))}</span></div></div><div class="i2stream-row-actions"><button type="button" class="i2stream-action preview">${_i2Escape(_i2Text('i2stream_preview'))}</button><a class="i2stream-action" href="${_i2Escape(_i2ReportContentUrl(report.token))}" download>${_i2Escape(_i2Text('i2stream_download'))}</a><button type="button" class="i2stream-action danger">${_i2Escape(_i2Text('delete_title'))}</button></div>`;
    const buttons = row.querySelectorAll('button');
    buttons[0].addEventListener('click', () => previewI2StreamReport(report.token));
    buttons[1].addEventListener('click', () => deleteI2StreamReport(report.token, report.name));
    container.appendChild(row);
  });
}

function previewI2StreamReport(token) {
  const report = _i2streamState.reports.find(item => item.token === token);
  if (!report) throw new TypeError('Report is not in the current global report list');
  _i2streamState.selectedReportToken = token;
  _i2RenderReports();
  const preview = document.getElementById('i2streamReportPreview');
  if (!preview) return;
  const url = _i2ReportContentUrl(token);
  preview.innerHTML = `<div class="i2stream-preview-head"><strong>${_i2Escape(report.name)}</strong><a class="i2stream-action" href="${_i2Escape(url)}" download>${_i2Escape(_i2Text('i2stream_download'))}</a></div><iframe class="i2stream-report-frame" src="${_i2Escape(url)}" sandbox title="${_i2Escape(report.name)}"></iframe>`;
}

async function deleteI2StreamReport(token, name) {
  const confirmed = await showConfirmDialog({title:`${_i2Text('delete_title')} ${name}?`,message:'',confirmLabel:_i2Text('delete_title'),danger:true,focusCancel:true});
  if (!confirmed) return;
  try {
    _i2Success(await api(`${I2STREAM_API}/reports/${encodeURIComponent(token)}`, {method: 'DELETE'}), 'report delete');
    if (_i2streamState.selectedReportToken === token) {
      _i2streamState.selectedReportToken = null;
      const preview = document.getElementById('i2streamReportPreview');
      if (preview) preview.innerHTML = `<div class="i2stream-empty">${_i2Escape(_i2Text('i2stream_select_report'))}</div>`;
    }
    await loadI2StreamReports();
  } catch (error) {
    _i2SetStatus('i2streamReportsStatus', `${_i2Text('error_prefix')}${error.message}`, 'error');
  }
}

async function loadI2StreamHistory(reset = false) {
  if (!_i2streamState.historyClientId) {
    _i2RenderHistoryPrompt();
    return;
  }
  const before = reset ? null : _i2streamState.nextBefore;
  if (!reset && before === null) return;
  const generation = ++_i2streamState.requestGeneration.history;
  const more = document.getElementById('i2streamHistoryMore');
  if (more) more.disabled = true;
  _i2SetStatus('i2streamHistoryStatus', _i2Text('loading'));
  try {
    const page = parseConversationPage(await api(conversationPageUrl(_i2streamState.historyClientId, I2STREAM_HISTORY_PAGE_SIZE, before)));
    if (generation !== _i2streamState.requestGeneration.history) return;
    const merged = reset ? [] : _i2streamState.conversations.slice();
    const byKey = new Map(merged.map(item => [item.conversationKey, item]));
    page.conversations.forEach(item => byKey.set(item.conversationKey, item));
    _i2streamState.conversations = Array.from(byKey.values());
    _i2streamState.nextBefore = page.nextBefore;
    _i2streamState.loaded.history = true;
    _i2RenderHistory();
    _i2SetStatus('i2streamHistoryStatus', '');
  } catch (error) {
    if (generation !== _i2streamState.requestGeneration.history) return;
    _i2RenderFailure('i2streamHistoryList', 'i2streamHistoryStatus', error);
  } finally {
    if (generation === _i2streamState.requestGeneration.history && more) more.disabled = false;
  }
}

function _i2ResetHistoryResults() {
  _i2streamState.requestGeneration.history += 1;
  _i2streamState.historyDetailGeneration += 1;
  _i2streamState.loaded.history = false;
  _i2streamState.conversations = [];
  _i2streamState.nextBefore = null;
  _i2streamState.selectedConversationKey = null;
  const detail = document.getElementById('i2streamHistoryDetail');
  if (detail) detail.innerHTML = `<div class="i2stream-empty">${_i2Escape(_i2Text('i2stream_select_history'))}</div>`;
}

function _i2RenderHistoryPrompt() {
  const container = document.getElementById('i2streamHistoryList');
  if (container) container.innerHTML = `<div class="i2stream-empty">${_i2Escape(_i2Text('i2stream_enter_client_id'))}</div>`;
  const more = document.getElementById('i2streamHistoryMore');
  if (more) more.hidden = true;
  _i2SetStatus('i2streamHistoryStatus', '');
}

async function submitI2StreamHistoryClientId(event) {
  event.preventDefault();
  const input = document.getElementById('i2streamHistoryClientId');
  const clientId = input ? input.value.trim() : '';
  if (!I2STREAM_CLIENT_ID_RE.test(clientId)) {
    _i2SetStatus('i2streamHistoryStatus', _i2Text('i2stream_invalid_client_id'), 'error');
    return;
  }
  if (_i2streamState.historyClientId !== clientId) {
    _i2streamState.historyClientId = clientId;
    _i2ResetHistoryResults();
  }
  await loadI2StreamHistory(true);
}

async function loadMoreI2StreamHistory() {
  await loadI2StreamHistory(false);
}

function _i2RenderHistory() {
  const container = document.getElementById('i2streamHistoryList');
  if (!container) return;
  if (!_i2streamState.conversations.length) {
    container.innerHTML = `<div class="i2stream-empty">${_i2Escape(_i2Text('i2stream_empty_history'))}</div>`;
  } else {
    container.innerHTML = '';
    _i2streamState.conversations.forEach((conversation, index) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = `i2stream-row i2stream-history-row${conversation.conversationKey === _i2streamState.selectedConversationKey ? ' selected' : ''}`;
      row.dataset.conversationKey = conversation.conversationKey;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', conversation.conversationKey === _i2streamState.selectedConversationKey ? 'true' : 'false');
      row.tabIndex = index === 0 ? 0 : -1;
      row.innerHTML = `<div class="i2stream-row-main"><div class="i2stream-row-title">${_i2Escape(conversation.preview || conversation.conversationId)}</div><div class="i2stream-row-meta"><span>${_i2Escape(_i2Text('n_messages', conversation.messageCount))}</span><span>${_i2Escape(_i2FormatDate(conversation.latestMessageAt))}</span></div></div><span class="i2stream-row-arrow" aria-hidden="true">›</span>`;
      row.addEventListener('click', () => selectI2StreamConversation(conversation.conversationKey));
      container.appendChild(row);
    });
  }
  const more = document.getElementById('i2streamHistoryMore');
  if (more) more.hidden = _i2streamState.nextBefore === null;
}

async function selectI2StreamConversation(conversationKey) {
  const conversation = _i2streamState.conversations.find(item => item.conversationKey === conversationKey);
  if (!conversation) throw new TypeError('Conversation is not in the current client history page');
  _i2streamState.selectedConversationKey = conversationKey;
  const generation = ++_i2streamState.historyDetailGeneration;
  _i2RenderHistory();
  const detail = document.getElementById('i2streamHistoryDetail');
  if (detail) detail.innerHTML = `<div class="i2stream-empty">${_i2Escape(_i2Text('loading'))}</div>`;
  try {
    const payload = parseConversationDetail(await api(conversationDetailUrl(conversation.conversationId, conversation.clientId)));
    if (generation !== _i2streamState.historyDetailGeneration ||
        _i2streamState.selectedConversationKey !== conversationKey) return;
    _i2RenderHistoryDetail(payload, conversation);
  } catch (error) {
    if (generation !== _i2streamState.historyDetailGeneration ||
        _i2streamState.selectedConversationKey !== conversationKey) return;
    if (detail) detail.innerHTML = `<div class="i2stream-empty i2stream-error">${_i2Escape(error.message)}</div>`;
  }
}

function _i2RenderHistoryDetail(payload, conversation) {
  const detail = document.getElementById('i2streamHistoryDetail');
  if (!detail) return;
  const messages = payload.messages.map(message => `<article class="i2stream-message" data-role="${_i2Escape(message.role)}"><div class="i2stream-message-head"><span>${_i2Escape(message.role)}</span><time>${_i2Escape(_i2FormatDate(message.createdAt))}</time></div><div class="i2stream-message-body">${_i2Escape(message.content)}</div></article>`).join('');
  detail.innerHTML = `<div class="i2stream-detail-head"><div><strong>${_i2Escape(conversation.preview || payload.conversationId)}</strong><div>${_i2Escape(_i2Text('n_messages', payload.messages.length))}</div></div></div><div class="i2stream-message-list">${messages || `<div class="i2stream-empty">${_i2Escape(_i2Text('i2stream_empty_messages'))}</div>`}</div>`;
}

function _i2HistoryKeydown(event) {
  if (!['ArrowDown', 'ArrowUp', 'Enter', 'Escape'].includes(event.key)) return;
  const rows = Array.from(event.currentTarget.querySelectorAll('.i2stream-history-row'));
  if (!rows.length) return;
  const current = rows.indexOf(document.activeElement);
  if (event.key === 'Enter' && current >= 0) {
    event.preventDefault();
    rows[current].click();
    return;
  }
  if (event.key === 'Escape') {
    event.preventDefault();
    const tab = document.querySelector('[data-i2stream-section="history"]');
    if (tab) tab.focus();
    return;
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const delta = event.key === 'ArrowDown' ? 1 : -1;
    const next = current < 0 ? 0 : (current + delta + rows.length) % rows.length;
    rows.forEach((row, index) => { row.tabIndex = index === next ? 0 : -1; });
    rows[next].focus();
  }
}

window.__i2streamConsoleTest = {
  parseConversationPage,
  parseConversationDetail,
  parseKnowledgeFiles,
  parseKnowledgeCollections,
  chooseKnowledgeCollection,
  knowledgeCollectionUrl,
  createKnowledgeCollectionRequest,
  parseKnowledgeConfiguration,
  parseKnowledgeConnectionCheck,
  parseKnowledgeMcpSync,
  normalizeKnowledgeConfiguration,
  saveKnowledgeConfigurationRequest,
  reconcileKnowledgeSelection,
  deleteKnowledgeFiles: _i2DeleteKnowledgeFiles,
  parseReports,
  conversationPageUrl,
  conversationDetailUrl,
  parseNodes,
  parseLogmonitorPreflight,
  parseLogmonitorInstallation,
  normalizeLogmonitorPayload,
  readLogmonitorPrivateKey,
  clearLogmonitorCredentials: _i2ClearLogmonitorCredentials,
  redactLogmonitorText,
  deleteNodeRequest,
  deleteI2StreamNode,
  loadI2StreamNodes,
  loadOnlineNodes,
  getOnlineNodesState: _onlineNodesStateForTest,
  loadI2StreamHistory,
  loadI2StreamKnowledge,
  resolveI2StreamBrowserUrl,
};

// i2Stream links are additive query parameters, so existing session path/query
// routing remains intact. Invalid views deliberately fall back to Knowledge.
if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
  _bindOnlineNodes();
  _renderOnlineNodes();
  startOnlineNodesPolling();
  const query = new URLSearchParams(window.location.search || '');
  if (query.get('panel') !== 'i2stream') return;
  const requestedView = query.get('view');
  const view = I2STREAM_SECTIONS.has(requestedView) ? requestedView : 'knowledge';
  _i2streamState.section = view;
  if (typeof switchPanel === 'function') switchPanel('i2stream');
});
