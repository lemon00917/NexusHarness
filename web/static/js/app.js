/**
 * NexusHarness Web UI - Production JavaScript
 */

// State
let currentEventSource = null;
let currentApprovalId = null;
let isRunning = false;
let currentStep = 0;
let maxSteps = 10;
let currentAuditRecord = null;
let currentSessionId = null;
let currentMessages = [];

function toggleMobileSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    if (sidebar && overlay) {
        sidebar.classList.toggle('open');
        overlay.classList.toggle('open');
    }
}

function toggleMobilePanel() {
    const panel = document.getElementById('infoPanel');
    if (panel) {
        panel.classList.toggle('open');
    }
}

function togglePanel(header) {
    const section = header.closest('.panel-section');
    if (section) {
        section.classList.toggle('collapsed');
    }
}

// ──────────────────────────────────────────────────
// Initialization
// ──────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    loadSystemStatus();
    loadSkills();
    loadTools()
    loadMemory();
    loadAudit();
    loadConfig();
    loadSessions();
    updateStepText(0);

    // Load conversation history and restore current session
    loadConversations().then(renderConversationList);
    const saved = loadConversationFromLocal();
    if (saved && saved.messages && saved.messages.length > 0) {
        currentSessionId = saved.session_id;
        currentMessages = saved.messages;
        const container = document.getElementById('messagesContainer');
        container.innerHTML = '';
        for (const msg of currentMessages) {
            appendMessage(msg.type, msg.content);
        }
    }

    // Save before leaving
    window.addEventListener('beforeunload', () => {
        if (currentSessionId && currentMessages.length > 0) {
            saveConversationToServer();
        }
    });
});

// ──────────────────────────────────────────────────
// API Calls
// ──────────────────────────────────────────────────

async function apiCall(endpoint, options = {}) {
    try {
        const response = await fetch(endpoint, {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        });
        const json = await response.json();
        if (!response.ok && json.error) {
            console.error('API error:', response.status, endpoint, json);
        }
        return json;
    } catch (error) {
        console.error('API call failed:', error);
        return { error: error.message };
    }
}

// ──────────────────────────────────────────────────
// Conversation Persistence
// ──────────────────────────────────────────────────

function saveConversationToLocal() {
    if (!currentSessionId) return;
    const data = {
        session_id: currentSessionId,
        title: currentMessages.length > 0 ? currentMessages[0].content.substring(0, 30) : '新会话',
        messages: currentMessages,
        updated_at: new Date().toISOString(),
    };
    localStorage.setItem('mh_current_session', JSON.stringify(data));
}

function loadConversationFromLocal() {
    try {
        const raw = localStorage.getItem('mh_current_session');
        if (!raw) return null;
        return JSON.parse(raw);
    } catch { return null; }
}

function clearLocalSession() {
    localStorage.removeItem('mh_current_session');
}

async function saveConversationToServer() {
    if (!currentSessionId || currentMessages.length === 0) return;
    const data = {
        session_id: currentSessionId,
        title: currentMessages[0].content.substring(0, 50),
        messages: currentMessages,
        created_at: currentMessages[0].timestamp || new Date().toISOString(),
        updated_at: new Date().toISOString(),
    };
    await apiCall('/api/conversations', {
        method: 'POST',
        body: JSON.stringify(data),
    });
}

async function loadConversations() {
    return await apiCall('/api/conversations');
}

async function loadConversation(sessionId) {
    return await apiCall(`/api/conversations/${sessionId}`);
}

async function deleteConversation(sessionId) {
    return await apiCall(`/api/conversations/${sessionId}`, { method: 'DELETE' });
}

function renderConversationList(data) {
    const list = document.getElementById('chatHistory');
    if (!list) return;
    const conversations = data.conversations || [];

    const activeId = currentSessionId || '';
    list.innerHTML = conversations.map(conv => `
        <div class="chat-history-item ${conv.session_id === activeId ? 'active' : ''}"
             onclick="loadConv('${conv.session_id}')">
            <span class="chat-icon">💬</span>
            <span class="chat-title">${escapeHtml(conv.title || '未命名')}</span>
        </div>
    `).join('');
}

async function loadConv(sessionId) {
    const data = await loadConversation(sessionId);
    if (data.error || !data.conversation) return;

    currentSessionId = sessionId;
    currentMessages = data.conversation.messages || [];

    // Render messages
    const container = document.getElementById('messagesContainer');
    container.innerHTML = '';
    for (const msg of currentMessages) {
        appendMessage(msg.type, msg.content);
    }

    clearLocalSession();
    await loadConversations().then(renderConversationList);
}

window.loadConv = loadConv;

// ──────────────────────────────────────────────────
// Data Loading
// ──────────────────────────────────────────────────

async function loadConfig() {
    const data = await apiCall('/api/config');
    if (data.error) return;
    maxSteps = data.max_steps || 10;
    document.getElementById('stepText').textContent = `Step 0/${maxSteps}`;
}

async function loadSystemStatus() {
    const data = await apiCall('/api/status');
    if (data.error) return;

    document.getElementById('statProvider').textContent = data.provider || '-';
    document.getElementById('statModel').textContent = data.main_model || '-';
    document.getElementById('statCalls').textContent = data.total_calls || 0;
    document.getElementById('statTokens').textContent =
        ((data.total_input_tokens || 0) + (data.total_output_tokens || 0)).toLocaleString();
    document.getElementById('statCost').textContent = `$${(data.total_cost_usd || 0).toFixed(6)}`;
    document.getElementById('modelBadge').textContent = data.main_model || 'Unknown';
}

