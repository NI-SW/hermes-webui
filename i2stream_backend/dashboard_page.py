from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import quote

from dashboard_chat_page import render_dashboard_chat_view


VALID_DASHBOARD_VIEWS = {"knowledge", "reports", "chat", "chat-history"}
DASHBOARD_NAV_ITEMS = {
    "knowledge": ("知识库", "/dashboard"),
    "reports": ("Report", "/reports"),
    "chat": ("AI 对话", "/chat"),
    "chat-history": ("对话历史", "/chat-history"),
}


DASHBOARD_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Hermes Dashboard</title>
    <style>
      :root {
        color-scheme: light;
        --ink: #333333;
        --muted: #717f90;
        --page: #f6f7fb;
        --panel: #ffffff;
        --line: #dcdee2;
        --line-soft: #e4e7ed;
        --line-strong: #c0c4cc;
        --primary: #0599cc;
        --primary-strong: #3190f6;
        --primary-soft: #f4f8ff;
        --success: #67c23a;
        --warning: #e6a23c;
        --danger: #f56c6c;
        --shadow: 0 1px 6px rgba(0, 0, 0, 0.12);
        font-family: "微软雅黑", Avenir, Helvetica, Arial, sans-serif;
        background: var(--page);
        color: var(--ink);
      }
      * { box-sizing: border-box; }
      body { margin: 0; min-height: 100vh; background: var(--page); font-size: 14px; -webkit-font-smoothing: antialiased; }
      button, input { font: inherit; }
      button:focus-visible, input:focus-visible {
        outline: 2px solid rgba(49, 144, 246, 0.28);
        outline-offset: 2px;
      }
      #app { display: grid; grid-template-columns: 232px minmax(0, 1fr); min-height: 100vh; }
      aside {
        position: sticky;
        top: 0;
        height: 100vh;
        background: var(--panel);
        color: var(--muted);
        border-right: 1px solid var(--line);
        padding: 0 0 24px;
        overflow: hidden;
      }
      .brand {
        height: 68px;
        padding: 14px 20px 12px;
        border-bottom: 1px solid var(--line-soft);
        background: var(--panel);
      }
      .brand strong { display: block; color: #2f4056; font-size: 16px; line-height: 1.35; letter-spacing: 0; }
      .brand span { display: block; margin-top: 4px; color: #96a4b4; font-size: 12px; }
      nav { display: grid; gap: 6px; padding: 14px 10px; }
      .nav-button {
        display: block;
        width: 100%;
        border: 1px solid transparent;
        border-radius: 16px;
        background: transparent;
        color: var(--muted);
        cursor: pointer;
        min-height: 34px;
        padding: 8px 14px;
        text-align: left;
        text-decoration: none;
        font-size: 14px;
        font-weight: 500;
      }
      .nav-button:hover { background: var(--primary); color: #ffffff; }
      .nav-button[aria-selected="true"] {
        background: var(--primary);
        border-color: var(--primary);
        color: #ffffff;
        box-shadow: none;
      }
      main { min-width: 0; padding: 20px; background: var(--page); }
      .view { max-width: none; }
      .view-header {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 16px;
        align-items: center;
        min-height: 54px;
        margin-bottom: 12px;
        padding: 0 0 10px;
        border-bottom: 1px solid var(--line);
        color: var(--ink);
      }
      h1 { margin: 0; font-size: 20px; line-height: 1.35; font-weight: 600; letter-spacing: 0; }
      p { margin: 4px 0 0; color: var(--muted); line-height: 1.6; font-size: 13px; }
      .view-header p { color: var(--muted); max-width: 760px; }
      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 5px;
        box-shadow: var(--shadow);
        margin-bottom: 16px;
        overflow: hidden;
      }
      .panel-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        border-bottom: 1px solid var(--line);
        padding: 10px 16px;
        background: var(--panel);
      }
      .panel-title { margin: 0; color: #2f4056; font-size: 15px; font-weight: 600; }
      .panel-body { padding: 14px 16px; }
      .intake-zone {
        border: 1px dashed #d1dbe5;
        border-radius: 5px;
        background: #fbfdff;
        padding: 12px;
      }
      .toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; }
      .upload-form {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 12px;
        align-items: center;
      }
      input[type="file"], input[type="text"] {
        min-width: min(460px, 100%);
        height: 32px;
        border: 1px solid #dcdfe6;
        border-radius: 4px;
        background: #ffffff;
        color: var(--ink);
        padding: 6px 11px;
      }
      input[type="file"] { padding: 4px 8px; }
      .file-picker {
        min-width: 0;
        display: block;
      }
      .file-picker-trigger {
        display: none;
      }
      .file-picker-trigger:hover {
        color: var(--primary-strong);
        background: #ecf5ff;
      }
      .file-picker:focus-within {
        border-color: var(--primary-strong);
        box-shadow: 0 0 0 2px rgba(49, 144, 246, 0.12);
      }
      .file-picker-summary {
        display: none;
      }
      .selection-panel {
        border-top: 1px solid var(--line);
        background: #ffffff;
      }
      .selection-panel[hidden] { display: none; }
      .selection-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-height: 40px;
        padding: 8px 16px;
        background: var(--primary-soft);
        color: #2f4056;
      }
      .selection-header strong { font-size: 13px; font-weight: 600; }
      .selection-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 110px 74px;
        gap: 12px;
        align-items: center;
      }
      .selection-name { min-width: 0; overflow-wrap: anywhere; }
      .selection-size { color: var(--muted); font-size: 12px; text-align: right; }
      .button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 32px;
        border: 1px solid var(--primary-strong);
        border-radius: 4px;
        background: var(--primary-strong);
        color: #ffffff;
        cursor: pointer;
        font-weight: 500;
        padding: 6px 14px;
        text-decoration: none;
      }
      .button:hover { filter: brightness(0.96); }
      .button.secondary { background: #ffffff; border-color: #dcdfe6; color: #606266; }
      .button.secondary:hover { color: var(--primary-strong); border-color: var(--primary-strong); }
      .button.danger { background: var(--danger); border-color: var(--danger); }
      .button:disabled { cursor: not-allowed; opacity: 0.55; }
      .status { min-height: 20px; color: var(--muted); font-size: 12px; line-height: 20px; }
      .status.error { color: var(--danger); }
      .status.success { color: var(--success); }
      .status.processing { color: var(--warning); }
      .result-list { display: grid; gap: 0; }
      .item {
        border-top: 1px solid var(--line);
        padding: 10px 16px;
        background: #ffffff;
      }
      .item:hover { background: #fbfdff; }
      .item:first-child { border-top: 0; }
      .item-title { display: flex; gap: 10px; align-items: flex-start; justify-content: space-between; }
      .item-title strong { word-break: break-word; }
      .badge {
        flex: 0 0 auto;
        border-radius: 10px;
        background: #e6f7ff;
        color: var(--primary);
        font-size: 12px;
        font-weight: 500;
        line-height: 18px;
        padding: 0 8px;
      }
      .badge.failed, .badge.error { background: #fff1f0; color: var(--danger); }
      .badge.completed, .badge.success { background: #f0f9eb; color: var(--success); }
      .badge.processing, .badge.submitted, .badge.pending { background: #fdf6ec; color: var(--warning); }
      .item-meta { margin-top: 6px; color: #606266; font-size: 12px; line-height: 1.6; word-break: break-word; }
      .item-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; justify-content: flex-end; }
      .inline { display: inline-flex; gap: 8px; align-items: center; }
      form.inline-form { margin: 0; }
      .empty { color: var(--muted); padding: 18px 16px; text-align: center; }
      code { background: #f5f7fa; border: 1px solid #ebeef5; border-radius: 3px; padding: 1px 5px; font-family: Consolas, "SFMono-Regular", monospace; }
      .file-grid {
        display: grid;
        grid-template-columns: 28px minmax(180px, 1.1fr) minmax(240px, 1.4fr) 90px 90px 160px 96px;
        gap: 12px;
        align-items: center;
      }
      .file-grid.header {
        padding: 0 16px;
        min-height: 42px;
        background: var(--primary-soft);
        border-bottom: 1px solid var(--line);
        color: #666666;
        font-size: 12px;
        font-weight: 600;
      }
      .file-grid .mono {
        font-family: Consolas, "SFMono-Regular", monospace;
        font-size: 12px;
        word-break: break-all;
        color: #606266;
      }
      .report-grid {
        display: grid;
        grid-template-columns: minmax(180px, 1.2fr) minmax(220px, 1.3fr) 110px 110px 170px 132px;
        gap: 12px;
        align-items: center;
      }
      .report-grid.header {
        padding: 0 16px;
        min-height: 42px;
        background: var(--primary-soft);
        border-bottom: 1px solid var(--line);
        color: #666666;
        font-size: 12px;
        font-weight: 600;
      }
      .report-name { min-width: 0; word-break: break-word; font-weight: 600; }
      .report-description { min-width: 0; color: #606266; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .report-actions { display: flex; justify-content: flex-end; gap: 8px; }
      .report-actions form { margin: 0; }
      .message-list {
        margin-top: 10px;
        border: 1px solid var(--line);
        border-radius: 5px;
        overflow: hidden;
      }
      .message-row {
        display: grid;
        grid-template-columns: 120px minmax(0, 1fr);
        gap: 12px;
        border-top: 1px solid var(--line);
        padding: 9px 12px;
        background: #ffffff;
      }
      .message-row:first-child { border-top: 0; }
      .message-role {
        color: var(--primary);
        font-family: Consolas, "SFMono-Regular", monospace;
        font-size: 12px;
        font-weight: 600;
      }
      .message-content { min-width: 0; color: var(--ink); white-space: pre-wrap; word-break: break-word; }
      .message-time { margin-top: 6px; color: var(--muted); font-size: 12px; }
      @media (max-width: 760px) {
        #app { grid-template-columns: 1fr; }
        aside { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
        .brand { height: auto; }
        nav { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 10px; }
        .nav-button { text-align: center; padding: 8px; }
        main { padding: 14px 10px; }
        .view-header { grid-template-columns: 1fr; min-height: auto; }
        .panel-header { align-items: flex-start; flex-direction: column; }
        .upload-form { grid-template-columns: 1fr; }
        .upload-form .button { justify-self: end; }
        .selection-row { grid-template-columns: minmax(0, 1fr) auto; }
        .selection-row .badge { grid-column: 1 / -1; justify-self: start; }
        .file-grid { grid-template-columns: 1fr; }
        .report-grid { grid-template-columns: 1fr; }
        .file-grid.header { display: none; }
        .report-grid.header { display: none; }
        .message-row { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <div id="app">
      <aside>
        <div class="brand">
          <strong>Hermes Dashboard</strong>
          <span>知识库、Report 与 AI 对话</span>
        </div>
        <nav aria-label="Dashboard views">
          <a class="nav-button" href="/dashboard" data-view="knowledge" aria-selected="false">知识库</a>
          <a class="nav-button" href="/reports" data-view="reports" aria-selected="false">Report</a>
          <a class="nav-button" href="/chat" data-view="chat" aria-selected="false">AI 对话</a>
          <a class="nav-button" href="/chat-history" data-view="chat-history" aria-selected="false">对话历史</a>
        </nav>
      </aside>
      <main>
        <article id="view-knowledge" class="view" data-view-panel="knowledge">
          <header class="view-header">
            <div>
              <h1>知识库上传</h1>
              <p>提交文档到 RAG 服务，并在同一页面跟踪处理状态。</p>
            </div>
          </header>
          <section class="panel">
            <div class="panel-header">
              <h2 class="panel-title">上传队列</h2>
              <div id="knowledge-status" class="status" role="status"></div>
            </div>
            <div class="panel-body intake-zone">
              <form id="knowledge-form" class="upload-form">
                <div class="file-picker">
                  <label class="file-picker-trigger" for="knowledge-files">选择文件</label>
                  <input id="knowledge-files" name="file" type="file" accept=".txt,.md,.pdf,.docx,.xls,.xlsx" multiple required />
                  <span id="knowledge-file-summary" class="file-picker-summary">支持 txt、md、pdf、docx、xls、xlsx，可多选</span>
                </div>
                <button id="knowledge-upload" class="button" type="submit">上传到 RAG</button>
              </form>
            </div>
            <div id="knowledge-results" class="result-list"></div>
          </section>
        </article>

        <article id="view-knowledge-files" class="view" data-view-panel="knowledge-files" hidden>
          <header class="view-header">
            <div>
              <h1>知识库文件</h1>
              <p>按 file_id 查看当前向量库中已经入库的文件。</p>
            </div>
          </header>
          <section class="panel">
            <div class="panel-header toolbar">
              <h2 class="panel-title">已入库文件</h2>
              <div class="inline">
                <span id="vector-files-count" class="status">共 0 个</span>
                <button id="vector-files-refresh" class="button secondary" type="button">刷新</button>
              </div>
            </div>
            <div class="panel-body">
              <div id="vector-files-status" class="status" role="status"></div>
            </div>
            <div class="file-grid header" aria-hidden="true">
              <span>文件名</span>
              <span>File ID</span>
              <span>类型</span>
              <span>Chunks</span>
              <span>上传时间</span>
              <span>操作</span>
            </div>
            <div id="vector-files-list" class="result-list"></div>
          </section>
        </article>

        <article id="view-reports" class="view" data-view-panel="reports" hidden>
          <header class="view-header">
            <div>
              <h1>Report</h1>
              <p>管理外部客户端上传到 Backend 的 Report 文件，不在页面内预览内容。</p>
            </div>
          </header>
          <section class="panel">
            <div class="panel-header toolbar">
              <label class="inline">
                <input id="reports-select-all" type="checkbox" />
                <span>全选</span>
              </label>
              <div class="inline">
                <span id="reports-selection" class="status">已选择 0 / 0</span>
                <button id="reports-refresh" class="button secondary" type="button">刷新</button>
                <button id="reports-delete-selected" class="button danger" type="button" disabled>批量删除</button>
              </div>
            </div>
            <div class="panel-body">
              <div id="reports-status" class="status" role="status"></div>
            </div>
            <div class="report-grid header" aria-hidden="true">
              <span></span>
              <span>名称</span>
              <span>描述</span>
              <span>类型</span>
              <span>大小</span>
              <span>创建时间</span>
              <span>操作</span>
            </div>
            <div id="reports-list" class="result-list"></div>
          </section>
        </article>

        <article id="view-chat-history" class="view" data-view-panel="chat-history" hidden>
          <header class="view-header">
            <div>
              <h1>对话历史</h1>
              <p>按 Client ID 查询该浏览器身份下保存的会话与消息。</p>
            </div>
          </header>
          <section class="panel">
            <div class="panel-header">
              <h2 class="panel-title">查询条件</h2>
              <div id="chat-history-status" class="status" role="status"></div>
            </div>
            <div class="panel-body">
              <form id="chat-history-form" class="upload-form">
                <input id="chat-history-client-id" type="text" autocomplete="off" placeholder="Client ID" required />
                <button id="chat-history-submit" class="button" type="submit">查询</button>
              </form>
            </div>
            <div id="chat-history-results" class="result-list"></div>
          </section>
        </article>
      </main>
    </div>

  </body>
</html>
"""


def render_dashboard_page(
    active_view: str,
    vector_files: list[dict[str, Any]] | None = None,
    report_files: list[dict[str, Any]] | None = None,
    uploaded_knowledge_files: list[dict[str, Any]] | None = None,
    chat_client_id: str = "",
    chat_conversations: list[dict[str, Any]] | None = None,
) -> str:
    view = active_view if active_view in VALID_DASHBOARD_VIEWS else "knowledge"
    html = DASHBOARD_PAGE_HTML
    for candidate in DASHBOARD_NAV_ITEMS:
        selected = "true" if candidate == view else "false"
        html = html.replace(
            f'data-view="{candidate}" aria-selected="false"',
            f'data-view="{candidate}" aria-selected="{selected}"',
        )

    main_start = html.index("      <main>")
    main_end = html.index("      </main>", main_start) + len("      </main>")
    html = (
        html[:main_start]
        + render_dashboard_main(view, vector_files, report_files, uploaded_knowledge_files, chat_client_id, chat_conversations)
        + html[main_end:]
    )
    return html


def render_dashboard_main(
    active_view: str,
    vector_files: list[dict[str, Any]] | None = None,
    report_files: list[dict[str, Any]] | None = None,
    uploaded_knowledge_files: list[dict[str, Any]] | None = None,
    chat_client_id: str = "",
    chat_conversations: list[dict[str, Any]] | None = None,
) -> str:
    if active_view == "knowledge":
        if vector_files is None:
            raise ValueError("vector_files is required for knowledge view")
        content = render_knowledge_view(vector_files, uploaded_knowledge_files)
    elif active_view == "reports":
        if report_files is None:
            raise ValueError("report_files is required for reports view")
        content = render_reports_view(report_files)
    elif active_view == "chat":
        content = render_dashboard_chat_view()
    elif active_view == "chat-history":
        content = render_chat_history_view(chat_client_id, chat_conversations)
    else:
        raise ValueError(f"unknown dashboard view: {active_view}")
    return f"      <main>\n{content}\n      </main>"


KNOWLEDGE_SELECTION_SCRIPT = """          <script>
            (() => {
              const knowledgeFiles = document.getElementById("knowledge-files");
              const selection = document.getElementById("knowledge-selection");
              const selectionCount = document.getElementById("knowledge-selection-count");
              const selectionList = document.getElementById("knowledge-selection-list");
              if (!(knowledgeFiles instanceof HTMLInputElement) ||
                  !(selection instanceof HTMLElement) ||
                  !(selectionCount instanceof HTMLElement) ||
                  !(selectionList instanceof HTMLElement)) {
                throw new Error("Knowledge upload controls are missing");
              }

              const formatFileSize = (size) => {
                if (size < 1024) return `${size} B`;
                if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
                return `${(size / 1024 / 1024).toFixed(1)} MB`;
              };

              knowledgeFiles.addEventListener("change", () => {
                const files = Array.from(knowledgeFiles.files);
                selectionList.replaceChildren();
                selection.hidden = files.length === 0;
                selectionCount.textContent = `共 ${files.length} 个`;

                for (const file of files) {
                  const row = document.createElement("article");
                  row.className = "item selection-row";

                  const name = document.createElement("strong");
                  name.className = "selection-name";
                  name.textContent = file.name;

                  const size = document.createElement("span");
                  size.className = "selection-size";
                  size.textContent = formatFileSize(file.size);

                  const state = document.createElement("span");
                  state.className = "badge pending";
                  state.textContent = "待上传";

                  row.append(name, size, state);
                  selectionList.append(row);
                }
              });
            })();
          </script>"""


KNOWLEDGE_FILES_BATCH_SCRIPT = """          <script>
            (() => {
              const knowledgeBatchForm = document.getElementById("knowledge-batch-delete-form");
              const selectAll = document.getElementById("knowledge-files-select-all");
              const selectionCount = document.getElementById("vector-files-count");
              const deleteSelected = document.getElementById("knowledge-files-delete-selected");
              const checkboxes = Array.from(document.querySelectorAll(".knowledge-file-checkbox"));
              if (!(knowledgeBatchForm instanceof HTMLFormElement) ||
                  !(selectAll instanceof HTMLInputElement) ||
                  !(selectionCount instanceof HTMLElement) ||
                  !(deleteSelected instanceof HTMLButtonElement) ||
                  !checkboxes.every((checkbox) => checkbox instanceof HTMLInputElement)) {
                throw new Error("Knowledge batch delete controls are missing");
              }

              const updateSelection = () => {
                const selectedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
                selectionCount.textContent = `已选择 ${selectedCount} / ${checkboxes.length}`;
                selectAll.checked = checkboxes.length > 0 && selectedCount === checkboxes.length;
                selectAll.indeterminate = selectedCount > 0 && selectedCount < checkboxes.length;
                selectAll.disabled = checkboxes.length === 0;
                deleteSelected.disabled = selectedCount === 0;
                return selectedCount;
              };

              selectAll.addEventListener("change", () => {
                for (const checkbox of checkboxes) {
                  checkbox.checked = selectAll.checked;
                }
                updateSelection();
              });
              for (const checkbox of checkboxes) {
                checkbox.addEventListener("change", updateSelection);
              }
              knowledgeBatchForm.addEventListener("submit", (event) => {
                const selectedCount = updateSelection();
                if (selectedCount === 0 ||
                    !window.confirm(`确定删除选中的 ${selectedCount} 个已入库文件吗？此操作不可撤销。`)) {
                  event.preventDefault();
                  return;
                }
                deleteSelected.disabled = true;
                deleteSelected.textContent = "删除中…";
              });
              updateSelection();
            })();
          </script>"""


def render_knowledge_view(
    vector_files: list[dict[str, Any]],
    uploaded_files: list[dict[str, Any]] | None = None,
) -> str:
    status = ""
    if uploaded_files is not None:
        status = f"已提交 {len(uploaded_files)} 个文件到 RAG。"
    return f"""        <article id="view-knowledge" class="view" data-view-panel="knowledge">
          <header class="view-header">
            <div>
              <h1>知识库</h1>
              <p>上传文档并管理当前已经入库的文件。</p>
            </div>
          </header>
          <section class="panel">
            <div class="panel-header">
              <h2 class="panel-title">上传队列</h2>
              <div id="knowledge-status" class="status" role="status">{escape(status)}</div>
            </div>
            <div class="panel-body intake-zone">
              <form id="knowledge-form" class="upload-form" method="post" action="/dashboard" enctype="multipart/form-data">
                <div class="file-picker">
                  <label class="file-picker-trigger" for="knowledge-files">选择文件</label>
                  <input id="knowledge-files" name="file" type="file" accept=".txt,.md,.pdf,.docx,.xls,.xlsx" multiple required />
                  <span id="knowledge-file-summary" class="file-picker-summary">支持 txt、md、pdf、docx、xls、xlsx，可多选</span>
                </div>
                <button id="knowledge-upload" class="button" type="submit">上传到 RAG</button>
              </form>
            </div>
            <section id="knowledge-selection" class="selection-panel" aria-live="polite" hidden>
              <div class="selection-header">
                <strong>待上传文件</strong>
                <span id="knowledge-selection-count" class="status">共 0 个</span>
              </div>
              <div id="knowledge-selection-list" class="result-list"></div>
            </section>
            <div id="knowledge-results" class="result-list">{render_uploaded_knowledge_rows(uploaded_files)}</div>
          </section>
{render_knowledge_files_panel(vector_files)}
{KNOWLEDGE_SELECTION_SCRIPT}
{KNOWLEDGE_FILES_BATCH_SCRIPT}
        </article>"""


def render_knowledge_files_panel(vector_files: list[dict[str, Any]]) -> str:
    select_all_disabled = " disabled" if not vector_files else ""
    return f"""          <section class="panel" aria-labelledby="knowledge-files-title">
            <div class="panel-header toolbar">
              <h2 id="knowledge-files-title" class="panel-title">已入库文件</h2>
              <div class="inline">
                <a class="button secondary" href="/dashboard">刷新</a>
                <form id="knowledge-batch-delete-form" class="inline" method="post" action="/knowledge/files/delete">
                  <label class="inline">
                    <input id="knowledge-files-select-all" type="checkbox" aria-label="全选已入库文件"{select_all_disabled} />
                    <span>全选</span>
                  </label>
                  <span id="vector-files-count" class="status">已选择 0 / {len(vector_files)}</span>
                  <button id="knowledge-files-delete-selected" class="button danger" type="submit" disabled>批量删除</button>
                </form>
              </div>
            </div>
            <div class="panel-body">
              <div id="vector-files-status" class="status" role="status"></div>
            </div>
            <div class="file-grid header" aria-hidden="true">
              <span></span>
              <span>文件名</span>
              <span>File ID</span>
              <span>类型</span>
              <span>Chunks</span>
              <span>上传时间</span>
              <span>操作</span>
            </div>
            <div id="vector-files-list" class="result-list">{render_vector_file_rows(vector_files)}</div>
          </section>"""


def render_reports_view(files: list[dict[str, Any]]) -> str:
    return f"""        <article id="view-reports" class="view" data-view-panel="reports">
          <header class="view-header">
            <div>
              <h1>Report</h1>
              <p>管理外部客户端上传到 Backend 的 Report 文件，不在页面内预览内容。</p>
            </div>
          </header>
          <section class="panel">
            <div class="panel-header toolbar">
              <div class="inline">
                <span id="reports-selection" class="status">共 {len(files)} 个</span>
                <a class="button secondary" href="/reports">刷新</a>
              </div>
            </div>
            <div class="panel-body">
              <div id="reports-status" class="status" role="status"></div>
            </div>
            <div class="report-grid header" aria-hidden="true">
              <span>名称</span>
              <span>描述</span>
              <span>类型</span>
              <span>大小</span>
              <span>创建时间</span>
              <span>操作</span>
            </div>
            <div id="reports-list" class="result-list">{render_report_rows(files)}</div>
          </section>
        </article>"""


def render_chat_history_view(
    client_id: str = "",
    conversations: list[dict[str, Any]] | None = None,
) -> str:
    status = ""
    if conversations is not None:
        status = f"查询完成：{len(conversations)} 个会话。"
    return f"""        <article id="view-chat-history" class="view" data-view-panel="chat-history">
          <header class="view-header">
            <div>
              <h1>对话历史</h1>
              <p>按 Client ID 查询该浏览器身份下保存的会话与消息。</p>
            </div>
          </header>
          <section class="panel">
            <div class="panel-header">
              <h2 class="panel-title">查询条件</h2>
              <div id="chat-history-status" class="status" role="status">{escape(status)}</div>
            </div>
            <div class="panel-body">
              <form id="chat-history-form" class="upload-form" method="get" action="/chat-history">
                <input id="chat-history-client-id" name="client_id" type="text" autocomplete="off" placeholder="Client ID" value="{escape(client_id)}" required />
                <button id="chat-history-submit" class="button" type="submit">查询</button>
              </form>
            </div>
            <div id="chat-history-results" class="result-list">{render_chat_history_rows(conversations)}</div>
          </section>
        </article>"""


def render_vector_file_rows(files: list[dict[str, Any]]) -> str:
    if not files:
        return '<div class="empty">当前向量库没有已入库文件。</div>'

    rows: list[str] = []
    for file in files:
        file_id = str(file["file_id"])
        display_name = str(file["display_name"])
        file_type = str(file.get("file_type") or "未知")
        total_chunks = file.get("total_chunks")
        chunks = str(total_chunks) if isinstance(total_chunks, int) else "未知"
        upload_time = str(file.get("upload_time") or "未知")
        rows.append(
            '<article class="item file-grid">'
            f'<input class="knowledge-file-checkbox" type="checkbox" name="file_id" '
            f'value="{escape(file_id)}" form="knowledge-batch-delete-form" '
            f'aria-label="选择 {escape(display_name)}" />'
            f'<strong>{escape(display_name)}</strong>'
            f'<span class="mono">{escape(file_id)}</span>'
            f'<span>{escape(file_type)}</span>'
            f'<span>{escape(chunks)}</span>'
            f'<span>{escape(upload_time)}</span>'
            f'<form class="inline-form" method="post" action="/knowledge/files/{quote(file_id, safe="")}/delete">'
            '<button class="button danger" type="submit">删除</button>'
            '</form>'
            '</article>'
        )
    return "".join(rows)


def render_report_rows(files: list[dict[str, Any]]) -> str:
    if not files:
        return '<div class="empty">还没有 Report。</div>'

    rows: list[str] = []
    for file in files:
        token = str(file["token"])
        name = str(file.get("name") or "download")
        url = str(file["url"])
        description = str(file.get("description") or "无")
        media_type = str(file.get("media_type") or "未知")
        size = format_bytes(file.get("size"))
        created_at = str(file.get("created_at") or "未知")
        action_text = "打开" if media_type == "text/html" or name.lower().endswith((".html", ".htm")) else "下载"
        rows.append(
            '<article class="item report-grid">'
            f'<strong class="report-name">{escape(name)}</strong>'
            f'<span class="report-description" title="{escape(description)}">{escape(description)}</span>'
            f'<span>{escape(media_type)}</span>'
            f'<span>{escape(size)}</span>'
            f'<span>{escape(created_at)}</span>'
            '<div class="report-actions">'
            f'<a class="button secondary" href="{escape(url)}" target="_blank" rel="noopener">{escape(action_text)}</a>'
            f'<form method="post" action="/reports/{escape(token)}/delete">'
            '<button class="button danger" type="submit">删除</button>'
            '</form>'
            '</div>'
            '</article>'
        )
    return "".join(rows)


def format_bytes(value: Any) -> str:
    if not isinstance(value, int | float):
        return "未知"
    if value < 1024:
        return f"{value:.0f} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1024 / 1024:.1f} MB"


def render_uploaded_knowledge_rows(files: list[dict[str, Any]] | None) -> str:
    if files is None:
        return ""
    if not files:
        return '<div class="empty">没有提交任何文件。</div>'

    rows: list[str] = []
    for file in files:
        filename = str(file["filename"])
        file_id = str(file["file_id"])
        task_id = str(file["task_id"])
        rows.append(
            '<article class="item">'
            f'<div class="item-title"><strong>{escape(filename)}</strong><span class="badge submitted">submitted</span></div>'
            '<div class="item-meta">'
            f'File ID: <code>{escape(file_id)}</code><br />'
            f'Task ID: <code>{escape(task_id)}</code><br />'
            '说明：上传已提交，RAG 处理完成后会出现在下方已入库文件列表。'
            '</div>'
            '</article>'
        )
    return "".join(rows)


def render_chat_history_rows(conversations: list[dict[str, Any]] | None) -> str:
    if conversations is None:
        return ""
    if not conversations:
        return '<div class="empty">没有找到该 Client ID 的对话记录。</div>'

    rows: list[str] = []
    for conversation in conversations:
        conversation_id = str(conversation["conversation_id"])
        messages = conversation.get("messages")
        if not isinstance(messages, list):
            messages = []
        message_rows = []
        for message in messages:
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            created_at = str(message.get("created_at") or "")
            message_rows.append(
                '<div class="message-row">'
                f'<div class="message-role">{escape(role)}</div>'
                '<div>'
                f'<div class="message-content">{escape(content)}</div>'
                f'<div class="message-time">{escape(created_at)}</div>'
                '</div>'
                '</div>'
            )
        rows.append(
            '<article class="item">'
            f'<div class="item-title"><strong>{escape(conversation_id)}</strong>'
            f'<span class="badge">{len(messages)} 条</span></div>'
            f'<div class="message-list">{"".join(message_rows)}</div>'
            '</article>'
        )
    return "".join(rows)
