"""Self-contained HTML template for the session viewer."""

VIEWER_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session Viewer</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --accent: #58a6ff;
    --user-bg: #1c2333;
    --assistant-bg: #161b22;
    --tool-bg: #1a1e2a;
    --thinking-bg: #1a1a2e;
    --code-bg: #0d1117;
    --success: #3fb950;
    --error: #f85149;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
  }
  .header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .header h1 { font-size: 16px; font-weight: 600; }
  .header .meta { color: var(--text-muted); font-size: 13px; }
  .container { max-width: 960px; margin: 0 auto; padding: 24px 16px; }
  .loading { text-align: center; padding: 48px; color: var(--text-muted); }
  .error-msg { text-align: center; padding: 48px; color: var(--error); }
  .stats {
    display: flex;
    gap: 24px;
    padding: 16px 0;
    margin-bottom: 16px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .stat { font-size: 13px; color: var(--text-muted); }
  .stat strong { color: var(--text); }

  .turn {
    margin-bottom: 16px;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .turn-header {
    padding: 10px 16px;
    font-size: 13px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .turn-header .role-icon {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    flex-shrink: 0;
  }
  .turn.user .turn-header { background: var(--user-bg); }
  .turn.user .role-icon { background: var(--accent); color: #000; }
  .turn.assistant .turn-header { background: var(--assistant-bg); }
  .turn.assistant .role-icon { background: var(--success); color: #000; }
  .turn-header .timestamp { margin-left: auto; color: var(--text-muted); font-weight: 400; }

  .turn-body { padding: 16px; }
  .turn.user .turn-body { background: var(--user-bg); }
  .turn.assistant .turn-body { background: var(--assistant-bg); }

  .content-block { margin-bottom: 12px; }
  .content-block:last-child { margin-bottom: 0; }

  .text-content { white-space: pre-wrap; word-break: break-word; }
  .text-content p { margin-bottom: 8px; }
  .text-content code {
    background: var(--code-bg);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
  }
  .text-content pre {
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    overflow-x: auto;
    margin: 8px 0;
  }
  .text-content pre code { background: none; padding: 0; }

  .thinking-block {
    background: var(--thinking-bg);
    border: 1px solid #2d2d5e;
    border-radius: 6px;
    overflow: hidden;
  }
  .thinking-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 13px;
    color: var(--text-muted);
    user-select: none;
    background: none;
    border: none;
    width: 100%;
    text-align: left;
  }
  .thinking-toggle:hover { color: var(--text); }
  .thinking-toggle .arrow { transition: transform 0.2s; }
  .thinking-toggle.open .arrow { transform: rotate(90deg); }
  .thinking-content {
    display: none;
    padding: 12px;
    border-top: 1px solid #2d2d5e;
    font-size: 13px;
    color: var(--text-muted);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 400px;
    overflow-y: auto;
  }
  .thinking-content.open { display: block; }

  .tool-block {
    background: var(--tool-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .tool-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 13px;
    user-select: none;
    background: none;
    border: none;
    width: 100%;
    text-align: left;
    color: var(--text);
  }
  .tool-header:hover { background: rgba(255,255,255,0.03); }
  .tool-header .arrow { color: var(--text-muted); transition: transform 0.2s; }
  .tool-header.open .arrow { transform: rotate(90deg); }
  .tool-name { color: var(--accent); font-family: monospace; font-weight: 600; }
  .tool-detail {
    display: none;
    border-top: 1px solid var(--border);
    padding: 12px;
    font-size: 13px;
    max-height: 500px;
    overflow-y: auto;
  }
  .tool-detail.open { display: block; }
  .tool-detail pre {
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 10px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 12px;
  }
  .tool-detail .label {
    font-size: 11px;
    text-transform: uppercase;
    color: var(--text-muted);
    margin-bottom: 4px;
    font-weight: 600;
  }
  .tool-detail .section { margin-bottom: 12px; }
  .tool-detail .section:last-child { margin-bottom: 0; }

  .tool-result-block {
    background: var(--tool-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .tool-result-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 13px;
    user-select: none;
    background: none;
    border: none;
    width: 100%;
    text-align: left;
    color: var(--text);
  }
  .tool-result-header:hover { background: rgba(255,255,255,0.03); }
  .tool-result-header .arrow { color: var(--text-muted); transition: transform 0.2s; }
  .tool-result-header.open .arrow { transform: rotate(90deg); }
  .tool-result-detail {
    display: none;
    border-top: 1px solid var(--border);
    padding: 12px;
    font-size: 13px;
    max-height: 500px;
    overflow-y: auto;
  }
  .tool-result-detail.open { display: block; }
  .tool-result-detail pre {
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 10px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 12px;
  }
</style>
</head>
<body>
<div class="header">
  <h1>Agent Session Viewer</h1>
  <span class="meta" id="session-id"></span>
</div>
<div class="container">
  <div id="stats" class="stats" style="display:none"></div>
  <div id="loading" class="loading">Loading session data...</div>
  <div id="error" class="error-msg" style="display:none"></div>
  <div id="conversation"></div>
</div>
<script>
(function() {
  const sessionId = window.SESSION_ID;
  document.getElementById("session-id").textContent = sessionId;

  fetch(`/sessions/${sessionId}/data`)
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then(data => render(data))
    .catch(err => {
      document.getElementById("loading").style.display = "none";
      const el = document.getElementById("error");
      el.style.display = "block";
      el.textContent = "Failed to load session: " + err.message;
    });

  function render(entries) {
    document.getElementById("loading").style.display = "none";

    // Compute stats
    const userTurns = entries.filter(e => e.type === "user" && e.message);
    const assistantTurns = entries.filter(e => e.type === "assistant" && e.message);
    let toolCount = 0;
    for (const e of assistantTurns) {
      const content = e.message?.content;
      if (Array.isArray(content)) {
        toolCount += content.filter(b => b.type === "tool_use").length;
      }
    }

    const timestamps = entries.filter(e => e.timestamp).map(e => new Date(e.timestamp));
    let duration = "";
    if (timestamps.length >= 2) {
      const ms = timestamps[timestamps.length - 1] - timestamps[0];
      const secs = Math.floor(ms / 1000);
      if (secs < 60) duration = secs + "s";
      else if (secs < 3600) duration = Math.floor(secs/60) + "m " + (secs%60) + "s";
      else duration = Math.floor(secs/3600) + "h " + Math.floor((secs%3600)/60) + "m";
    }

    const statsEl = document.getElementById("stats");
    statsEl.style.display = "flex";
    statsEl.innerHTML =
      `<div class="stat"><strong>${userTurns.length}</strong> user turns</div>` +
      `<div class="stat"><strong>${assistantTurns.length}</strong> assistant turns</div>` +
      `<div class="stat"><strong>${toolCount}</strong> tool calls</div>` +
      (duration ? `<div class="stat"><strong>${duration}</strong> duration</div>` : "");

    // Build conversation from entries with type "user" or "assistant" that have message
    const conv = document.getElementById("conversation");
    // Track tool_use IDs to match with results
    const toolUseMap = {};

    for (const entry of entries) {
      if (entry.type !== "user" && entry.type !== "assistant") continue;
      if (!entry.message) continue;

      const role = entry.message.role || entry.type;
      if (role !== "user" && role !== "assistant") continue;

      const content = entry.message.content;
      if (!content) continue;

      // Skip entries that are just streaming duplicates of previously rendered content.
      // Claude Code JSONL streams assistant messages as incremental entries sharing the
      // same requestId. We only need the final version of each message (the one with the
      // most content blocks). We will deduplicate below.

      const turn = document.createElement("div");
      turn.className = "turn " + role;

      const header = document.createElement("div");
      header.className = "turn-header";

      const icon = document.createElement("span");
      icon.className = "role-icon";
      icon.textContent = role === "user" ? "U" : "A";

      const label = document.createElement("span");
      label.textContent = role === "user" ? "User" : "Assistant";

      const ts = document.createElement("span");
      ts.className = "timestamp";
      if (entry.timestamp) {
        const d = new Date(entry.timestamp);
        ts.textContent = d.toLocaleString();
      }

      header.appendChild(icon);
      header.appendChild(label);
      header.appendChild(ts);
      turn.appendChild(header);

      const body = document.createElement("div");
      body.className = "turn-body";

      const blocks = Array.isArray(content) ? content : [{type: "text", text: String(content)}];

      let hasVisibleContent = false;
      for (const block of blocks) {
        const el = renderBlock(block, toolUseMap);
        if (el) {
          body.appendChild(el);
          hasVisibleContent = true;
        }
      }

      if (!hasVisibleContent) continue;

      turn.appendChild(body);
      conv.appendChild(turn);
    }
  }

  function renderBlock(block, toolUseMap) {
    if (block.type === "text" && block.text) {
      const div = document.createElement("div");
      div.className = "content-block text-content";
      div.innerHTML = renderMarkdown(block.text);
      return div;
    }

    if (block.type === "thinking" && block.thinking) {
      const div = document.createElement("div");
      div.className = "content-block thinking-block";

      const toggle = document.createElement("button");
      toggle.className = "thinking-toggle";
      const preview = block.thinking.substring(0, 80).replace(/\\n/g, " ");
      toggle.innerHTML = `<span class="arrow">&#9654;</span> Thinking... <span style="color:var(--text-muted);font-weight:400;font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(preview)}</span>`;

      const content = document.createElement("div");
      content.className = "thinking-content";
      content.textContent = block.thinking;

      toggle.onclick = () => {
        toggle.classList.toggle("open");
        content.classList.toggle("open");
      };

      div.appendChild(toggle);
      div.appendChild(content);
      return div;
    }

    if (block.type === "tool_use") {
      const div = document.createElement("div");
      div.className = "content-block tool-block";

      const header = document.createElement("button");
      header.className = "tool-header";
      header.innerHTML = `<span class="arrow">&#9654;</span> <span class="tool-name">${escapeHtml(block.name || "tool")}</span>`;

      const detail = document.createElement("div");
      detail.className = "tool-detail";

      if (block.input) {
        const section = document.createElement("div");
        section.className = "section";
        const label = document.createElement("div");
        label.className = "label";
        label.textContent = "Input";
        const pre = document.createElement("pre");
        pre.textContent = typeof block.input === "string"
          ? block.input
          : JSON.stringify(block.input, null, 2);
        section.appendChild(label);
        section.appendChild(pre);
        detail.appendChild(section);
      }

      // Store for matching with result
      if (block.id) {
        toolUseMap[block.id] = detail;
      }

      header.onclick = () => {
        header.classList.toggle("open");
        detail.classList.toggle("open");
      };

      div.appendChild(header);
      div.appendChild(detail);
      return div;
    }

    if (block.type === "tool_result") {
      const div = document.createElement("div");
      div.className = "content-block tool-result-block";

      const header = document.createElement("button");
      header.className = "tool-result-header";
      const statusIcon = block.is_error ? "&#10060;" : "&#9989;";
      header.innerHTML = `<span class="arrow">&#9654;</span> ${statusIcon} Tool Result`;

      const detail = document.createElement("div");
      detail.className = "tool-result-detail";

      let resultText = "";
      if (typeof block.content === "string") {
        resultText = block.content;
      } else if (Array.isArray(block.content)) {
        resultText = block.content
          .filter(c => c.type === "text")
          .map(c => c.text)
          .join("\\n");
      }

      if (resultText) {
        const pre = document.createElement("pre");
        // Truncate very long results in the UI
        if (resultText.length > 10000) {
          pre.textContent = resultText.substring(0, 10000) + "\\n\\n... (truncated)";
        } else {
          pre.textContent = resultText;
        }
        detail.appendChild(pre);
      }

      header.onclick = () => {
        header.classList.toggle("open");
        detail.classList.toggle("open");
      };

      div.appendChild(header);
      div.appendChild(detail);
      return div;
    }

    return null;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function renderMarkdown(text) {
    // Minimal markdown rendering: code blocks, inline code, bold, italic, links
    let html = escapeHtml(text);

    // Fenced code blocks
    html = html.replace(/```(\\w*)\\n([\\s\\S]*?)```/g, function(_, lang, code) {
      return '<pre><code>' + code + '</code></pre>';
    });

    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Bold
    html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');

    // Italic
    html = html.replace(/(?<![*])\\*([^*]+)\\*(?![*])/g, '<em>$1</em>');

    // Line breaks
    html = html.replace(/\\n/g, '<br>');

    return html;
  }
})();
</script>
</body>
</html>
"""