async function loadSkills() {
    const list = document.getElementById('skillsList');
    const data = await apiCall('/api/skills');

    if (data.error || !data.skills || data.skills.length === 0) {
        list.innerHTML = '<div class="empty-text">无可用 Skills</div>';
        return;
    }

    list.innerHTML = data.skills.map(s => `
        <div class="skill-item ${s.enabled ? '' : 'disabled'}">
            <div class="skill-info">
                <div class="skill-name">${escapeHtml(s.name)}</div>
                <div class="skill-desc">${escapeHtml(s.description || '')}</div>
            </div>
            <label class="skill-toggle">
                <input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="toggleSkill('${escapeHtml(s.name)}', this.checked)">
                <span class="toggle-slider"></span>
            </label>
        </div>
    `).join('');
}

async function toggleSkill(name, enabled) {
    await apiCall(`/api/skills/${encodeURIComponent(name)}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
    });
    loadSkills();
}

async function loadTools() {
    const list = document.getElementById('toolsList');
    if (!list) return;

    const data = await apiCall('/api/tools');
    if (data.error || !data.tools || data.tools.length === 0) {
        list.innerHTML = '<div class="empty-text">无可用工具</div>';
        return;
    }

    list.innerHTML = data.tools.map(t => `
        <div class="tool-item ${t.enabled ? '' : 'disabled'}">
            <span class="tool-icon">⚙️</span>
            <span class="tool-name" title="${escapeHtml(t.description || '')}">${escapeHtml(t.name)}</span>
            <span class="tool-safety ${t.safety}">${t.safety}</span>
            <label class="skill-toggle">
                <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTool('${escapeHtml(t.name)}', this.checked)">
                <span class="toggle-slider"></span>
            </label>
        </div>
    `).join('');
}

async function toggleTool(name, enabled) {
    const endpoint = enabled ? `/api/tools/${encodeURIComponent(name)}/enable` : `/api/tools/${encodeURIComponent(name)}/disable`;
    await apiCall(endpoint, { method: 'POST' });
    loadTools();
}

window.toggleTool = toggleTool;

async function loadMemory() {
    const list = document.getElementById('memoryList');
    const data = await apiCall('/api/memory');

    if (data.error || !data.memories || data.memories.length === 0) {
        list.innerHTML = '<div class="empty-text">暂无记忆</div>';
        return;
    }

    list.innerHTML = data.memories.slice(-5).reverse().map(m => `
        <div class="memory-item" onclick="showMemoryDetail('${escapeHtml(m.summary || m.task || '')}')">
            <div class="memory-date">${m.date || ''}</div>
            <div class="memory-summary">${escapeHtml(m.summary || m.task || '')}</div>
        </div>
    `).join('');
}

async function loadAudit() {
    const list = document.getElementById('auditList');
    const data = await apiCall('/api/audit');

    if (data.error || !data.records || data.records.length === 0) {
        list.innerHTML = '<div class="empty-text">暂无审计记录</div>';
        return;
    }

    // Store records globally for detail lookup (avoid huge onclick attributes)
    window._auditRecords = data.records;

    list.innerHTML = data.records.slice(-10).reverse().map((r, i) => {
        const realIdx = data.records.indexOf(r);
        return `
        <div class="audit-item ${r.approved ? 'approved' : 'rejected'}"
             onclick="showAuditDetailByIdx(${realIdx})">
            <div class="audit-icon ${r.approved ? 'approved' : 'rejected'}">${r.approved ? '✓' : '✗'}</div>
            <div class="audit-body">
                <div class="audit-item-header">
                    <span class="audit-tool">${escapeHtml(r.tool)}</span>
                </div>
                <div class="audit-meta">
                    <span class="audit-time">${r.timestamp ? r.timestamp.substring(11, 19) : ''}</span>
                </div>
            </div>
        </div>
        `;
    }).join('');
}

function showAuditDetailByIdx(idx) {
    const r = window._auditRecords[idx];
    if (!r) return;
    showAuditDetail(r.session_id, r.step, r.tool, r.args, r.approved, r.operator || 'human', r.timestamp || '');
}

function showAuditDetail(sessionId, step, tool, args, approved, operator, timestamp) {
    currentAuditRecord = { sessionId, step, tool, args, approved, operator, timestamp };
    const detailBody = document.getElementById('detailBody');
    const argsStr = typeof args === 'string' ? args : JSON.stringify(args, null, 2);
    document.querySelector('.detail-export').style.display = '';
    document.getElementById('detailTitle').textContent = `审计详情 - ${tool}`;
    detailBody.innerHTML = `
        <div class="detail-row">
            <div class="detail-label">时间</div>
            <div class="detail-value">${timestamp ? timestamp.replace('T', ' ').substring(0, 19) : '-'}</div>
        </div>
        <div class="detail-row">
            <div class="detail-label">操作</div>
            <div class="detail-value"><span class="status-badge ${approved ? 'approved' : 'rejected'}">${approved ? '✅ 批准' : '❌ 拒绝'}</span></div>
        </div>
        <div class="detail-row">
            <div class="detail-label">工具</div>
            <div class="detail-value tool-name">${escapeHtml(tool)}</div>
        </div>
        <div class="detail-row">
            <div class="detail-label">Session</div>
            <div class="detail-value session-id">${escapeHtml(sessionId)}</div>
        </div>
        <div class="detail-row">
            <div class="detail-label">Step</div>
            <div class="detail-value">${step}</div>
        </div>
        <div class="detail-row">
            <div class="detail-label">操作人</div>
            <div class="detail-value">${operator}</div>
        </div>
        <div class="detail-row">
            <div class="detail-label">参数</div>
            <pre class="detail-args">${escapeHtml(argsStr)}</pre>
        </div>
    `;
    document.getElementById('detailOverlay').style.display = 'block';
    document.getElementById('detailPanel').style.display = 'flex';
}

function showMemoryDetail(summary) {
    currentAuditRecord = null;
    const detailBody = document.getElementById('detailBody');
    document.getElementById('detailTitle').textContent = '记忆详情';
    document.querySelector('.detail-export').style.display = 'none';
    detailBody.innerHTML = `
        <div class="detail-row">
            <div class="detail-label">摘要</div>
            <div class="detail-value">${escapeHtml(summary)}</div>
        </div>
    `;
    document.getElementById('detailOverlay').style.display = 'block';
    document.getElementById('detailPanel').style.display = 'flex';
}

function exportAuditDetail() {
    if (!currentAuditRecord) return;
    const { sessionId, step, tool, args, approved, operator, timestamp } = currentAuditRecord;
    const argsStr = typeof args === 'string' ? args : JSON.stringify(args, null, 2);
    const content = `NexusHarness 审计记录
====================
时间: ${timestamp ? timestamp.replace('T', ' ').substring(0, 19) : '-'}
操作: ${approved ? '批准' : '拒绝'}
工具: ${tool}
Session: ${sessionId}
Step: ${step}
操作人: ${operator}
参数:
${argsStr}
`;
    downloadFile(`${tool}_${timestamp ? timestamp.substring(0, 10) : Date.now()}.txt`, content);
}

function downloadFile(filename, content) {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

function closeDetail() {
    document.getElementById('detailOverlay').style.display = 'none';
    document.getElementById('detailPanel').style.display = 'none';
    currentAuditRecord = null;
}

async function exportAllAudit() {
    const data = await apiCall('/api/audit?limit=100');
    if (data.error || !data.records || data.records.length === 0) {
        alert('暂无审计记录可导出');
        return;
    }

    const lines = ['时间,操作,工具,Session,Step,操作人,参数'];
    for (const r of data.records) {
        const args = typeof r.args === 'string' ? r.args : JSON.stringify(r.args || {});
        const row = [
            r.timestamp || '',
            r.approved ? '批准' : '拒绝',
            r.tool || '',
            r.session_id || '',
            r.step || '',
            r.operator || '',
            `"${args.replace(/"/g, '""')}"`
        ];
        lines.push(row.join(','));
    }

    const content = '\uFEFF' + lines.join('\n');
    const date = new Date().toISOString().substring(0, 10);
    downloadFile(`audit_${date}.csv`, content);
}

