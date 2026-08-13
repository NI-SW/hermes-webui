from __future__ import annotations


def render_dashboard_chat_view() -> str:
    return """        <article id="view-chat" class="view chat-view" data-view-panel="chat">
          <style>
            .chat-view {
              --chat-ink: #263443;
              --chat-muted: #708092;
              --chat-line: #dce4ec;
              --chat-soft: #f5f8fb;
              --chat-cyan: #058fbd;
              --chat-cyan-soft: #eaf8fc;
              height: calc(100vh - 40px);
              min-height: 640px;
              display: grid;
              grid-template-columns: 286px minmax(0, 1fr);
              overflow: hidden;
              background: #fff;
              border: 1px solid var(--chat-line);
              border-radius: 12px;
              box-shadow: 0 12px 34px rgba(43, 66, 87, 0.08);
            }
            .sr-only {
              position: absolute;
              width: 1px;
              height: 1px;
              padding: 0;
              margin: -1px;
              overflow: hidden;
              clip: rect(0, 0, 0, 0);
              white-space: nowrap;
              border: 0;
            }
            .chat-view textarea:focus-visible,
            .chat-view input:focus-visible,
            .chat-view button:focus-visible {
              outline: 2px solid rgba(5, 143, 189, .28);
              outline-offset: 2px;
            }
            .chat-session-pane {
              min-width: 0;
              min-height: 0;
              display: grid;
              grid-template-rows: auto auto minmax(0, 1fr) auto;
              overflow: hidden;
              border-right: 1px solid var(--chat-line);
              background: #f8fafc;
            }
            .chat-session-head { padding: 18px 16px 12px; }
            .chat-session-head h1 { color: var(--chat-ink); font-size: 18px; }
            .chat-session-head p { margin-top: 3px; }
            .chat-new-button {
              width: 100%;
              min-height: 42px;
              margin-top: 14px;
              border: 1px solid #b8dae6;
              border-radius: 10px;
              background: #fff;
              color: #087da3;
              cursor: pointer;
              font-weight: 600;
            }
            .chat-new-button:hover { background: var(--chat-cyan-soft); }
            .chat-search-wrap { padding: 0 16px 10px; }
            .chat-search-wrap input[type="search"] {
              width: 100%;
              height: 36px;
              border: 1px solid var(--chat-line);
              border-radius: 9px;
              background: #fff;
              color: var(--chat-ink);
              padding: 0 11px;
            }
            .chat-session-list { min-height: 0; overflow-y: auto; padding: 4px 9px 14px; }
            .chat-session-item {
              width: 100%;
              display: grid;
              gap: 3px;
              border: 1px solid transparent;
              border-radius: 10px;
              background: transparent;
              color: var(--chat-ink);
              cursor: pointer;
              padding: 10px 11px;
              text-align: left;
            }
            .chat-session-item:hover { background: #fff; border-color: #e4ebf1; }
            .chat-session-item[aria-current="true"] {
              background: #fff;
              border-color: #c7e4ee;
              box-shadow: 0 4px 14px rgba(42, 98, 117, 0.07);
            }
            .chat-session-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
            .chat-session-meta { color: var(--chat-muted); font-size: 11px; }
            .chat-session-foot {
              border-top: 1px solid var(--chat-line);
              color: var(--chat-muted);
              font-size: 11px;
              line-height: 1.5;
              padding: 11px 16px;
            }
            .chat-workspace {
              min-width: 0;
              min-height: 0;
              display: grid;
              grid-template-rows: 64px minmax(0, 1fr) auto;
              overflow: hidden;
              background: #fff;
            }
            .chat-toolbar {
              display: flex;
              align-items: center;
              justify-content: space-between;
              gap: 16px;
              border-bottom: 1px solid var(--chat-line);
              padding: 0 22px;
            }
            .chat-current-title { min-width: 0; }
            .chat-current-title strong {
              display: block;
              overflow: hidden;
              text-overflow: ellipsis;
              white-space: nowrap;
              color: var(--chat-ink);
              font-size: 15px;
            }
            .chat-connection {
              display: inline-flex;
              align-items: center;
              gap: 6px;
              margin-top: 4px;
              color: var(--chat-muted);
              font-size: 11px;
            }
            .chat-connection::before {
              width: 7px;
              height: 7px;
              border-radius: 50%;
              background: #78b84a;
              content: "";
            }
            .chat-toolbar-actions { display: flex; gap: 8px; }
            .chat-quiet-button {
              min-height: 32px;
              border: 1px solid var(--chat-line);
              border-radius: 8px;
              background: #fff;
              color: #617284;
              cursor: pointer;
              padding: 5px 10px;
            }
            .chat-quiet-button:hover { border-color: #a9cbd7; color: #087da3; }
            .chat-quiet-button.danger:hover { border-color: #e6b5b5; color: #b84a4a; }
            .chat-quiet-button:disabled { cursor: not-allowed; opacity: .45; }
            .chat-messages {
              min-height: 0;
              overflow-y: auto;
              scroll-behavior: smooth;
              padding: 30px max(24px, calc((100% - 860px) / 2)) 38px;
            }
            .chat-welcome {
              min-height: 100%;
              display: grid;
              place-content: center;
              color: var(--chat-muted);
              text-align: center;
            }
            .chat-welcome-mark {
              width: 58px;
              height: 58px;
              display: grid;
              place-items: center;
              margin: 0 auto 14px;
              border: 1px solid #bcdce7;
              border-radius: 18px 18px 18px 6px;
              background: linear-gradient(145deg, #f5fcfe, #e8f7fb);
              color: var(--chat-cyan);
              font-family: Consolas, monospace;
              font-size: 19px;
              font-weight: 700;
            }
            .chat-welcome h2 { margin: 0; color: var(--chat-ink); font-size: 20px; }
            .chat-welcome p { max-width: 440px; margin: 8px auto 0; }
            .chat-turn { margin: 0 0 28px; color: var(--chat-ink); }
            .chat-turn-user { display: flex; justify-content: flex-end; }
            .chat-user-bubble {
              max-width: min(78%, 680px);
              border: 1px solid #d7e7ed;
              border-radius: 16px 16px 5px 16px;
              background: #f0f8fb;
              padding: 11px 14px;
              line-height: 1.7;
              white-space: pre-wrap;
              overflow-wrap: anywhere;
            }
            .chat-assistant-label {
              display: flex;
              align-items: center;
              gap: 8px;
              margin-bottom: 9px;
              color: #536576;
              font-size: 12px;
              font-weight: 600;
            }
            .chat-assistant-label::before {
              width: 20px;
              height: 20px;
              display: grid;
              place-items: center;
              border-radius: 7px 7px 7px 2px;
              background: var(--chat-cyan);
              color: #fff;
              content: "H";
              font: 700 10px/1 Consolas, monospace;
            }
            .chat-assistant-body {
              color: #2e3b48;
              font-size: 14px;
              line-height: 1.8;
              white-space: pre-wrap;
              overflow-wrap: anywhere;
            }
            .chat-assistant-body.pending { color: #8492a0; }
            .chat-execution-trail {
              position: relative;
              display: grid;
              gap: 9px;
              margin: 12px 0 14px 9px;
              padding-left: 18px;
            }
            .chat-execution-trail:empty { display: none; }
            .chat-execution-trail::before {
              position: absolute;
              top: 5px;
              bottom: 5px;
              left: 3px;
              width: 1px;
              background: #c8dbe3;
              content: "";
            }
            .chat-trace-item {
              position: relative;
              color: #667889;
              font-size: 12px;
              line-height: 1.6;
              white-space: pre-wrap;
              overflow-wrap: anywhere;
            }
            .chat-trace-item::before {
              position: absolute;
              top: 6px;
              left: -19px;
              width: 7px;
              height: 7px;
              border: 2px solid #fff;
              border-radius: 50%;
              background: #7ab8cc;
              box-shadow: 0 0 0 1px #9fc8d6;
              content: "";
            }
            .chat-trace-item.tool { color: #546b7a; font-family: Consolas, "SFMono-Regular", monospace; }
            .chat-trace-item.error { color: #b65252; }
            .chat-approval-card {
              position: relative;
              border: 1px solid #e6cf9d;
              border-radius: 10px;
              background: #fffaf0;
              padding: 12px 13px;
            }
            .chat-approval-card::before {
              position: absolute;
              top: 15px;
              left: -20px;
              width: 8px;
              height: 8px;
              border: 2px solid #fff;
              border-radius: 50%;
              background: #d9a43e;
              box-shadow: 0 0 0 1px #e6c477;
              content: "";
            }
            .chat-approval-card strong { display: block; color: #795c25; font-size: 13px; }
            .chat-approval-description { margin-top: 4px; color: #765f35; font-size: 12px; line-height: 1.6; white-space: pre-wrap; }
            .chat-approval-command {
              margin-top: 8px;
              border: 1px solid #eadbb9;
              border-radius: 6px;
              background: rgba(255, 255, 255, .72);
              color: #5f5749;
              font: 11px/1.55 Consolas, "SFMono-Regular", monospace;
              padding: 7px 8px;
              white-space: pre-wrap;
              overflow-wrap: anywhere;
            }
            .chat-approval-actions { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
            .chat-approval-button {
              min-height: 30px;
              border: 1px solid #d7bd83;
              border-radius: 7px;
              background: #fff;
              color: #765b25;
              cursor: pointer;
              padding: 4px 10px;
              font-size: 12px;
            }
            .chat-approval-button.primary { border-color: #ba8c2f; background: #ba8c2f; color: #fff; }
            .chat-approval-button.deny { border-color: #dfb1ab; color: #a44a42; }
            .chat-approval-button:disabled { cursor: not-allowed; opacity: .55; }
            .chat-composer-shell {
              border-top: 1px solid #e7edf2;
              background: rgba(255, 255, 255, .96);
              padding: 14px max(24px, calc((100% - 860px) / 2)) 18px;
            }
            .chat-composer {
              display: grid;
              grid-template-columns: minmax(0, 1fr) auto;
              gap: 10px;
              align-items: end;
              border: 1px solid #cfdbe4;
              border-radius: 14px;
              background: #fff;
              box-shadow: 0 7px 24px rgba(46, 72, 94, .08);
              padding: 10px 10px 10px 14px;
            }
            .chat-composer:focus-within { border-color: #79bcd1; box-shadow: 0 0 0 3px rgba(5, 143, 189, .1); }
            .chat-composer textarea {
              width: 100%;
              min-height: 42px;
              max-height: 180px;
              resize: none;
              border: 0;
              outline: 0;
              background: transparent;
              color: var(--chat-ink);
              font: inherit;
              line-height: 1.55;
              padding: 8px 0 4px;
            }
            .chat-send-button {
              width: 42px;
              height: 42px;
              border: 0;
              border-radius: 11px;
              background: var(--chat-cyan);
              color: #fff;
              cursor: pointer;
              font-size: 17px;
              font-weight: 700;
            }
            .chat-send-button.stop { background: #5d6b78; font-size: 13px; }
            .chat-send-button:disabled { cursor: not-allowed; opacity: .45; }
            .chat-composer-meta {
              display: flex;
              justify-content: space-between;
              min-height: 18px;
              margin-top: 6px;
              color: var(--chat-muted);
              font-size: 11px;
            }
            .chat-page-status.error { color: #b34f4f; }
            .chat-page-status.running { color: #9a7124; }
            @media (max-width: 980px) {
              .chat-view { grid-template-columns: 230px minmax(0, 1fr); }
            }
            @media (max-width: 760px) {
              .chat-view { height: auto; min-height: 760px; grid-template-columns: 1fr; }
              .chat-session-pane { max-height: 260px; border-right: 0; border-bottom: 1px solid var(--chat-line); }
              .chat-session-head { padding-top: 14px; }
              .chat-session-foot { display: none; }
              .chat-workspace { min-height: 660px; }
              .chat-toolbar { padding: 0 14px; }
              .chat-messages { padding: 24px 15px 30px; }
              .chat-composer-shell { padding: 11px 12px 14px; }
              .chat-user-bubble { max-width: 90%; }
            }
            @media (prefers-reduced-motion: reduce) {
              .chat-messages { scroll-behavior: auto; }
            }
          </style>

          <section class="chat-session-pane" aria-label="对话列表">
            <div class="chat-session-head">
              <h1>AI 对话</h1>
              <p>连接 Hermes stream-qa</p>
              <button id="dashboard-chat-new" class="chat-new-button" type="button">＋ 新建对话</button>
            </div>
            <label class="chat-search-wrap">
              <span class="sr-only">搜索对话</span>
              <input id="dashboard-chat-search" type="search" placeholder="搜索对话" autocomplete="off" />
            </label>
            <div id="dashboard-chat-sessions" class="chat-session-list"></div>
            <div class="chat-session-foot">会话内容保存在 Hermes 8641 对应的 profile 中。</div>
          </section>

          <section class="chat-workspace" aria-label="当前对话">
            <header class="chat-toolbar">
              <div class="chat-current-title">
                <strong id="dashboard-chat-title">新对话</strong>
                <span class="chat-connection">Hermes 8641</span>
              </div>
              <div class="chat-toolbar-actions">
                <button id="dashboard-chat-refresh" class="chat-quiet-button" type="button">刷新</button>
                <button id="dashboard-chat-delete" class="chat-quiet-button danger" type="button" disabled>删除</button>
              </div>
            </header>
            <div id="dashboard-chat-messages" class="chat-messages" aria-live="polite"></div>
            <div class="chat-composer-shell">
              <form id="dashboard-chat-composer" class="chat-composer">
                <textarea id="dashboard-chat-input" rows="1" maxlength="100000" placeholder="输入消息，Enter 发送，Shift + Enter 换行"></textarea>
                <button id="dashboard-chat-send" class="chat-send-button" type="submit" aria-label="发送消息">↑</button>
              </form>
              <div class="chat-composer-meta">
                <span id="dashboard-chat-status" class="chat-page-status" role="status">正在连接 Hermes…</span>
                <span>Approve 可在执行轨迹中处理</span>
              </div>
            </div>
          </section>

          <script>
            (() => {
              const required = (id, type) => {
                const element = document.getElementById(id);
                if (!(element instanceof type)) throw new Error(`Missing dashboard chat control: ${id}`);
                return element;
              };
              const sessionsElement = required("dashboard-chat-sessions", HTMLElement);
              const messagesElement = required("dashboard-chat-messages", HTMLElement);
              const newButton = required("dashboard-chat-new", HTMLButtonElement);
              const searchInput = required("dashboard-chat-search", HTMLInputElement);
              const titleElement = required("dashboard-chat-title", HTMLElement);
              const refreshButton = required("dashboard-chat-refresh", HTMLButtonElement);
              const deleteButton = required("dashboard-chat-delete", HTMLButtonElement);
              const composer = required("dashboard-chat-composer", HTMLFormElement);
              const messageInput = required("dashboard-chat-input", HTMLTextAreaElement);
              const sendButton = required("dashboard-chat-send", HTMLButtonElement);
              const statusElement = required("dashboard-chat-status", HTMLElement);

              const state = {
                sessions: [],
                activeSessionId: null,
                activeRunId: null,
                lastSeq: 0,
                liveTurn: null,
                pollGeneration: 0,
              };

              const api = async (path, options = {}) => {
                const headers = new Headers(options.headers || {});
                if (options.body !== undefined) headers.set("Content-Type", "application/json");
                const response = await fetch(path, {...options, headers});
                const text = await response.text();
                let payload;
                try {
                  payload = text ? JSON.parse(text) : {};
                } catch (error) {
                  throw new Error(`服务返回了无法解析的响应（HTTP ${response.status}）`, {cause: error});
                }
                if (!response.ok) {
                  const detail = typeof payload.detail === "string" ? payload.detail : `请求失败（HTTP ${response.status}）`;
                  throw new Error(detail);
                }
                return payload;
              };

              const setStatus = (message, tone = "") => {
                statusElement.textContent = message;
                statusElement.className = `chat-page-status ${tone}`.trim();
              };

              const setRunning = (running) => {
                sendButton.classList.toggle("stop", running);
                sendButton.textContent = running ? "■" : "↑";
                sendButton.setAttribute("aria-label", running ? "停止生成" : "发送消息");
                messageInput.disabled = running;
                sendButton.disabled = false;
                newButton.disabled = running;
                deleteButton.disabled = state.activeSessionId === null || running;
              };

              const shouldStickToBottom = () =>
                messagesElement.scrollHeight - messagesElement.scrollTop - messagesElement.clientHeight < 100;

              const scrollToBottom = (force = false) => {
                if (force || shouldStickToBottom()) messagesElement.scrollTop = messagesElement.scrollHeight;
              };

              const showWelcome = () => {
                messagesElement.replaceChildren();
                const welcome = document.createElement("section");
                welcome.className = "chat-welcome";
                const mark = document.createElement("div");
                mark.className = "chat-welcome-mark";
                mark.textContent = ">_";
                const heading = document.createElement("h2");
                heading.textContent = "让 Hermes 帮你梳理问题";
                const copy = document.createElement("p");
                copy.textContent = state.activeSessionId
                  ? "描述现象、环境和你希望确认的结果。需要授权时，操作会停在执行轨迹中等待你决定。"
                  : "直接输入内容即可开始新对话；已有对话可以从左侧继续。";
                welcome.append(mark, heading, copy);
                messagesElement.append(welcome);
              };

              const createTurn = (role, content) => {
                const turn = document.createElement("article");
                turn.className = `chat-turn chat-turn-${role}`;
                if (role === "user") {
                  const bubble = document.createElement("div");
                  bubble.className = "chat-user-bubble";
                  bubble.textContent = content;
                  turn.append(bubble);
                  messagesElement.append(turn);
                  return {turn};
                }
                const label = document.createElement("div");
                label.className = "chat-assistant-label";
                label.textContent = "Hermes";
                const trail = document.createElement("div");
                trail.className = "chat-execution-trail";
                const body = document.createElement("div");
                body.className = "chat-assistant-body";
                body.textContent = content;
                turn.append(label, trail, body);
                messagesElement.append(turn);
                return {turn, trail, body};
              };

              const addTrace = (turn, text, kind = "") => {
                const item = document.createElement("div");
                item.className = `chat-trace-item ${kind}`.trim();
                item.textContent = text;
                turn.trail.append(item);
                return item;
              };

              const renderStoredMessage = (message) => {
                if (message.role !== "user" && message.role !== "assistant") return;
                const content = typeof message.content === "string" ? message.content : "[当前消息格式暂不支持展示]";
                const turn = createTurn(message.role, content);
                if (message.role === "assistant" && turn.trail) {
                  const reasoning = typeof message.reasoning_content === "string"
                    ? message.reasoning_content
                    : (typeof message.reasoning === "string" ? message.reasoning : "");
                  if (reasoning) addTrace(turn, reasoning, "reasoning");
                }
              };

              const renderSessions = () => {
                const query = searchInput.value.trim().toLocaleLowerCase();
                sessionsElement.replaceChildren();
                const visible = state.sessions.filter((session) => {
                  const title = typeof session.title === "string" ? session.title : "未命名对话";
                  return !query || title.toLocaleLowerCase().includes(query);
                });
                if (visible.length === 0) {
                  const empty = document.createElement("div");
                  empty.className = "empty";
                  empty.textContent = query ? "没有匹配的对话" : "还没有对话";
                  sessionsElement.append(empty);
                  return;
                }
                for (const session of visible) {
                  if (typeof session.id !== "string") continue;
                  const button = document.createElement("button");
                  button.type = "button";
                  button.className = "chat-session-item";
                  button.setAttribute("aria-current", String(session.id === state.activeSessionId));
                  const title = document.createElement("span");
                  title.className = "chat-session-title";
                  title.textContent = typeof session.title === "string" && session.title ? session.title : "未命名对话";
                  const meta = document.createElement("span");
                  meta.className = "chat-session-meta";
                  const count = Number.isInteger(session.message_count) ? session.message_count : 0;
                  meta.textContent = `${count} 条消息`;
                  button.append(title, meta);
                  button.addEventListener("click", () => selectSession(session.id));
                  sessionsElement.append(button);
                }
              };

              const loadSessions = async () => {
                const payload = await api("/api/dashboard-agent/sessions");
                if (!Array.isArray(payload.sessions)) throw new Error("会话列表响应缺少 sessions");
                state.sessions = payload.sessions;
                renderSessions();
                setStatus("已连接 Hermes 8641");
              };

              const activeSession = () => state.sessions.find((session) => session.id === state.activeSessionId);

              const selectSession = async (sessionId) => {
                state.pollGeneration += 1;
                state.activeSessionId = sessionId;
                state.activeRunId = null;
                state.lastSeq = 0;
                state.liveTurn = null;
                renderSessions();
                const session = activeSession();
                titleElement.textContent = session && typeof session.title === "string" ? session.title : "未命名对话";
                setRunning(false);
                setStatus("正在加载对话…", "running");
                try {
                  const payload = await api(`/api/dashboard-agent/sessions/${encodeURIComponent(sessionId)}/messages`);
                  if (state.activeSessionId !== sessionId) return;
                  if (!Array.isArray(payload.messages)) throw new Error("消息响应缺少 messages");
                  messagesElement.replaceChildren();
                  for (const message of payload.messages) renderStoredMessage(message);
                  if (messagesElement.childElementCount === 0) showWelcome();
                  scrollToBottom(true);
                  const active = await api(`/api/dashboard-agent/sessions/${encodeURIComponent(sessionId)}/active-run`);
                  if (state.activeSessionId !== sessionId) return;
                  if (active.run && typeof active.run.run_id === "string") {
                    state.activeRunId = active.run.run_id;
                    state.liveTurn = createTurn("assistant", "");
                    state.liveTurn.body.classList.add("pending");
                    state.liveTurn.body.textContent = "正在恢复执行状态…";
                    setRunning(true);
                    setStatus(active.run.status === "waiting_for_approval" ? "等待授权" : "Hermes 正在处理…", "running");
                    pollRun(++state.pollGeneration);
                  } else {
                    setStatus("已连接 Hermes 8641");
                  }
                } catch (error) {
                  if (state.activeSessionId === sessionId) setStatus(error.message, "error");
                }
              };

              const createSession = async (message) => {
                setStatus("正在新建对话…", "running");
                const title = message.replace(/\\s+/g, " ").trim().slice(0, 32);
                const payload = await api("/api/dashboard-agent/sessions", {
                  method: "POST",
                  body: JSON.stringify({title: title || "新对话"}),
                });
                if (!payload.session || typeof payload.session.id !== "string") throw new Error("新建会话响应缺少 session");
                state.activeSessionId = payload.session.id;
                await loadSessions();
                const session = activeSession();
                titleElement.textContent = session && typeof session.title === "string" ? session.title : title;
                setRunning(true);
                return payload.session.id;
              };

              const beginNewDraft = () => {
                state.pollGeneration += 1;
                state.activeSessionId = null;
                state.activeRunId = null;
                state.lastSeq = 0;
                state.liveTurn = null;
                titleElement.textContent = "新对话";
                renderSessions();
                showWelcome();
                setRunning(false);
                setStatus("已连接 Hermes 8641");
                messageInput.focus();
              };

              const addApproval = (turn, event) => {
                const runId = state.activeRunId;
                if (!runId) throw new Error("收到授权请求时没有活动 Run");
                const card = document.createElement("section");
                card.className = "chat-approval-card";
                const heading = document.createElement("strong");
                heading.textContent = "需要你的授权";
                const description = document.createElement("div");
                description.className = "chat-approval-description";
                description.textContent = typeof event.description === "string" && event.description
                  ? event.description
                  : "Hermes 希望继续执行当前操作。";
                card.append(heading, description);
                if (typeof event.command === "string" && event.command) {
                  const command = document.createElement("div");
                  command.className = "chat-approval-command";
                  command.textContent = event.command;
                  card.append(command);
                }
                const actions = document.createElement("div");
                actions.className = "chat-approval-actions";
                const requestedChoices = Array.isArray(event.choices) ? event.choices : ["once", "deny"];
                const labels = {once: "允许这一次", session: "本次会话允许", always: "始终允许", deny: "拒绝"};
                for (const choice of requestedChoices) {
                  if (!(choice in labels)) continue;
                  const button = document.createElement("button");
                  button.type = "button";
                  button.className = `chat-approval-button ${choice === "once" ? "primary" : ""} ${choice === "deny" ? "deny" : ""}`.trim();
                  button.textContent = labels[choice];
                  button.addEventListener("click", async () => {
                    for (const candidate of actions.querySelectorAll("button")) candidate.disabled = true;
                    setStatus("正在提交授权决定…", "running");
                    try {
                      await api(`/api/dashboard-agent/runs/${encodeURIComponent(runId)}/approval`, {
                        method: "POST",
                        body: JSON.stringify({choice}),
                      });
                      description.textContent = `已提交：${labels[choice]}`;
                      setStatus("Hermes 已继续执行", "running");
                    } catch (error) {
                      for (const candidate of actions.querySelectorAll("button")) candidate.disabled = false;
                      setStatus(error.message, "error");
                    }
                  });
                  actions.append(button);
                }
                card.append(actions);
                turn.trail.append(card);
              };

              const handleRunEvent = (event) => {
                const turn = state.liveTurn;
                if (!turn) throw new Error("收到 Run 事件时没有活动回复");
                const stickToBottom = shouldStickToBottom();
                const type = event.event;
                if (type === "reasoning.available") {
                  addTrace(turn, typeof event.text === "string" ? event.text : "Hermes 更新了推理过程");
                } else if (type === "tool.started") {
                  const name = typeof event.tool === "string" ? event.tool : "tool";
                  addTrace(turn, `正在调用 ${name}${typeof event.preview === "string" && event.preview ? ` · ${event.preview}` : ""}`, "tool");
                } else if (type === "tool.completed") {
                  const name = typeof event.tool === "string" ? event.tool : "tool";
                  addTrace(turn, `${event.error ? "调用失败" : "调用完成"} · ${name}`, event.error ? "error" : "tool");
                } else if (type === "approval.request") {
                  addApproval(turn, event);
                  setStatus("等待你的授权", "running");
                } else if (type === "approval.responded") {
                  addTrace(turn, "授权决定已接收，继续执行", "tool");
                } else if (type === "message.delta") {
                  if (typeof event.delta !== "string") throw new Error("message.delta 缺少文本");
                  if (turn.body.classList.contains("pending")) {
                    turn.body.classList.remove("pending");
                    turn.body.textContent = "";
                  }
                  turn.body.textContent += event.delta;
                } else if (type === "run.completed") {
                  if (turn.body.classList.contains("pending")) turn.body.textContent = "";
                  if (!turn.body.textContent && typeof event.output === "string") turn.body.textContent = event.output;
                  turn.body.classList.remove("pending");
                  setStatus("回复完成");
                } else if (type === "run.failed") {
                  const error = typeof event.error === "string" ? event.error : "Hermes 执行失败";
                  addTrace(turn, error, "error");
                  if (turn.body.classList.contains("pending")) turn.body.textContent = "";
                  turn.body.classList.remove("pending");
                  if (!turn.body.textContent) turn.body.textContent = "本次回复未完成。";
                  setStatus(error, "error");
                } else if (type === "run.cancelled") {
                  addTrace(turn, "已停止生成", "tool");
                  if (turn.body.classList.contains("pending")) turn.body.textContent = "";
                  turn.body.classList.remove("pending");
                  if (!turn.body.textContent) turn.body.textContent = "已停止。";
                  setStatus("已停止生成");
                }
                scrollToBottom(stickToBottom);
              };

              const pollRun = async (generation) => {
                if (!state.activeRunId || generation !== state.pollGeneration) return;
                try {
                  const payload = await api(`/api/dashboard-agent/runs/${encodeURIComponent(state.activeRunId)}/events?after=${state.lastSeq}`);
                  if (generation !== state.pollGeneration) return;
                  if (!Array.isArray(payload.events)) throw new Error("Run 事件响应缺少 events");
                  if (payload.truncated) addTrace(state.liveTurn, "部分较早的执行轨迹已过期，请以最终回复为准。", "error");
                  for (const event of payload.events) {
                    if (!Number.isInteger(event.seq)) throw new Error("Run 事件缺少 seq");
                    handleRunEvent(event);
                    state.lastSeq = Math.max(state.lastSeq, event.seq);
                  }
                  if (payload.done) {
                    state.activeRunId = null;
                    state.liveTurn = null;
                    setRunning(false);
                    await loadSessions();
                    return;
                  }
                  window.setTimeout(() => pollRun(generation), 450);
                } catch (error) {
                  if (generation === state.pollGeneration) {
                    setStatus(error.message, "error");
                    window.setTimeout(() => pollRun(generation), 1500);
                  }
                }
              };

              const startRun = async (message) => {
                if (state.activeRunId) return;
                messagesElement.querySelector(".chat-welcome")?.remove();
                createTurn("user", message);
                state.liveTurn = createTurn("assistant", "正在思考…");
                state.liveTurn.body.classList.add("pending");
                scrollToBottom(true);
                setRunning(true);
                setStatus("Hermes 正在处理…", "running");
                try {
                  const sessionId = state.activeSessionId || await createSession(message);
                  const payload = await api(`/api/dashboard-agent/sessions/${encodeURIComponent(sessionId)}/runs`, {
                    method: "POST",
                    body: JSON.stringify({message}),
                  });
                  if (typeof payload.run_id !== "string") throw new Error("启动响应缺少 run_id");
                  state.activeRunId = payload.run_id;
                  state.lastSeq = 0;
                  pollRun(++state.pollGeneration);
                } catch (error) {
                  addTrace(state.liveTurn, error.message, "error");
                  state.liveTurn.body.classList.remove("pending");
                  state.liveTurn.body.textContent = "消息没有发送成功。";
                  state.liveTurn = null;
                  setRunning(false);
                  setStatus(error.message, "error");
                }
              };

              const stopRun = async () => {
                if (!state.activeRunId) return;
                sendButton.disabled = true;
                setStatus("正在停止…", "running");
                try {
                  await api(`/api/dashboard-agent/runs/${encodeURIComponent(state.activeRunId)}/stop`, {
                    method: "POST",
                    body: JSON.stringify({}),
                  });
                } catch (error) {
                  sendButton.disabled = false;
                  setStatus(error.message, "error");
                }
              };

              composer.addEventListener("submit", (event) => {
                event.preventDefault();
                if (state.activeRunId) {
                  stopRun();
                  return;
                }
                const message = messageInput.value.trim();
                if (!message) return;
                messageInput.value = "";
                messageInput.style.height = "auto";
                startRun(message);
              });
              messageInput.addEventListener("keydown", (event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                  event.preventDefault();
                  composer.requestSubmit();
                }
              });
              messageInput.addEventListener("input", () => {
                messageInput.style.height = "auto";
                messageInput.style.height = `${Math.min(messageInput.scrollHeight, 180)}px`;
              });
              newButton.addEventListener("click", beginNewDraft);
              searchInput.addEventListener("input", renderSessions);
              refreshButton.addEventListener("click", async () => {
                try {
                  await loadSessions();
                  if (state.activeSessionId) await selectSession(state.activeSessionId);
                } catch (error) {
                  setStatus(error.message, "error");
                }
              });
              deleteButton.addEventListener("click", async () => {
                if (!state.activeSessionId || !window.confirm("确定删除当前对话吗？此操作不可撤销。")) return;
                const sessionId = state.activeSessionId;
                deleteButton.disabled = true;
                try {
                  await api(`/api/dashboard-agent/sessions/${encodeURIComponent(sessionId)}`, {method: "DELETE"});
                  beginNewDraft();
                  await loadSessions();
                } catch (error) {
                  setStatus(error.message, "error");
                  deleteButton.disabled = false;
                }
              });

              beginNewDraft();
              setStatus("正在连接 Hermes…", "running");
              loadSessions().catch((error) => setStatus(error.message, "error"));
            })();
          </script>
        </article>"""