// ──────────────────────────────────────────────────
// Task Execution
// ──────────────────────────────────────────────────

async function runTask() {
    if (isRunning) return;

    const taskInput = document.getElementById('taskInput');
    const task = taskInput.value.trim();
    if (!task) return;

    isRunning = true;
    currentStep = 0;
    updateStepText(0);

    // Create new session via API
    const sessionData = await apiCall('/api/sessions', {
        method: 'POST',
        body: JSON.stringify({ task }),
    });
    console.log('Session create result:', sessionData);

    if (sessionData.error || !sessionData.session_id) {
        appendError('创建会话失败: ' + (sessionData.error || JSON.stringify(sessionData)));
        isRunning = false;
        return;
    }

    currentSessionId = sessionData.session_id;
    currentMessages = [{type: 'human', content: task, timestamp: new Date().toISOString()}];
    saveConversationToLocal();

    // Clear welcome message
    const container = document.getElementById('messagesContainer');
    const welcome = container.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    // Add user message
    appendMessage('user', task);

    // Abort previous request if any
    if (currentEventSource) {
        currentEventSource.abort();
        currentEventSource = null;
    }

    // Use /api/run with session_id for SSE streaming
    const url = `/api/run?session_id=${currentSessionId}&task=${encodeURIComponent(task)}`;

    currentEventSource = new XMLHttpRequest();
    currentEventSource.open('GET', url, true);
    currentEventSource.setRequestHeader('Accept', 'text/event-stream');
    currentEventSource.setRequestHeader('Cache-Control', 'no-cache');

    // Track position in responseText to only process new lines
    let lastResponseLength = 0;

    currentEventSource.onprogress = () => {
        const fullText = currentEventSource.responseText;
        const newText = fullText.substring(lastResponseLength);
        lastResponseLength = fullText.length;

        const lines = newText.split('\n');
        let pendingEvent = null;

        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;

            if (trimmed.startsWith('event:')) {
                pendingEvent = trimmed.substring(6).trim();
            } else if (trimmed.startsWith('data:')) {
                const data = trimmed.substring(5).trim();
                if (data && pendingEvent) {
                    try {
                        const json = JSON.parse(data);
                        json._event = pendingEvent;
                        handleSSEEvent(json);
                    } catch (e) {}
                }
                pendingEvent = null;
            }
        }
    };

    currentEventSource.onload = () => {
        isRunning = false;
        currentEventSource = null;
    };

    currentEventSource.onerror = () => {
        isRunning = false;
        currentEventSource = null;
    };

    currentEventSource.send();
    taskInput.value = '';
}

function handleSSEEvent(data) {
    const eventType = data._event || data.event;
    switch (eventType) {
        case 'start':
            break;

        case 'step':
            currentStep = data.step;
            updateStepText(currentStep);
            appendStepIndicator(data.step);
            // Start new AI message for this step
            currentMessages.push({type: 'ai', content: '', timestamp: new Date().toISOString()});
            break;

        case 'token':
            appendStreamingToken(data.content);
            if (streamingMessageEl && currentMessages.length > 0) {
                currentMessages[currentMessages.length - 1].content += data.content;
                saveConversationToLocal();
            }
            break;

        case 'tool_call':
            appendToolCard(data.tool, data.args, data.step);
            break;

        case 'tool_result':
            updateToolResult(data.tool, data.result);
            break;

        case 'retry':
            showRetryEvent(data.tool, data.attempt, data.max_retries, data.delay, data.error);
            break;

        case 'tool_approved':
            updateToolStatus(data.tool, 'approved');
            break;

        case 'tool_rejected':
            updateToolStatus(data.tool, 'rejected');
            break;

        case 'approval_required':
            showApprovalModal(data.approval_id, data.tool, data.args);
            break;

        case 'complete':
            isRunning = false;
            renderMarkdown();
            loadAudit();
            loadMemory();
            loadSystemStatus();
            saveConversationToServer();
            loadConversations().then(renderConversationList);
            break;

        case 'token_stats':
            loadSystemStatus();
            break;

        case 'memory_saved':
            loadMemory();
            break;

        case 'error':
            appendError(data.message);
            isRunning = false;
            break;

        case 'interrupted':
            isRunning = false;
            appendSystemMessage('⏸ 会话已中断，可从当前状态恢复');
            loadSessions();
            break;

        case 'start':
            if (data.resuming) {
                appendSystemMessage('▶ 继续之前的会话');
            }
            break;
    }
}

function updateStepText(step) {
    document.getElementById('stepText').textContent = `Step ${step}/${maxSteps}`;
}

// ──────────────────────────────────────────────────
// Message & UI Helpers
// ──────────────────────────────────────────────────

function appendMessage(type, content) {
    const container = document.getElementById('messagesContainer');
    const div = document.createElement('div');
    div.className = `message ${type}`;

    // Tool call messages get the collapsible card treatment
    if (type === 'tool_call') {
        const toolName = extractToolName(content);
        const toolArgs = extractToolArgs(content);
        const toolResult = extractToolResult(content);
        div.innerHTML = buildToolCollapsedHtml(toolName, toolArgs, toolResult);
    } else {
        const avatar = type === 'user' ? '👤' : '🤖';
        // Parse thinking tags and step labels from content
        const parsed = parseMessageContent(content);
        div.innerHTML = `
            <div class="message-avatar">${avatar}</div>
            <div class="message-content">${parsed}</div>
        `;
    }
    container.appendChild(div);
    scrollToBottom();
    return div;
}

// Decode HTML entities in stored content. Also handles raw <tag> by first escaping
// them to &lt; &gt; so the browser doesn't parse them as HTML elements.
function decodeHtmlEntities(str) {
    if (!str) return '';
    // Use a div with textContent to safely encode raw < > to &lt; &gt;
    const tmp = document.createElement('div');
    tmp.textContent = str; // browser auto-escapes < > to &lt; &gt;
    const entityEncoded = tmp.innerHTML; // get &lt;thinking&gt;
    // Now decode all HTML entities back to characters
    const txt = document.createElement('textarea');
    txt.innerHTML = entityEncoded;
    return txt.value;
}

// Parse message content for thinking tags, step labels, tool call blocks
function parseMessageContent(content) {
    if (!content) return '';
    // Decode HTML entities (stored as &lt; &gt; etc.)
    let result = decodeHtmlEntities(content);

    // Parse <thinking>...</thinking> into styled blocks
    result = result.replace(/<thinking>([\s\S]*?)<\/thinking>/gi, '<div class="thinking-text">$1</div>');

    // Parse Step X/10 labels
    result = result.replace(/(Step \d+\/\d+[^<]*)/g, '<div class="step-label">$1</div>');

    // Parse inline tool call blocks like "🔧 weather\n参数: {...}\n执行结果: ..."
    result = result.replace(/(🔧 \w+[\s\S]*?(?:参数:|执行结果:)[\s\S]*?(?=\n\n|$))/g, function(match) {
        const toolName = match.match(/🔧 (\w+)/) ? match.match(/🔧 (\w+)/)[1] : 'tool';
        const argsMatch = match.match(/参数[:：]\s*([\s\S]*?)(?=执行结果[:：]|$)/);
        const resultMatch = match.match(/执行结果[:：]\s*([\s\S]*?)$/);
        const argsStr = argsMatch ? argsMatch[1].trim() : '{}';
        const resultText = resultMatch ? resultMatch[1].trim() : '(无输出)';

        const escapedName = escapeHtml(toolName);
        const escapedArgs = escapeHtml(argsStr);
        const escapedResult = escapeHtml(resultText);

        return `
<div class="tool-call-render">
    <div class="tool-collapsed" onclick="this.style.display='none';this.nextElementSibling.style.display='block';">
        <span class="tool-collapsed-icon">🔧</span>
        <span class="tool-collapsed-name">${escapedName}</span>
        <span class="tool-collapsed-status approved">✅</span>
    </div>
    <div class="tool-card" style="display:none;" onclick="this.style.display='none';this.previousElementSibling.style.display='inline-flex';">
        <div class="tool-card-header">
            <span class="tool-card-icon">🔧</span>
            <span class="tool-card-name">${escapedName}</span>
            <span class="tool-call-status approved">✅</span>
            <span class="tool-card-toggle">▾</span>
        </div>
        <div class="tool-card-body">
            <div class="tool-card-params">${escapedArgs}</div>
            <div class="tool-card-result">${escapedResult}</div>
        </div>
    </div>
</div>`;
    });

    // Convert line breaks to <br>
    result = result.replace(/\n/g, '<br>');

    return result;
}

// Extract tool name from plain text tool call content
function extractToolName(content) {
    if (!content) return 'tool';
    const m = content.match(/🔧\s*(\w+)/);
    return m ? m[1] : 'tool';
}

// Extract tool args from plain text
function extractToolArgs(content) {
    if (!content) return '{}';
    const m = content.match(/参数[:：]\s*(\{[\s\S]*?\}|\"[\s\S]*?\")/);
    return m ? m[1] : '{}';
}

// Extract tool result from plain text
function extractToolResult(content) {
    if (!content) return '(无输出)';
    const m = content.match(/执行结果[:：]\s*([\s\S]*)$/);
    return m ? m[1].trim() : '(无输出)';
}

// Build the collapsed + expanded tool card HTML
function buildToolCollapsedHtml(toolName, toolArgs, toolResult, status) {
    status = status || 'pending';
    toolArgs = toolArgs || '{}';
    toolResult = toolResult || '(无输出)';

    const statusClass = status === 'approved' ? 'approved' : status === 'rejected' ? 'rejected' : 'pending';
    const statusText = status === 'approved' ? '✅ 批准' : status === 'rejected' ? '❌ 拒绝' : '执行中';

    return `
        <div class="tool-collapsed" onclick="this.style.display='none';this.nextElementSibling.style.display='block';">
            <span class="tool-collapsed-icon">🔧</span>
            <span class="tool-collapsed-name">${escapeHtml(toolName)}</span>
            <span class="tool-collapsed-status ${statusClass}">${statusText}</span>
        </div>
        <div class="tool-card" style="display:none;" onclick="this.style.display='none';this.previousElementSibling.style.display='inline-flex';">
            <div class="tool-card-header">
                <span class="tool-card-icon">🔧</span>
                <span class="tool-card-name">${escapeHtml(toolName)}</span>
                <span class="tool-call-status ${statusClass}">${statusText}</span>
                <span class="tool-card-toggle">▾</span>
            </div>
            <div class="tool-card-body">
                <div class="tool-card-params">${escapeHtml(toolArgs)}</div>
                <div class="tool-card-result">${escapeHtml(toolResult)}</div>
            </div>
        </div>
    `;
}

function renderMarkdown() {
    if (streamingMessageEl) {
        const contentEl = streamingMessageEl.querySelector('.message-content');
        const stepEl = streamingMessageEl.querySelector('.step-label');
        const stepHtml = stepEl ? stepEl.outerHTML + '<span class="streaming-text"></span>' : '<span class="streaming-text"></span>';
        const raw = streamingContent;

        // Parse markdown but keep step label intact
        const parsed = marked.parse(raw);
        contentEl.innerHTML = stepHtml;
        const textEl = contentEl.querySelector('.streaming-text');
        textEl.innerHTML = parsed;
        streamingContent = '';
    }
    // Also render any non-streaming agent messages that might have markdown
    document.querySelectorAll('.message.agent .message-content').forEach(el => {
        const raw = el.textContent;
        if (raw && raw.includes('```')) {
            el.innerHTML = marked.parse(raw);
        }
    });
}

let streamingMessageEl = null;
let streamingContent = '';

function appendStreamingToken(content) {
    const container = document.getElementById('messagesContainer');

    if (!streamingMessageEl) {
        streamingMessageEl = document.createElement('div');
        streamingMessageEl.className = 'message agent';

        const stepHtml = pendingStepLabel ? `<div class="step-label">Step ${pendingStepLabel}/${maxSteps}</div>` : '';
        streamingMessageEl.innerHTML = `<div class="message-avatar">🤖</div><div class="message-content">${stepHtml}<span class="streaming-text"></span></div>`;
        pendingStepLabel = null;
        container.appendChild(streamingMessageEl);
    }

    const textEl = streamingMessageEl.querySelector('.streaming-text');
    streamingContent += content;
    textEl.textContent = streamingContent;
    scrollToBottom();
}

let pendingStepLabel = null;

function appendStepIndicator(step) {
    pendingStepLabel = step;
}

function appendToolCard(toolName, toolArgs, step) {
    streamingMessageEl = null;
    streamingContent = '';
    const container = document.getElementById('messagesContainer');
    const div = document.createElement('div');
    div.className = 'message tool-call';
    div.id = `tool-${step}`;

    const argsStr = typeof toolArgs === 'string' ? toolArgs : JSON.stringify(toolArgs, null, 2);

    div.innerHTML = `
        <div class="tool-collapsed" onclick="this.style.display='none';this.nextElementSibling.style.display='block';">
            <span class="tool-collapsed-icon">🔧</span>
            <span class="tool-collapsed-name">${escapeHtml(toolName)}</span>
            <span class="tool-collapsed-status pending" id="tool-collapsed-status-${step}">执行中</span>
        </div>
        <div class="tool-card" style="display:none;" onclick="this.style.display='none';this.previousElementSibling.style.display='inline-flex';">
            <div class="tool-card-header">
                <span class="tool-card-icon">🔧</span>
                <span class="tool-card-name">${escapeHtml(toolName)}</span>
                <span class="tool-call-status pending" id="tool-card-status-${step}">执行中</span>
                <span class="tool-card-toggle">▾</span>
            </div>
            <div class="tool-card-body">
                <div class="tool-card-params">${escapeHtml(argsStr)}</div>
                <div class="tool-card-result" id="tool-result-${step}">等待结果...</div>
            </div>
        </div>
    `;
    container.appendChild(div);
    scrollToBottom();
}

function updateToolResult(toolName, result) {
    const container = document.getElementById('messagesContainer');
    const toolCalls = container.querySelectorAll('.tool-call');
    const lastCall = toolCalls[toolCalls.length - 1];
    if (lastCall) {
        const resultEl = lastCall.querySelector('.tool-card-result');
        if (resultEl) resultEl.textContent = result || '(无输出)';
    }
}

function showRetryEvent(tool, attempt, max_retries, delay, error) {
    const container = document.getElementById('messagesContainer');
    const toolCalls = container.querySelectorAll('.tool-call');
    const lastCall = toolCalls[toolCalls.length - 1];
    if (!lastCall) return;

    // Update status to show retry
    const statusEl = lastCall.querySelector('.tool-call-status');
    if (statusEl) {
        statusEl.className = 'tool-call-status retry';
        statusEl.textContent = `重试 ${attempt}/${max_retries}`;
    }
    const collapsedStatusEl = lastCall.querySelector('.tool-collapsed-status');
    if (collapsedStatusEl) {
        collapsedStatusEl.className = 'tool-collapsed-status retry';
        collapsedStatusEl.textContent = `重试 ${attempt}/${max_retries}`;
    }

    // Show retry info in result area
    const resultEl = lastCall.querySelector('.tool-card-result');
    if (resultEl) {
        resultEl.innerHTML = `<span class="retry-info">🔄 重试 ${attempt}/${max_retries}，${delay.toFixed(1)}s 后重试...</span>
            <div class="retry-error">错误: ${escapeHtml(error || '未知错误')}</div>`;
    }
}

function updateToolStatus(toolName, status) {
    const container = document.getElementById('messagesContainer');
    const toolCalls = container.querySelectorAll('.tool-call');
    const lastCall = toolCalls[toolCalls.length - 1];
    if (!lastCall) return;

    const statusText = status === 'approved' ? '✅ 批准' : '❌ 拒绝';
    const statusClass = status === 'approved' ? 'approved' : 'rejected';

    // Update collapsed tag status
    const collapsedStatus = lastCall.querySelector('.tool-collapsed-status');
    if (collapsedStatus) {
        collapsedStatus.textContent = statusText;
        collapsedStatus.className = `tool-collapsed-status ${statusClass}`;
    }

    // Update card status
    const cardStatus = lastCall.querySelector('.tool-call-status');
    if (cardStatus) {
        cardStatus.textContent = statusText;
        cardStatus.className = `tool-call-status ${statusClass}`;
    }
}

function appendError(message) {
    const container = document.getElementById('messagesContainer');
    const div = document.createElement('div');
    div.className = 'error-message';
    div.textContent = `错误: ${message}`;
    container.appendChild(div);
    scrollToBottom();
}

function appendSystemMessage(message) {
    const container = document.getElementById('messagesContainer');
    const div = document.createElement('div');
    div.className = 'system-message';
    div.textContent = message;
    container.appendChild(div);
    scrollToBottom();
}

function scrollToBottom() {
    const container = document.getElementById('messagesContainer');
    container.scrollTop = container.scrollHeight;
}

// ──────────────────────────────────────────────────
// Approval Modal
// ──────────────────────────────────────────────────

function showApprovalModal(approvalId, toolName, toolArgs) {
    currentApprovalId = approvalId;
    document.getElementById('modalToolName').textContent = toolName;
    document.getElementById('modalToolArgs').textContent = JSON.stringify(toolArgs, null, 2);
    document.getElementById('approvalModal').style.display = 'flex';
}

async function approveOperation() {
    if (!currentApprovalId) return;

    await apiCall('/api/approve', {
        method: 'POST',
        body: JSON.stringify({ approval_id: currentApprovalId }),
    });

    document.getElementById('approvalModal').style.display = 'none';
    currentApprovalId = null;
    loadAudit();
}

async function rejectOperation() {
    if (!currentApprovalId) return;

    await apiCall('/api/reject', {
        method: 'POST',
        body: JSON.stringify({ approval_id: currentApprovalId }),
    });

    document.getElementById('approvalModal').style.display = 'none';
    currentApprovalId = null;
    loadAudit();
}

// ──────────────────────────────────────────────────
// Detail Panel
// ──────────────────────────────────────────────────

// ──────────────────────────────────────────────────
// Settings Panel
// ──────────────────────────────────────────────────

async function showSettings() {
    const data = await apiCall('/api/config');
    if (data.error) return;

    document.getElementById('settingProvider').value = data.provider || 'minimax';
    document.getElementById('settingMainModel').value = data.main_model || '';
    document.getElementById('settingMemoryModel').value = data.memory_model || '';
    document.getElementById('settingMaxSteps').value = data.max_steps || 10;
    document.getElementById('settingsModal').style.display = 'flex';
}

async function saveSettings() {
    const settings = {
        provider: document.getElementById('settingProvider').value,
        main_model: document.getElementById('settingMainModel').value.trim(),
        memory_model: document.getElementById('settingMemoryModel').value.trim(),
        max_steps: parseInt(document.getElementById('settingMaxSteps').value, 10),
    };

    const result = await apiCall('/api/config', {
        method: 'POST',
        body: JSON.stringify(settings),
    });

    if (result.error) {
        appendError('保存失败: ' + result.error);
        return;
    }

    document.getElementById('settingsModal').style.display = 'none';
    maxSteps = result.config.max_steps || 10;
    updateStepText(0);
    loadSystemStatus();
    document.getElementById('modelBadge').textContent = result.config.main_model || 'Unknown';
}

function closeSettings() {
    document.getElementById('settingsModal').style.display = 'none';
}

// ──────────────────────────────────────────────────
// Utility
// ──────────────────────────────────────────────────

function escapeHtml(text) {
    if (typeof text !== 'string') return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ──────────────────────────────────────────────────
// Input Handling
// ──────────────────────────────────────────────────

function handleKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        runTask();
    }
}

function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function fillTask(task) {
    document.getElementById('taskInput').value = task;
    document.getElementById('taskInput').focus();
}

function startNewChat() {
    // Save current conversation before starting new
    if (currentSessionId && currentMessages.length > 0) {
        saveConversationToServer();
    }

    document.getElementById('messagesContainer').innerHTML = `
        <div class="welcome-message">
            <div class="welcome-icon">🤖</div>
            <h3>你好，我是 NexusHarness</h3>
            <p>我可以帮你完成各种任务，比如查询天气、写代码、分析数据等。</p>
            <div class="welcome-suggestions">
                <button class="suggestion-btn" onclick="fillTask('今天北京天气怎么样？')">🌤️ 查询天气</button>
                <button class="suggestion-btn" onclick="fillTask('帮我写一个斐波那契数列的Python脚本')">🐍 写代码</button>
                <button class="suggestion-btn" onclick="fillTask('分析一下当前目录下有什么文件')">📁 查看文件</button>
            </div>
        </div>
    `;
    currentStep = 0;
    currentSessionId = null;
    currentMessages = [];
    clearLocalSession();
    updateStepText(0);
    loadConversations().then(renderConversationList);
}

// ──────────────────────────────────────────────────
// Session Management
// ──────────────────────────────────────────────────

async function loadSessions() {
    const data = await apiCall('/api/sessions');
    if (data.error) return;
    renderSessionsList(data.sessions || []);
}

function renderSessionsList(sessions) {
    const list = document.getElementById('sessionsList');
    if (!list) return;

    if (sessions.length === 0) {
        list.innerHTML = '<div class="empty-text">暂无会话</div>';
        return;
    }

    list.innerHTML = sessions.map(s => {
        const statusClass = s.status === 'active' ? 'active' : s.status === 'interrupted' ? 'interrupted' : 'completed';
        const statusText = s.status === 'active' ? '进行中' : s.status === 'interrupted' ? '已中断' : '已完成';
        const canResume = s.status === 'interrupted' || s.status === 'active';
        return `
        <div class="session-item ${s.session_id === currentSessionId ? 'active' : ''}">
            <div class="session-info" onclick="resumeSession('${s.session_id}')">
                <div class="session-task">${escapeHtml(s.task || '新会话')}</div>
                <div class="session-meta">
                    <span class="session-status ${statusClass}">${statusText}</span>
                    <span class="session-steps">Step ${s.step_count || 0}</span>
                </div>
            </div>
            <div class="session-actions">
                ${canResume ? `<button class="session-btn resume" onclick="resumeSession('${s.session_id}')" title="继续">▶</button>` : ''}
                ${s.status === 'active' ? `<button class="session-btn interrupt" onclick="interruptSession('${s.session_id}')" title="中断">⏸</button>` : ''}
                <button class="session-btn replay" onclick="openReplay('${s.session_id}')" title="回放">🔍</button>
                <button class="session-btn delete" onclick="deleteSession('${s.session_id}')" title="删除">🗑</button>
            </div>
        </div>
        `;
    }).join('');
}

async function createNewSession() {
    const taskInput = document.getElementById('taskInput');
    const task = taskInput ? taskInput.value.trim() : '';

    if (!task) {
        // Prompt user to enter task in the input
        taskInput.focus();
        return;
    }

    const data = await apiCall('/api/sessions', {
        method: 'POST',
        body: JSON.stringify({ task }),
    });

    if (data.error) {
        appendError('创建会话失败: ' + data.error);
        return;
    }

    currentSessionId = data.session_id;
    runTask();
}

async function resumeSession(sessionId) {
    const data = await apiCall(`/api/sessions/${sessionId}`);
    if (data.error || !data.session) {
        appendError('无法加载会话');
        return;
    }

    const session = data.session;
    currentSessionId = sessionId;

    // Clear current view
    const container = document.getElementById('messagesContainer');
    const welcome = container.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    // Render existing messages from harness_state
    const harnessState = session.harness_state || {};
    const messages = harnessState.messages || [];
    currentMessages = [];

    container.innerHTML = '';
    for (const msg of messages) {
        const type = msg.type || (msg.role === 'human' ? 'user' : 'ai');
        const content = typeof msg.content === 'string' ? msg.content : '';
        if (content) {
            appendMessage(type, content);
            currentMessages.push({ type, content, timestamp: new Date().toISOString() });
        }
    }

    // Update step counter
    currentStep = harnessState.step_count || 0;
    updateStepText(currentStep);

    // Run the session
    runSession(sessionId, session.task || '继续任务');
}

async function runSession(sessionId, task) {
    if (isRunning) return;

    isRunning = true;
    currentStep = 0;
    updateStepText(0);

    // Clear welcome message
    const container = document.getElementById('messagesContainer');
    const welcome = container.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    if (!currentMessages.some(m => m.type === 'human' && m.content === task)) {
        appendMessage('human', task);
        currentMessages.push({ type: 'human', content: task, timestamp: new Date().toISOString() });
    }
    saveConversationToLocal();

    // Abort previous request if any
    if (currentEventSource) {
        currentEventSource.abort();
        currentEventSource = null;
    }

    const url = `/api/run?session_id=${sessionId}&task=${encodeURIComponent(task)}`;

    currentEventSource = new XMLHttpRequest();
    currentEventSource.open('GET', url, true);
    currentEventSource.setRequestHeader('Accept', 'text/event-stream');
    currentEventSource.setRequestHeader('Cache-Control', 'no-cache');

    let lastResponseLength = 0;

    currentEventSource.onprogress = () => {
        const fullText = currentEventSource.responseText;
        const newText = fullText.substring(lastResponseLength);
        lastResponseLength = fullText.length;

        const lines = newText.split('\n');
        let pendingEvent = null;

        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;

            if (trimmed.startsWith('event:')) {
                pendingEvent = trimmed.substring(6).trim();
            } else if (trimmed.startsWith('data:')) {
                const data = trimmed.substring(5).trim();
                if (data && pendingEvent) {
                    try {
                        const json = JSON.parse(data);
                        json._event = pendingEvent;
                        handleSSEEvent(json);
                    } catch (e) {}
                }
                pendingEvent = null;
            }
        }
    };

    currentEventSource.onload = () => {
        isRunning = false;
        currentEventSource = null;
    };

    currentEventSource.onerror = () => {
        isRunning = false;
        currentEventSource = null;
    };

    currentEventSource.send();
}

async function interruptSession(sessionId) {
    const data = await apiCall(`/api/sessions/${sessionId}/interrupt`, { method: 'POST' });
    if (data.error) {
        appendError('中断失败: ' + data.error);
        return;
    }
    isRunning = false;
    if (currentEventSource) {
        currentEventSource.abort();
        currentEventSource = null;
    }
    loadSessions();
}

async function deleteSession(sessionId) {
    if (!confirm('确定删除这个会话？')) return;

    const data = await apiCall(`/api/sessions/${sessionId}`, { method: 'DELETE' });
    if (data.error) {
        appendError('删除失败: ' + data.error);
        return;
    }

    if (currentSessionId === sessionId) {
        startNewChat();
    }
    loadSessions();
    loadConversations().then(renderConversationList);
}

function openReplay(sessionId) {
    window.open(`/templates/replay.html?session_id=${sessionId}`, '_blank', 'width=1200,height=800');
}

window.resumeSession = resumeSession;
window.interruptSession = interruptSession;
window.deleteSession = deleteSession;
window.openReplay = openReplay;

// ──────────────────────────────────────────────────
// Clear Data Functions
// ──────────────────────────────────────────────────

async function clearMemory() {
    if (!confirm('确定清空所有记忆？此操作不可撤销。')) return;
    const data = await apiCall('/api/memory', { method: 'DELETE' });
    if (data.error) {
        appendError('清空记忆失败: ' + data.error);
        return;
    }
    loadMemory();
    showToast('记忆已清空');
}

async function clearAudit() {
    if (!confirm('确定清空所有审计日志？此操作不可撤销。')) return;
    const data = await apiCall('/api/audit', { method: 'DELETE' });
    if (data.error) {
        appendError('清空审计失败: ' + data.error);
        return;
    }
    loadAudit();
    showToast('审计日志已清空');
}

async function clearAllSessions() {
    if (!confirm('确定删除所有会话？此操作不可撤销。')) return;
    const data = await apiCall('/api/conversations', { method: 'DELETE' });
    if (data.error) {
        appendError('清空会话失败: ' + data.error);
        return;
    }
    loadSessions();
    loadConversations().then(renderConversationList);
    showToast('所有会话已删除');
}

function showToast(message) {
    const toast = document.createElement('div');
    toast.className = 'toast-message';
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

window.clearMemory = clearMemory;
window.clearAudit = clearAudit;
window.clearAllSessions = clearAllSessions;