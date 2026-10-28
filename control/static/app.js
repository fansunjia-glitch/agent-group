(() => {
  'use strict';

  const icons = {
    network: '<circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="m6.5 8 4 7.5m7-7.5-4 7.5M7.5 6h9"/>',
    agents: '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M8 12v2m8-2v2M9 17h6M1 12h3m16 0h3"/>',
    hash: '<path d="m10 3-4 18M18 3l-4 18M3 9h18M2 15h18"/>',
    trace: '<circle cx="5" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M5 7v9a3 3 0 0 0 3 3h9M9 5h7a3 3 0 0 1 0 6H9"/>',
    radio: '<circle cx="12" cy="12" r="2"/><path d="M5.6 5.6a9 9 0 0 0 0 12.8m12.8-12.8a9 9 0 0 1 0 12.8M8.4 8.4a5 5 0 0 0 0 7.2m7.2-7.2a5 5 0 0 1 0 7.2"/>',
    server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6m-6 11h6"/>',
    key: '<circle cx="8" cy="9" r="4"/><path d="m11 12 8 8m-3-3 3-3m-6 0 3-3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    back: '<path d="M19 12H5m5-5-5 5 5 5"/>',
    play: '<path d="m8 5 11 7-11 7Z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    edit: '<path d="m15 4 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14Z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5M6.5 6.5a7.8 7.8 0 0 1 13.3 5M4.2 12.5a7.8 7.8 0 0 0 13.3 5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
    send: '<path d="m21 3-7 18-4-8-8-4 19-6Zm-11 10L21 3"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 1h4"/>',
  };
  const $ = selector => document.querySelector(selector);
  const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
  const attr = escapeHTML;
  const cell = (label, content, className = '') => `<td data-label="${attr(label)}"${className ? ` class="${className}"` : ''}>${content}</td>`;
  const button = (label, action, id = '', kind = 'secondary', disabled = false) => `<button type="button" class="button ${kind}" data-action="${attr(action)}"${id ? ` data-id="${attr(id)}"` : ''}${disabled ? ' disabled' : ''}>${label}</button>`;
  const link = (label, page, id = '', className = 'text-link') => `<a class="${className}" href="#${page}${id ? `/${encodeURIComponent(id)}` : ''}">${escapeHTML(label)}</a>`;
  const badge = (text, color = '') => `<span class="badge ${color}">${escapeHTML(text)}</span>`;
  const runningStates = new Set(['starting', 'idle', 'working', 'stopping']);
  const activeStates = new Set(['idle', 'working']);
  const stateLabels = { stopped: '已停止', starting: '启动中', idle: '空闲', working: '执行中', stopping: '停止中', failed: '异常' };
  const pageNames = { overview: '协作概览', agents: 'Agents', topics: 'Topics', trace: '执行事件', notices: '通知中心', hosts: '运行主机', models: '模型配置' };
  const descriptions = { models: '统一管理 OpenAI 兼容接口，让不同 Agent 使用适合的模型。', overview: '查看各个 Agent 的当前状态与 Topic 协作关系。', agents: '管理 Agent 人设、启动任务，以及本地和远程运行状态。', topics: '查看协作 Topic、成员和相关事件。', trace: '按 Agent 查看实际产生的执行与通信事件。', notices: '通过管理员 Topic 同步意图，并跟踪每个 Agent 的回执。', hosts: '配置本地与同一局域网内的 SSH 主机，为 Agent 指定运行位置。' };
  let data = null;
  let connected = false;
  let lastSuccess = 0;
  let pollInFlight = null;
  let requestSequence = 0;
  let appliedSequence = 0;
  let toastTimer;
  const busy = new Set();
  const observedTopics = new Map();
  const chatHistories = new Map();
  let chatSearchTimer;
  const ui = { page: 'overview', id: null, traceProfile: '', expandedEvents: new Set(), formKind: null, editingId: null, chatTab: 'messages', chatQuery: '', chatMembers: false, chatScroll: new Map(), selectedAgents: new Set(), batch: null };
  const dialog = $('#editor-dialog');

  function paintIcons(scope = document) {
    scope.querySelectorAll('[data-icon]').forEach(element => { element.innerHTML = icon(element.dataset.icon); });
  }
  function timestamp(value, withDate = false) {
    if (value === null || value === undefined || value === '') return '—';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('zh-CN', { ...(withDate ? { month: '2-digit', day: '2-digit' } : {}), hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date);
  }
  function dateNumber(value) {
    return typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : new Date(value || 0).getTime();
  }
  function stateBadge(profile) {
    const state = profile.runtime?.state || 'stopped';
    return badge(stateLabels[state] || state, state === 'failed' ? 'red' : activeStates.has(state) ? 'green' : ['starting', 'stopping'].includes(state) ? 'amber' : '');
  }
  function hostName(id) { return data?.hosts.find(host => host.id === id)?.name || id || '未指定'; }
  function profileName(id) { return data?.profiles.find(profile => profile.id === id)?.name || id || '控制服务'; }
  function providerFor(profile) { return (data?.model_providers || []).find(provider => provider.id === profile?.provider_id); }
  function modelReady(profile) { return data?.model_providers ? !!providerFor(profile)?.credentials_configured : !!data?.credentials_configured; }
  function isBusy(key) { return busy.has(key) || !connected; }
  function canStartProfile(profile) {
    return !!profile && !profile.runtime?.cleanup_pending && !runningStates.has(profile.runtime?.state) && !profile.runtime?.pid && !busy.has(`profile:${profile.id}`);
  }
  function reconcileAgentSelection() {
    for (const id of ui.selectedAgents) {
      const profile = data.profiles.find(p => p.id === id);
      if (!profile || (!ui.batch?.running && !canStartProfile(profile))) ui.selectedAgents.delete(id);
    }
  }
  function renderBatchStart() {
    const eligible = data.profiles.filter(canStartProfile), count = ui.selectedAgents.size;
    const pending = !!ui.batch?.running;
    const results = ui.batch?.items || [];
    const completed = results.filter(item => !['queued', 'submitting'].includes(item.status)).length;
    const errors = results.filter(item => item.status === 'error');
    const labels = { queued: '排队中', submitting: '正在提交', submitted: '启动已提交', error: '提交失败', skipped: '已跳过' };
    return `<div class="agent-selection-bar"><label class="agent-select-all"><input id="select-all-agents" type="checkbox"${count && count === eligible.length ? ' checked' : ''}${pending || !connected || !eligible.length ? ' disabled' : ''}><span>全选可启动</span></label><span class="agent-selection-count">${pending ? `本次启动 <strong>${results.length}</strong> 个` : `已选 <strong>${count}</strong> / ${eligible.length} 个`}</span><div class="agent-selection-actions">${button('取消选择', 'clear-agent-selection', '', 'ghost', !count || pending)}${button(icon('play') + (pending ? `正在提交 ${completed} / ${results.length}` : `启动所选${count ? ` (${count})` : ''}`), 'start-selected-agents', '', 'primary', !count || pending || !connected)}</div></div>${results.length ? `<section class="panel agent-batch-results" aria-label="批量启动结果"><div class="panel-header"><div><h2>${pending ? '正在批量启动' : '本次启动结果'}</h2><small>${completed} / ${results.length} 已处理 · 具体运行状态见下方卡片</small></div>${pending ? badge('进行中', 'blue') : button('收起', 'dismiss-batch', '', 'ghost')}</div><div class="agent-batch-items">${results.map(item => {
      const profile = data.profiles.find(p => p.id === item.id);
      const failed = item.status === 'submitted' && profile?.runtime?.state === 'failed';
      return `<div class="agent-batch-item">${agentAvatar(item.name, 'xs')}<strong>${escapeHTML(item.name)}</strong>${badge(failed ? '运行异常' : labels[item.status], failed || item.status === 'error' ? 'red' : item.status === 'submitted' ? 'green' : '')}<span>${escapeHTML(failed ? profile.runtime.error || '查看卡片了解运行状态' : item.error || '')}</span></div>`;
    }).join('')}</div>${errors.length && !pending ? `<div class="agent-batch-retry">${button('选择提交失败项', 'select-failed-agents', '', 'secondary')}<span>修正原因后可再次启动，不会重复启动成功项。</span></div>` : ''}</section>` : ''}`;
  }
  async function startSelectedAgents() {
    if (ui.batch?.running) return;
    if (!connected) return toast('请先连接控制服务。', true);
    reconcileAgentSelection();
    const profiles = data.profiles.filter(p => ui.selectedAgents.has(p.id) && canStartProfile(p));
    if (!profiles.length) return toast('请选择至少一个可启动的 Agent。', true);
    if (!profiles.some(modelReady)) { toast('所选 Agent 的模型配置尚未设置 API Key，选择已保留。'); return openCredentials(); }
    const batch = { running: true, items: profiles.map(p => ({ id: p.id, name: p.name, status: 'queued', error: '' })) };
    ui.batch = batch;
    // Reserve every selected profile before the first await, including queued
    // items, so single-agent controls cannot launch/edit them during this batch.
    for (const item of batch.items) busy.add(`profile:${item.id}`);
    let next = 0;
    const worker = async () => {
      while (next < batch.items.length) {
        const item = batch.items[next++];
        try {
          const profile = data.profiles.find(p => p.id === item.id);
          if (!profile || runningStates.has(profile.runtime?.state) || profile.runtime?.pid) {
            item.status = 'skipped'; item.error = profile ? '状态已变化，无需重复启动。' : '配置已被移除。';
            ui.selectedAgents.delete(item.id);
            continue;
          }
          if (!modelReady(profile)) { item.status = 'error'; item.error = '该 Agent 的模型配置尚未设置 API Key。'; continue; }
          item.status = 'submitting'; renderPage();
          const runtime = await request(`/api/profiles/${encodeURIComponent(item.id)}/start`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Control-Token': data.csrf_token }, body: '{}' });
          const current = data.profiles.find(p => p.id === item.id);
          if (current) current.runtime = runtime;
          item.status = 'submitted';
          ui.selectedAgents.delete(item.id);
        } catch (error) { item.status = 'error'; item.error = error.message; }
        finally { renderPage(); }
      }
    };
    try {
      await Promise.all(Array.from({ length: Math.min(3, batch.items.length) }, worker));
      await loadState(true);
    } finally {
      batch.running = false;
      for (const item of batch.items) busy.delete(`profile:${item.id}`);
      reconcileAgentSelection(); renderPage();
    }
    const submitted = batch.items.filter(item => item.status === 'submitted').length;
    const errors = batch.items.filter(item => item.status === 'error').length;
    toast(`已提交 ${submitted} 个启动请求${errors ? `，${errors} 个提交失败，请查看结果。` : '，运行状态会自动更新。'}`, !!errors);
  }
  function empty(title, description, action = '', compact = false) {
    return `<div class="empty-state${compact ? ' compact' : ''}">${icon('network')}<h2>${escapeHTML(title)}</h2><p>${escapeHTML(description)}</p>${action}</div>`;
  }
  function errorNote(error) { return error ? `<div class="inline-note error">${icon('info')}<span>${escapeHTML(error)}</span></div>` : ''; }
  function infoNote(content, warning = false) { return `<div class="inline-note${warning ? ' warning' : ''}">${icon('info')}<span>${content}</span></div>`; }
  function table(headers, rows) { return `<div class="panel table-wrap"><table class="data-table"><thead><tr>${headers.map(header => `<th>${header}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`; }
  function back(page, label) { return `<div class="section-toolbar">${link(`← ${label}`, page, '', 'button ghost')}</div>`; }
  function sortedEvents(filter) { return (data?.events || []).filter(filter || (() => true)).slice().sort((a, b) => dateNumber(b.ts) - dateNumber(a.ts)).slice(0, 200); }
  function safeDetails(value) {
    if (!value || typeof value !== 'object') return value;
    if (Array.isArray(value)) return value.map(safeDetails);
    return Object.fromEntries(Object.entries(value).filter(([key]) => !/api.?key|authorization|password|secret|csrf|credential/i.test(key)).map(([key, item]) => [key, safeDetails(item)]));
  }
  function eventList(events, emptyMessage = '运行 Agent 后，这里会显示执行与通信事件。') {
    if (!events.length) return empty('还没有事件', emptyMessage, '', true);
    return `<div class="panel event-list">${events.map(event => {
      const id = String(event.id);
      const labels = { message_sent: '发送了消息', message_received: '接收了消息', topic_joined: '加入话题', topic_left: '离开话题', model_started: '开始思考', model_finished: '结束思考', tool_started: '调用工具', tool_finished: '工具返回', state: stateLabels[event.state] || '状态变化', ready: '启动就绪', worker_exit: '进程退出', admin_joined: '加入公告频道', notice_received: '收到管理员通知', notice_applied: '通知已写入上下文', profile_deleted: '配置已删除', topic_deleted: '话题已退出并删除', topic_delete_requested: '请求删除话题', topic_delete_failed: '话题退出失败', error: '运行异常', control_error: '控制异常' };
      const kind = event.type || 'event';
      const symbol = kind.startsWith('message') ? 'send' : kind.startsWith('tool') ? 'terminal' : kind.startsWith('topic') ? 'hash' : kind.includes('notice') ? 'radio' : 'trace';
      const summary = event.message || event.error || event.content || event.summary || event.payload || event.output || event.result;
      const topic = data.topics.find(t => t.topic_id === event.topic_id);
      return `<article class="event-item event-timeline-row"><div class="event-marker ${kind.includes('error') ? 'is-error' : ''}">${icon(symbol)}</div><div class="event-main"><div class="event-head"><strong>${escapeHTML(event.name || profileName(event.profile_id))}</strong><span>${escapeHTML(labels[kind] || kind)}</span>${event.status === 'interrupted' ? badge('被新消息打断', 'amber') : ''}<time>${escapeHTML(timestamp(event.ts, true))}</time></div>${summary && typeof summary === 'string' && summary !== 'null' ? `<p class="pre-wrap">${escapeHTML(summary.length > 420 ? `${summary.slice(0, 420)}…` : summary)}${event.truncated ? '（记录已截断）' : ''}</p>` : ''}<div class="event-context">${event.tool_name ? badge(event.tool_name) : ''}${topic ? link(`# ${topic.name}`, 'topics', topic.topic_id) : ''}${Number.isFinite(event.duration_ms) ? `<span>${(event.duration_ms / 1000).toFixed(2)} 秒</span>` : ''}</div><details data-event-id="${attr(id)}"${ui.expandedEvents.has(id) ? ' open' : ''}><summary>查看原始记录</summary><pre>${escapeHTML(JSON.stringify(safeDetails(event), null, 2))}</pre></details></div></article>`;
    }).join('')}</div>`;
  }
  function toast(message, error = false) {
    const element = $('#toast');
    element.textContent = message;
    element.className = `toast${error ? ' error' : ''}`;
    element.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { element.hidden = true; }, error ? 8000 : 4500);
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.method ? 90000 : 10000);
    try {
      const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, ...options });
      let result;
      try { result = await response.json(); } catch (_) { throw new Error(`控制服务返回了无法解析的响应（${response.status}）。`); }
      if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : `操作失败（${response.status}）。`);
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('控制服务响应超时，请稍后重试。');
      if (error instanceof TypeError) throw new Error('无法连接控制服务，请检查服务是否仍在运行。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function updateChrome(error = '') {
    const status = $('#connection-status');
    const stale = connected && Date.now() - lastSuccess > 7000;
    status.className = `connection ${connected ? (stale ? 'stale' : 'online') : 'offline'}`;
    status.innerHTML = `<span class="status-dot"></span>${connected ? (stale ? '状态已过期' : '已连接') : (data ? '连接中断' : '等待连接')}`;
    $('#credentials-label').textContent = '模型配置';
    $('#agent-count').textContent = data ? data.profiles.length : '—';
    $('#admin-endpoint').textContent = data?.admin?.endpoint || '等待连接';
    $('#footer-summary').textContent = data ? `${data.profiles.filter(p => activeStates.has(p.runtime?.state)).length} 运行中 · ${data.profiles.length} 个 Agent · ${data.topics.length} 个 Topic` : '等待状态';
    $('#last-updated').textContent = lastSuccess ? `更新于 ${timestamp(lastSuccess)}` : '尚未更新';
    const banner = $('#connection-banner');
    banner.hidden = connected && !stale;
    if (!banner.hidden) banner.innerHTML = `${escapeHTML(error || (data ? '连接中断。当前展示最后一次读取的状态；恢复连接后自动更新。' : '正在连接控制服务。'))} <button type="button" data-action="refresh">重新连接</button>`;
    document.querySelectorAll('.sidebar [data-page]').forEach(element => {
      if (element.dataset.page === ui.page) element.setAttribute('aria-current', 'page'); else element.removeAttribute('aria-current');
    });
  }
  async function loadState(force = false) {
    if (pollInFlight && !force) return pollInFlight;
    const sequence = ++requestSequence;
    const work = (async () => {
      try {
        const result = await request('/api/state');
        if (sequence < appliedSequence) return;
        if (!result || !Array.isArray(result.profiles) || !Array.isArray(result.hosts)) throw new Error('控制服务状态结构不完整。');
        appliedSequence = sequence;
        // A deletion can remove an author's messages from a shared topic, so
        // invalidate every page/search, not only the deleted topic's cache.
        const historyChanged = (data?.history_revision ?? 0) !== (result.history_revision ?? 0);
        if (historyChanged) {
          chatHistories.clear(); observedTopics.clear(); ui.chatScroll.clear(); ui.expandedEvents.clear();
        }
        data = { ...result, topics: result.topics || [], notices: result.notices || [], events: result.events || [], admin: result.admin || {} };
        const deletedTopicIds = new Set(result.deleted_topic_ids || []);
        const deletedProfileIds = new Set(result.deleted_profile_ids || []);
        for (const id of deletedTopicIds) observedTopics.delete(id);
        for (const [key, history] of chatHistories) if (deletedTopicIds.has(history.topicId)) chatHistories.delete(key);
        if (deletedProfileIds.has(ui.traceProfile)) ui.traceProfile = '';
        data.events = data.events.filter(event => !deletedTopicIds.has(event.topic_id) && !deletedProfileIds.has(event.profile_id));
        data.topics = data.topics.filter(topic => !deletedTopicIds.has(topic.topic_id));
        data.topics.forEach(topic => observedTopics.set(topic.topic_id, topic));
        const liveTopicIds = new Set(data.topics.map(topic => topic.topic_id));
        new Set(data.events.map(event => event.topic_id).filter(Boolean)).forEach(id => {
          if (liveTopicIds.has(id) || deletedTopicIds.has(id)) return;
          const named = data.events.find(event => event.topic_id === id && event.topic_name);
          const known = observedTopics.get(id);
          data.topics.push({ topic_id: id, name: known?.name || named?.topic_name || `历史话题 ${id.slice(0, 8)}`, description: known?.description || '此话题当前未被发现，下面展示已采集的历史消息。', is_admin: false, is_historical: true, members: [] });
        });
        reconcileAgentSelection();
        connected = true;
        lastSuccess = Date.now();
        updateChrome();
        renderPage(!historyChanged);
        if (ui.page === 'topics' && ui.id) refreshTopicHistory();
      } catch (error) {
        if (sequence < appliedSequence) return;
        connected = false;
        updateChrome(error.message);
        if (!data) $('#page-content').innerHTML = empty('尚未连接到控制服务', error.message, button('重新连接', 'refresh'));
        else renderPage(true);
      }
    })();
    pollInFlight = work;
    try { await work; } finally { if (pollInFlight === work) pollInFlight = null; }
  }
  async function mutate(path, payload = {}, method = 'POST', key = path) {
    if (!data?.csrf_token) throw new Error('尚未获取控制服务令牌，请先重新连接。');
    if (busy.has(key)) return null;
    busy.add(key);
    renderPage(true);
    try {
      const result = await request(path, { method, headers: { 'Content-Type': 'application/json', 'X-Control-Token': data.csrf_token }, body: JSON.stringify(payload) });
      await loadState(true);
      return result;
    } finally { busy.delete(key); renderPage(true); }
  }

  function profileActions(profile, compact = true) {
    const runtime = profile.runtime || {};
    const active = runningStates.has(runtime.state);
    const pending = busy.has(`profile:${profile.id}`);
    const canStart = canStartProfile(profile);
    return `${button(icon('edit') + ' 编辑', 'edit-profile', profile.id, 'secondary', active || pending || runtime.cleanup_pending || !connected)}${active ? button(icon('stop') + (runtime.state === 'stopping' ? '停止中' : '停止'), 'stop-profile', profile.id, 'secondary', runtime.state === 'stopping' || pending || !connected) : button(icon('play') + (runtime.cleanup_pending ? '清理中' : pending ? '处理中' : '启动'), 'start-profile', profile.id, 'primary', !canStart || !connected)}${runtime.state === 'failed' && runtime.pid ? button(icon('stop') + ' 停止', 'stop-profile', profile.id, 'secondary', pending || !connected) : ''}${button(icon('trash') + (runtime.cleanup_pending ? '重试清理' : ' 删除'), 'delete-profile', profile.id, 'danger', active || !!runtime.pid || pending || !connected)}`;
  }
  function renderOverview() {
    const profiles = data.profiles;
    const topics = data.topics;
    if (!profiles.length) return empty('让第一个 Agent 加入工作区', '创建独立的人设与启动任务，选择本地或 SSH 主机。启动后，在这里查看真实协作关系。', button(icon('plus') + ' 创建 Agent', 'new-profile', '', 'primary') + button('配置运行主机', 'go-hosts'));
    const active = profiles.filter(p => activeStates.has(p.runtime?.state)).length;
    return `<div class="summary-strip"><div class="summary-stat"><strong>${profiles.length}</strong><span>已登记 Agent</span></div><div class="summary-stat"><strong>${active}</strong><span>运行中</span></div><div class="summary-stat"><strong>${topics.length}</strong><span>协作 Topic</span></div></div>${window.AgentTopology.render(data, { escapeHTML, icon, agentAvatar, stateBadge })}${!data.profiles.every(modelReady) ? `<div class="spaced">${infoNote('部分 Agent 的模型配置尚未设置 API Key，请前往“模型配置”补充。', true)}</div>` : ''}<div class="section-toolbar spaced"><strong>最近事件</strong>${link('查看全部 →', 'trace')}</div>${eventList(sortedEvents().slice(0, 4))}`;
  }
  function agentTone(name) {
    // An initial keeps a person's color stable across pages and restarts.
    return String((String(name || '?').toLocaleLowerCase().codePointAt(0) || 0) % 6);
  }
  function agentAvatar(profileOrName, size = 'md') {
    const name = typeof profileOrName === 'string' ? profileOrName : profileOrName?.name || '?';
    const tone = agentTone(name);
    const sizes = new Set(['xs', 'sm', 'md', 'lg', 'xl']);
    const safeSize = sizes.has(size) ? size : 'md';
    const hairstyles = [
      '<path d="M20 31c-2-14 6-21 14-21 10 0 17 8 14 22l-5-12c-6 5-11 6-19 4z"/>',
      '<path d="M18 32c-2-13 3-22 16-22s19 10 15 24l-6-8-4-8-7 7-9 1z"/>',
      '<path d="M18 31c-1-12 7-20 18-19 11 0 16 8 14 19l-7-7c-7 2-15 1-21 6z"/><path d="M18 21h33l-4-7H23z"/>',
      '<path d="M19 35c-5-19 3-26 15-26 16 0 22 12 15 31l-5-16c-8 1-14-1-18-6l-5 18z"/>',
      '<path d="M18 30c-1-10 3-18 17-18 10 0 16 7 15 18l-8-5-6-8-8 10z"/>',
      '<path d="M19 33c-3-13 2-23 16-23 12 0 18 10 15 24l-6-10c-7-1-12-3-17-6l-5 17z"/>'
    ];
    return `<span class="agent-avatar agent-avatar-${safeSize} agent-tone-${tone}" aria-hidden="true"><svg viewBox="0 0 68 68" focusable="false"><circle class="agent-avatar-back" cx="34" cy="34" r="34"/><path class="agent-avatar-shirt" d="M9 68c1-17 10-25 25-25s24 8 25 25Z"/><path class="agent-avatar-neck" d="M28 41h12v10c-2 5-10 5-12 0Z"/><ellipse class="agent-avatar-face" cx="34" cy="31" rx="15" ry="18"/><g class="agent-avatar-hair">${hairstyles[Number(tone)]}</g><path class="agent-avatar-feature" d="M27 31h.1M41 31h.1"/><path class="agent-avatar-smile" d="M30 39q4 3 8 0"/><path class="agent-avatar-collar" d="m24 47 10 8 10-8"/></svg></span>`;
  }
  function agentPersonaExcerpt(profile) {
    const blocks = String(profile.persona || '').split(/\n\s*\n/).map(item => item.trim()).filter(Boolean);
    return blocks.length ? blocks[blocks.length - 1] : '还没有填写人设。';
  }
  function agentRole(profile) {
    const own = agentPersonaExcerpt(profile).slice(0, 90);
    const roles = [{ phrase: '面包店', label: '面包店店主' }, { phrase: '木匠', label: '木匠' }, { phrase: '花店', label: '花店店主' }];
    const found = roles.map(role => ({ ...role, index: own.indexOf(role.phrase) })).filter(role => role.index >= 0).sort((a, b) => a.index - b.index)[0];
    return found?.label || '独立 Agent';
  }
  function agentEvents(profile) {
    return (data.events || []).filter(event => event.profile_id === profile.id).slice().sort((a, b) => dateNumber(a.ts) - dateNumber(b.ts));
  }
  function agentJoinedTopics(profile) {
    return data.topics.filter(topic => (topic.members || []).some(member => member.profile_id === profile.id));
  }
  function agentActivityBars(events) {
    if (!events.length) return '<span class="agent-quiet">暂无活动记录</span>';
    const dates = events.map(event => dateNumber(event.ts)).filter(Number.isFinite);
    if (!dates.length) return '<span class="agent-quiet">暂无活动记录</span>';
    const start = Math.min(...dates), end = Math.max(...dates), span = Math.max(1, end - start);
    const bins = Array(12).fill(0);
    dates.forEach(date => { bins[Math.min(11, Math.floor((date - start) / span * 12))]++; });
    const maximum = Math.max(...bins, 1);
    return `<svg class="agent-spark" viewBox="0 0 144 30" role="img" aria-label="当前保留事件从早到晚的活动分布，共 ${events.length} 条"><path class="agent-spark-base" d="M0 29H144"/>${bins.map((value, i) => { const height = value ? 3 + value / maximum * 23 : 1; return `<rect x="${i * 12 + 2}" y="${29 - height}" width="7" height="${height}" rx="2"/>`; }).join('')}</svg>`;
  }
  function agentCountRing(running, total) {
    const circumference = 2 * Math.PI * 24;
    return `<svg class="agent-summary-ring" viewBox="0 0 64 64" role="img" aria-label="${total} 个 Agent 中 ${running} 个运行中"><circle class="agent-ring-track" cx="32" cy="32" r="24"/><circle class="agent-ring-value" cx="32" cy="32" r="24" stroke-dasharray="${circumference * running / Math.max(total, 1)} ${circumference}" transform="rotate(-90 32 32)"/><text x="32" y="37" text-anchor="middle">${running}</text></svg>`;
  }
  function agentActivityMix(events) {
    const kinds = [
      { label: '发出消息', value: events.filter(event => event.type === 'message_sent').length, tone: 0 },
      { label: '接收消息', value: events.filter(event => event.type === 'message_received').length, tone: 3 },
      { label: '生成回合', value: events.filter(event => event.type === 'model_finished').length, tone: 4 },
      { label: '工具执行', value: events.filter(event => event.type === 'tool_finished').length, tone: 1 },
    ];
    const maximum = Math.max(...kinds.map(item => item.value), 1);
    return `<div class="agent-mix">${kinds.map(item => `<div class="agent-mix-row agent-tone-${item.tone}"><span>${item.label}</span><svg viewBox="0 0 180 8" role="img" aria-label="${item.label} ${item.value} 次"><rect class="agent-mix-track" width="180" height="8" rx="4"/><rect class="agent-mix-value" width="${180 * item.value / maximum}" height="8" rx="4"/></svg><strong>${item.value}</strong></div>`).join('')}</div><p class="agent-window-note">基于控制台当前保留的最近 ${data.events.length} 条事件，非历史累计。</p>`;
  }
  function agentTopicMap(profile, topics) {
    if (!topics.length) return `<div class="agent-no-topics">${agentAvatar(profile, 'lg')}<span class="agent-connection-dashes"></span><span class="agent-empty-topic">${icon('hash')}</span><p>还没有加入 Topic</p><small>加入后，这里会显示实际成员关系。</small></div>`;
    const visible = topics.slice(0, 6), height = Math.max(180, visible.length * 72 + 16), center = height / 2;
    return `<svg class="agent-topic-map agent-tone-${agentTone(profile.name)}" viewBox="0 0 600 ${height}" role="img" aria-label="${attr(profile.name)} 与 ${topics.length} 个已加入 Topic 的成员关系"><title>${escapeHTML(profile.name)} 的 Topic 成员关系</title>${visible.map((topic, index) => { const y = index * 72 + 44; return `<path class="agent-map-edge" d="M104 ${center} C180 ${center},175 ${y},246 ${y}"/><a href="#topics/${encodeURIComponent(topic.topic_id)}" aria-label="${attr(`查看 Topic ${topic.name}`)}"><rect class="agent-map-topic" x="246" y="${y - 25}" width="330" height="50" rx="10"/><text class="agent-map-hash" x="264" y="${y + 5}">#</text><text class="agent-map-label" x="290" y="${y - 3}">${escapeHTML(topic.name.length > 23 ? `${topic.name.slice(0, 23)}…` : topic.name)}</text><text class="agent-map-sub" x="290" y="${y + 15}">${(topic.members || []).length} 位成员${topic.is_admin ? ' · 管理员频道' : ''}</text><title>${escapeHTML(topic.name)}</title></a>`; }).join('')}<circle class="agent-map-core" cx="78" cy="${center}" r="28"/><text class="agent-map-initial" x="78" y="${center + 7}" text-anchor="middle">${escapeHTML(Array.from(profile.name || '?')[0].toUpperCase())}</text><text class="agent-map-name" x="78" y="${center + 49}" text-anchor="middle">${escapeHTML(profile.name)}</text></svg>${topics.length > visible.length ? `<div class="agent-more-topics">${topics.slice(6).map(topic => link(`# ${topic.name}`, 'topics', topic.topic_id, 'agent-topic-pill')).join('')}</div>` : ''}<p class="agent-window-note">连线表示当前加入关系，点击话题进入群聊。</p>`;
  }
  function renderAgents() {
    if (ui.id) return renderAgentDetail();
    if (!data.profiles.length) return empty('还没有 Agent 配置', '为 Agent 设置人设、初始任务和运行主机。配置保留，运行可以单独启动或停止。', button(icon('plus') + ' 创建 Agent', 'new-profile', '', 'primary'));
    const active = data.profiles.filter(profile => activeStates.has(profile.runtime?.state)).length;
    const talking = data.profiles.filter(profile => profile.runtime?.state === 'working').length;
    const failed = data.profiles.filter(profile => profile.runtime?.state === 'failed').length;
    const messageCount = data.events.filter(event => event.type === 'message_sent').length;
    const cards = data.profiles.map(profile => {
      const r = profile.runtime || {}, events = agentEvents(profile), joined = agentJoinedTopics(profile);
      const lastMessage = events.filter(event => event.type === 'message_sent').at(-1);
      const lastOutput = events.filter(event => event.type === 'model_finished' && event.output).at(-1);
      const recent = lastMessage || lastOutput;
      const summary = recent ? (lastMessage ? lastMessage.payload : lastOutput.output) : '';
      const sent = events.filter(event => event.type === 'message_sent').length;
      return `<article class="agent-person-card agent-tone-${agentTone(profile.name)}${ui.selectedAgents.has(profile.id) ? ' is-selected' : ''}"><div class="agent-card-select-row"><label><input type="checkbox" id="select-agent-${attr(profile.id)}" data-agent-select="${attr(profile.id)}" aria-label="选择 ${attr(profile.name)}"${ui.selectedAgents.has(profile.id) ? ' checked' : ''}${!canStartProfile(profile) || ui.batch?.running || !connected ? ' disabled' : ''}><span>${ui.selectedAgents.has(profile.id) ? '已选择' : canStartProfile(profile) ? '选择' : '当前不可启动'}</span></label>${stateBadge(profile)}</div><div class="agent-card-top"><a class="agent-portrait-link" href="#agents/${encodeURIComponent(profile.id)}" aria-label="${attr(`查看 ${profile.name} 详情`)}">${agentAvatar(profile, 'lg')}<span class="agent-presence ${activeStates.has(r.state) ? 'agent-presence-online' : r.state === 'failed' ? 'agent-presence-error' : ''}"></span></a><div class="agent-card-identity"><h2>${link(profile.name, 'agents', profile.id, 'agent-name-link')}</h2><span class="agent-role-label">${escapeHTML(agentRole(profile))}</span></div></div><p class="agent-persona-preview">${escapeHTML(agentPersonaExcerpt(profile))}</p><div class="agent-card-meta"><span>${icon('server')}${escapeHTML(hostName(profile.host_id))}</span><span>${icon('agents')}${escapeHTML(profile.model || '未设置模型')}</span></div><div class="agent-card-counters"><div><strong>${joined.length}</strong><span>参与话题</span></div><div><strong>${sent}</strong><span>发出消息</span></div><div class="agent-card-activity">${agentActivityBars(events)}<span>近期活动</span></div></div><div class="agent-card-topics">${joined.slice(0, 2).map(topic => link(`# ${topic.name}`, 'topics', topic.topic_id, 'agent-topic-pill')).join('')}${joined.length > 2 ? link(`+${joined.length - 2}`, 'agents', profile.id, 'agent-topic-pill agent-topic-more') : ''}${!joined.length ? '<span class="agent-quiet">还没有加入 Topic</span>' : ''}</div><div class="agent-last-message"><div class="agent-last-heading"><span>${lastMessage ? '最近发言' : lastOutput ? '最近回复 · 未发送到 Topic' : '等待新的故事'}</span>${recent ? `<time>${escapeHTML(timestamp(recent.ts))}</time>` : ''}</div><p>${summary ? escapeHTML(summary) : '启动并参与话题后，在这里查看最新动态。'}</p></div>${r.error ? `<div class="agent-card-error">${errorNote(r.error)}</div>` : ''}<div class="agent-card-footer"><a class="agent-detail-link" href="#agents/${encodeURIComponent(profile.id)}">个人详情 ${icon('arrow')}</a><div class="agent-card-actions">${profileActions(profile)}</div></div></article>`;
    }).join('');
    return `<div class="agent-workspace-overview"><div class="agent-overview-live">${agentCountRing(active, data.profiles.length)}<div><strong>${active} <span>/ ${data.profiles.length}</span></strong><p>Agent 正在运行</p></div></div><div class="agent-overview-stat"><span class="agent-stat-symbol">${icon('trace')}</span><div><strong>${talking}</strong><p>正在执行</p></div></div><div class="agent-overview-stat"><span class="agent-stat-symbol">${icon('send')}</span><div><strong>${messageCount}</strong><p>窗口内发言</p></div></div><div class="agent-overview-stat"><span class="agent-stat-symbol${failed ? ' agent-stat-error' : ''}">${icon(failed ? 'info' : 'check')}</span><div><strong>${failed}</strong><p>异常成员</p></div></div></div>${renderBatchStart()}<div class="agent-card-grid">${cards}</div><p class="agent-window-note">发言数量与活动图来自控制台当前保留的最近 ${data.events.length} 条事件，不代表历史累计。运行期间可查看详情；编辑与删除需先停止 Agent。</p>`;
  }
  function renderAgentDetail() {
    const p = data.profiles.find(profile => profile.id === ui.id);
    if (!p) return empty('没有找到这个 Agent', '该配置可能已移除，返回列表查看当前成员。', link('返回 Agents', 'agents', '', 'button secondary'));
    const r = p.runtime || {}, joined = agentJoinedTopics(p), events = agentEvents(p);
    const lastMessage = events.filter(event => event.type === 'message_sent').at(-1);
    const sent = events.filter(event => event.type === 'message_sent').length;
    const received = events.filter(event => event.type === 'message_received').length;
    const lastSeen = r.last_seen ? timestamp(r.last_seen) : '尚未运行';
    const personaId = `agent-persona-${p.id}`, runtimeId = `agent-runtime-${p.id}`;
    return `${back('agents', '返回 Agent 列表')}<div class="agent-profile-hero agent-tone-${agentTone(p.name)}"><div class="agent-profile-person">${agentAvatar(p, 'xl')}<div><span class="agent-role-label">${escapeHTML(agentRole(p))}</span><h2>${escapeHTML(p.name)}</h2><div class="agent-profile-status">${stateBadge(p)}<span>${icon('server')}${escapeHTML(hostName(p.host_id))}</span><span>${escapeHTML(p.model || '未设置模型')}</span></div></div></div><div class="agent-profile-actions">${profileActions(p)}</div><div class="agent-profile-quote"><span>${icon('info')}</span><p>${escapeHTML(agentPersonaExcerpt(p))}</p></div></div>${r.error ? `<div class="spaced">${errorNote(r.error)}</div>` : ''}<div class="agent-detail-metrics"><div><span>${icon('hash')}</span><strong>${joined.length}</strong><p>已加入话题</p></div><div><span>${icon('send')}</span><strong>${sent}</strong><p>窗口内发出</p></div><div><span>${icon('radio')}</span><strong>${received}</strong><p>窗口内接收</p></div><div><span>${icon('trace')}</span><strong class="agent-metric-time">${escapeHTML(lastSeen)}</strong><p>最近心跳 / 事件</p></div></div><div class="agent-detail-grid"><section class="panel agent-relationship-panel"><div class="panel-header"><div><h2>参与的对话</h2><small>Agent 与 Topic 的实际关系</small></div>${badge(`${joined.length} 个话题`, 'blue')}</div><div class="agent-map-wrap">${agentTopicMap(p, joined)}</div></section><section class="panel agent-activity-panel"><div class="panel-header"><h2>近期活动分布</h2>${icon('trace')}</div><div class="panel-body">${agentActivityMix(events)}<div class="agent-admin-membership">${icon(r.admin_joined ? 'check' : 'radio')}<div><strong>${r.admin_joined ? '已加入管理员频道' : '尚未加入管理员频道'}</strong><p>${r.admin_joined ? '管理员通知可由运行时接收。' : r.invite_error ? escapeHTML(r.invite_error) : '启动并完成邀请后可接收管理员通知。'}</p></div></div></div></section></div>${lastMessage ? `<section class="panel agent-detail-message"><span class="agent-detail-message-icon">${icon('send')}</span><div><div class="agent-last-heading"><strong>最近发言</strong><time>${escapeHTML(timestamp(lastMessage.ts, true))}</time></div><p>${escapeHTML(lastMessage.payload || '')}</p>${link('打开这段对话 →', 'topics', lastMessage.topic_id, 'agent-detail-link')}</div></section>` : ''}<div class="agent-detail-folds"><details class="panel agent-fold" data-event-id="${attr(personaId)}"${ui.expandedEvents.has(personaId) ? ' open' : ''}><summary><span>${icon('agents')}人设与启动安排</span><small>查看完整配置</small></summary><div class="panel-body"><section class="detail-section"><h3>完整人设</h3><p class="pre-wrap">${escapeHTML(p.persona || '未设置')}</p></section><section class="detail-section"><h3>启动任务</h3><p class="pre-wrap">${escapeHTML(p.initial_prompt || '未设置')}</p></section><section class="detail-section"><h3>模型配置</h3><dl class="facts"><dt>模型配置</dt><dd>${escapeHTML(providerFor(p)?.name || '未选择')} · ${escapeHTML(p.model)}</dd><dt>API 地址</dt><dd>${escapeHTML(p.base_url)}</dd><dt>长期记忆</dt><dd>${p.enable_memory ? '启用' : '关闭'}</dd><dt>思考模式</dt><dd>${providerFor(p)?.api_mode === 'deepseek' ? (p.thinking ? '启用' : '关闭') : '使用模型默认策略'}</dd></dl></section></div></details><details class="panel agent-fold" data-event-id="${attr(runtimeId)}"${ui.expandedEvents.has(runtimeId) ? ' open' : ''}><summary><span>${icon('terminal')}运行与连接信息</span><small>PID / ID / 地址</small></summary><div class="panel-body"><dl class="facts"><dt>配置 ID</dt><dd class="mono">${escapeHTML(p.id)}</dd><dt>模型 User ID</dt><dd class="mono">${escapeHTML(p.user_id || `agent_${p.id}`)}<div class="cell-sub">用于请求身份与缓存隔离。</div></dd><dt>运行主机</dt><dd>${escapeHTML(hostName(p.host_id))}</dd><dt>进程 PID</dt><dd class="mono">${escapeHTML(r.pid || '—')}</dd><dt>Agent ID</dt><dd class="mono">${escapeHTML(r.agent_id || '—')}</dd><dt>通信地址</dt><dd class="mono">${escapeHTML(r.endpoint || '—')}</dd><dt>Run ID</dt><dd class="mono">${escapeHTML(r.run_id || '—')}</dd><dt>最后事件</dt><dd>${escapeHTML(timestamp(r.last_seen, true))}</dd></dl></div></details></div><div class="section-toolbar spaced"><strong>活动时间线</strong>${button('查看全部事件 ' + icon('arrow'), 'trace-agent', p.id, 'ghost')}</div>${eventList(sortedEvents(event => event.profile_id === p.id).slice(0, 12))}`;
  }
  function chatHistory(topicId = ui.id, query = ui.chatQuery.trim()) {
    const key = JSON.stringify([topicId, query]);
    if (!chatHistories.has(key)) {
      chatHistories.set(key, { topicId, query, messages: [], total: 0, newest: 0, before: null, moreBefore: false, initialized: false, loading: false, error: '', key });
      if (chatHistories.size > 12) chatHistories.delete(chatHistories.keys().next().value);
    }
    return chatHistories.get(key);
  }
  function archivedMessage(message) {
    return { ...message, profileId: message.profile_id, isNotice: !!message.notice_id,
      notice: message.notice_id ? data.notices.find(n => n.id === message.notice_id) : null };
  }
  async function refreshTopicHistory(mode = 'newer') {
    if (!data || ui.page !== 'topics' || !ui.id || (data.deleted_topic_ids || []).includes(ui.id)) return;
    const history = chatHistory();
    if (history.loading || (mode === 'older' && (!history.initialized || !history.moreBefore))) return;
    const olderError = mode === 'newer' && history.lastMode === 'older' ? history.error : '';
    history.loading = true;
    if (!olderError) { history.error = ''; history.lastMode = mode; }
    renderPage(true);
    try {
      let pages = 0, more = false;
      do {
        const initial = !history.initialized;
        const params = new URLSearchParams({ limit: '50' });
        if (history.query) params.set('q', history.query);
        if (!initial) params.set(mode === 'older' ? 'before' : 'after', String(mode === 'older' ? history.before : history.newest));
        const result = await request(`/api/topics/${encodeURIComponent(history.topicId)}/messages?${params}`);
        // Requests started before a purge must never repopulate a new cache.
        if (chatHistories.get(history.key) !== history) return;
        if (result.history_revision !== undefined && data.history_revision !== undefined && result.history_revision !== data.history_revision) {
          if (result.history_revision > data.history_revision) await loadState(true);
          return;
        }
        if (!Array.isArray(result.messages)) throw new Error('历史记录响应不完整，请重试。');
        const messages = new Map(history.messages.map(message => [message.key, message]));
        result.messages.forEach(message => messages.set(message.key, message));
        history.messages = [...messages.values()].sort((a, b) => a.seq - b.seq);
        history.total = result.total;
        if (initial || mode === 'older') {
          history.before = result.next_before;
          history.moreBefore = !!result.has_more;
        }
        if (initial || mode !== 'older') history.newest = Math.max(history.newest, result.latest_cursor || 0);
        history.initialized = true;
        more = !initial && mode !== 'older' && result.has_more && result.messages.length > 0;
      } while (more && ++pages < 5);
    } catch (error) { history.error = olderError || error.message; }
    finally {
      history.loading = false;
      if (chatHistories.get(history.key) === history && ui.page === 'topics' && ui.id === history.topicId && ui.chatQuery.trim() === history.query) renderPage();
    }
  }
  function renderHistoryControls(history) {
    const progress = history.initialized ? `${history.query ? '匹配' : '共'} ${history.total} 条 · 已加载 ${history.messages.length} 条` : '正在读取已保存的完整历史';
    return `<div class="chat-history-controls"><div class="chat-history-note">${icon('info')}${progress}${history.loading ? ' · 加载中…' : ''}</div>${history.error ? `<div class="chat-history-error" role="alert">${escapeHTML(history.error)} ${button('重试加载', 'chat-history-retry', '', 'secondary', history.loading)}</div>` : ''}${history.moreBefore ? button(history.loading ? '加载中…' : '加载更早的消息', 'chat-history-older', '', 'secondary', history.loading) : history.initialized && history.messages.length ? '<span class="chat-history-start">已到最早的已保存消息</span>' : ''}</div>`;
  }
  function topicMessages(topic) {
    if (ui.page === 'topics' && ui.id === topic.topic_id) return chatHistory().messages.map(archivedMessage);
    if (topic.last_message) return [archivedMessage(topic.last_message)];
    // A send event represents one broadcast. Receiver observations have no
    // shared message ID, so they remain in the activity view, never text-deduped.
    const messages = data.events.filter(e => e.topic_id === topic.topic_id && e.type === 'message_sent').map(e => ({
      key: `sent-${e.id}`, ts: e.ts, author: e.name || profileName(e.profile_id), profileId: e.profile_id,
      content: typeof e.payload === 'string' ? e.payload : '', truncated: !!e.truncated, eventId: e.id,
    }));
    const noticeIds = new Set(data.events.filter(e => e.topic_id === topic.topic_id && e.notice_id).map(e => e.notice_id));
    data.notices.filter(n => n.topic_id === topic.topic_id || noticeIds.has(n.id)).forEach(n => {
      messages.push({ key: `notice-${n.id}`, ts: n.created_at, author: '管理员', content: n.content, notice: n });
    });
    return messages.sort((a, b) => dateNumber(a.ts) - dateNumber(b.ts));
  }
  function topicSymbol(topic, small = false) {
    return `<span class="chat-topic-symbol${topic.is_admin ? ' is-admin' : ''}${small ? ' small' : ''}">${icon(topic.is_admin ? 'radio' : 'hash')}</span>`;
  }
  function chatMembers(topic) {
    if (!topic.is_historical) return topic.members || [];
    if (topic.participants || topic.historical_members || topic.members?.length) return topic.participants || topic.historical_members || topic.members;
    const ids = new Set(data.events.filter(e => e.topic_id === topic.topic_id && e.type === 'message_sent').map(e => e.profile_id));
    return [...ids].map(id => ({ profile_id: id, name: data.profiles.find(p => p.id === id)?.name || data.events.find(e => e.profile_id === id)?.name || String(id).slice(0, 8) }));
  }
  function chatExcerpt(value, limit = 76) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return escapeHTML(text.length > limit ? text.slice(0, limit) + '…' : text);
  }
  function canDeleteTopic(topic) {
    return !!topic && topic.can_delete !== false && !topic.is_admin;
  }
  function topicDeleteAction(topic) {
    return canDeleteTopic(topic) ? button(icon('trash') + ' 删除话题', 'delete-topic', topic.topic_id, 'danger', isBusy(`topic:${topic.topic_id}`)) : `<span class="topic-protected" title="${attr(topic.delete_reason || '管理员频道用于全体通知，暂不支持删除。')}">${icon('key')}管理员频道保留</span>`;
  }
  function renderTopicDeletionStatus() {
    const pending = (data.topic_deletions || []).filter(item => item.pending_profiles?.length);
    if (!pending.length) return '';
    return `<section class="panel topic-deletion-status" aria-label="话题删除同步状态"><h2>${icon('refresh')}正在同步话题退出</h2>${pending.map(item => `<div><strong>${escapeHTML(item.name || observedTopics.get(item.topic_id)?.name || item.topic_id.slice(0, 12))}</strong><p>等待 ${item.pending_profiles.map(id => escapeHTML(profileName(id))).join('、')} 确认退出；控制台会自动重试。</p>${item.errors && Object.values(item.errors).length ? `<small>${escapeHTML(Object.values(item.errors).join('；'))}</small>` : ''}</div>`).join('')}</section>`;
  }
  function renderCleanupStatus() {
    const jobs = data.cleanup_jobs || [];
    if (!jobs.length) return '';
    return `<section class="panel topic-deletion-status" aria-label="级联清理状态" role="status"><h2>${icon('refresh')}关联数据正在清理</h2>${jobs.map(job => `<div><strong>${escapeHTML(job.name || job.id)}</strong><p>${job.kind === 'profile' ? 'Agent' : 'Topic'} 删除尚未完成，控制台会自动重试；重启服务后也会继续。</p>${job.error ? `<small>${escapeHTML(job.error)}</small>` : ''}</div>`).join('')}</section>`;
  }
  function renderTopics() {
    if (ui.id) return renderTopicDetail();
    if (!data.topics.length) return renderTopicDeletionStatus() + empty('还没有协作 Topic', '居民创建话题后，这里会显示他们的群聊。管理员公告也可以在这里查看。', button(icon('plus') + ' 创建管理员 Topic', 'new-topic', '', 'primary'));
    const rooms = data.topics.map(t => ({ topic: t, messages: topicMessages(t) })).sort((a, b) => dateNumber(b.messages.at(-1)?.ts) - dateNumber(a.messages.at(-1)?.ts));
    return `${renderTopicDeletionStatus()}<div class="chat-directory-intro"><div class="chat-directory-art" aria-hidden="true">${icon('radio')}${icon('hash')}${icon('send')}</div><div><h2>每个话题，都是一间群聊</h2><p>看看大家正在聊什么，点击卡片进入对话。</p></div><div class="chat-directory-total"><strong>${rooms.length}</strong><span>个话题</span></div></div><div class="chat-room-grid">${rooms.map(({ topic: t, messages }) => {
      const latest = messages.at(-1), members = chatMembers(t);
      return `<article class="chat-room-card panel"><a class="chat-room-card-link" href="#topics/${encodeURIComponent(t.topic_id)}"><div class="chat-room-heading">${topicSymbol(t)}<div><h3>${escapeHTML(t.name)}</h3><span>${t.is_admin ? '管理员公告' : t.is_historical ? '历史群聊' : '成员群聊'} · ${members.length} 位${t.is_historical ? '历史参与者' : '受管成员'}</span></div>${icon('arrow')}</div><p class="chat-room-description">${escapeHTML(t.description || '话题成员在这里交流。')}</p><div class="chat-room-preview"><span>${latest ? escapeHTML(latest.author) + ' · ' + escapeHTML(timestamp(latest.ts)) : '等待第一条消息'}</span><p>${latest ? chatExcerpt(latest.content, 100) : '有新的对话时，消息会自动出现在这里。'}</p></div></a><div class="chat-room-footer"><div class="chat-avatar-stack">${members.slice(0, 5).map(m => agentAvatar(m, 'sm')).join('')}${members.length > 5 ? `<span>+${members.length - 5}</span>` : ''}</div><span>${(t.message_count ?? messages.length) ? `${t.message_count ?? messages.length} 条消息` : '暂无消息'}</span></div><div class="chat-room-actions">${topicDeleteAction(t)}${link('进入群聊 →', 'topics', t.topic_id, 'text-link')}</div></article>`;
    }).join('')}</div><p class="page-note">群聊按话题保存完整正文，支持加载更早消息与搜索全部已保存历史。接收记录和工具调用位于“运行事件”。</p>`;
  }
  function renderTopicDetail() {
    const t = data.topics.find(topic => topic.topic_id === ui.id);
    if (!t) return empty('没有找到这个 Topic', '返回列表查看控制服务当前可见的 Topic。', link('返回 Topics', 'topics', '', 'button secondary'));
    const history = chatHistory(), allMessages = topicMessages(t), query = ui.chatQuery.trim().toLocaleLowerCase();
    const messages = allMessages;
    const events = sortedEvents(e => e.topic_id === t.topic_id || e.topic?.topic_id === t.topic_id);
    const members = chatMembers(t), online = members.filter(m => activeStates.has(data.profiles.find(p => p.id === m.profile_id)?.runtime?.state)).length;
    const scrollKey = JSON.stringify([t.topic_id, ui.chatTab, ui.chatQuery]);
    let lastDate = '';
    const bubbles = messages.map(m => {
      const date = new Date(m.ts).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' });
      const separator = date !== lastDate ? `<div class="chat-date-separator"><span>${escapeHTML(date)}</span></div>` : '';
      lastDate = date;
      const p = data.profiles.find(profile => profile.id === m.profileId);
      const receipts = m.notice ? receiptStats(m.notice) : null;
      return `${separator}<article class="chat-message${m.isNotice ? ' from-admin' : ''}" data-message-key="${attr(m.key)}" data-message-seq="${Number(m.seq) || 0}">${m.isNotice ? `<span class="chat-admin-avatar">${icon('radio')}</span>` : (p ? `<a href="#agents/${encodeURIComponent(p.id)}" aria-label="查看 ${attr(m.author)}">${agentAvatar(p, 'sm')}</a>` : agentAvatar(m.author, 'sm'))}<div class="chat-message-main"><div class="chat-message-meta">${p ? link(m.author, 'agents', p.id) : `<strong>${escapeHTML(m.author)}</strong>`}${m.isNotice ? badge('公告', 'blue') : ''}<time>${escapeHTML(timestamp(m.ts))}</time></div><div class="chat-bubble"><p>${escapeHTML(m.content || '（空消息）')}</p>${m.truncated ? '<small class="chat-truncated">旧版本只保存了部分正文，未找到可恢复的原文。</small>' : ''}</div>${receipts ? `<a class="chat-message-receipt" href="#notices/${encodeURIComponent(m.notice.id)}">${icon(receipts.applied === receipts.total && receipts.total ? 'check' : 'info')}${receipts.applied} / ${receipts.total} 已写入上下文 · 查看回执</a>` : ''}</div></article>`;
    }).join('');
    const roomRail = data.topics.map(room => {
      const latest = topicMessages(room).at(-1);
      return `<a class="chat-room-switch${room.topic_id === t.topic_id ? ' selected' : ''}" href="#topics/${encodeURIComponent(room.topic_id)}"${room.topic_id === t.topic_id ? ' aria-current="page"' : ''}>${topicSymbol(room, true)}<span><strong>${escapeHTML(room.name)}</strong><small>${latest ? chatExcerpt(`${latest.author}：${latest.content}`, 40) : `${(room.members || []).length} 位成员 · 暂无消息`}</small></span></a>`;
    }).join('');
    const technicalId = `topic-info-${t.topic_id}`;
    return `<div class="chat-back-row">${link('← 所有 Topics', 'topics', '', 'text-link')}<div class="chat-back-actions">${topicDeleteAction(t)}</div></div><div class="chat-shell${ui.chatMembers ? ' show-members' : ''}"><nav class="chat-room-rail" aria-label="话题会话"><div class="chat-rail-title">${icon('hash')}话题会话 <span>${data.topics.length}</span></div>${roomRail}</nav><section class="chat-conversation" aria-label="${attr(t.name)}群聊"><header class="chat-header">${topicSymbol(t)}<div><h2>${escapeHTML(t.name)}</h2><p>${members.length} 位${t.is_historical ? '历史参与者' : '受管成员'} <span>·</span> ${online} 位运行中 ${t.is_admin ? '<span>·</span> 管理员公告频道' : ''}</p></div><button type="button" class="icon-button chat-members-toggle" data-action="toggle-chat-members" aria-label="查看群成员" aria-expanded="${ui.chatMembers}">${icon('agents')}</button></header><div class="chat-toolbar"><div class="chat-tabs" role="group" aria-label="话题视图"><button type="button" data-action="chat-messages" aria-pressed="${ui.chatTab === 'messages'}">聊天 <span>${history.initialized ? history.total : (t.message_count ?? '…')}</span></button><button type="button" data-action="chat-events" aria-pressed="${ui.chatTab === 'events'}">运行事件 <span>${events.length}</span></button></div>${ui.chatTab === 'messages' ? `<label class="chat-search">${icon('trace')}<input id="chat-search" type="search" placeholder="搜索全部历史" aria-label="搜索全部历史消息或发言人" value="${attr(ui.chatQuery)}"></label>` : ''}</div><div class="chat-scroll" id="chat-stream" data-scroll-key="${attr(scrollKey)}" tabindex="0" aria-label="${ui.chatTab === 'messages' ? '聊天消息' : '话题运行事件'}">${ui.chatTab === 'messages' ? `${renderHistoryControls(history)}${bubbles || (!history.initialized || history.loading || history.error ? '' : `<div class="chat-empty">${topicSymbol(t)}<h3>${query ? '没有匹配的消息' : '对话还没有开始'}</h3><p>${query ? '换一个关键词，或清空搜索查看全部消息。' : '成员发送消息后，会以聊天气泡显示在这里。'}</p></div>`)}` : eventList(events, '还没有与此话题关联的运行事件。')}</div><div class="chat-jump-slot"><button type="button" class="button secondary chat-jump" data-action="chat-latest" hidden>${icon('arrow')}回到最新消息</button></div>${t.is_admin ? `<form id="chat-notice-form" class="chat-composer"><label class="sr-only" for="chat-notice-content">管理员通知内容</label><textarea id="chat-notice-content" name="content" rows="3" maxlength="12000" required placeholder="发布一条镇务公告，通知全体 Agent…">${escapeHTML(ui.chatDraft || '')}</textarea><div><span>广播至全部 ${data.profiles.length} 个已登记 Agent，包括未运行的成员</span><button type="submit" class="button primary"${isBusy('chat-notice') || !data.profiles.length ? ' disabled' : ''}>${icon('send')}${busy.has('chat-notice') ? '发送中…' : '发送公告'}</button></div></form>` : `<div class="chat-observer-bar"><div>${icon('info')}<span>你正在旁观成员对话</span></div>${button(icon('radio') + ' 发布全体通知', 'new-notice', '', 'secondary', !connected)}</div>`}</section><aside class="chat-side" aria-label="群成员和资料"><div class="chat-side-heading"><h3>${t.is_historical ? '历史参与者' : '群成员'} <span>${members.length}</span></h3><button type="button" class="icon-button chat-members-toggle" data-action="toggle-chat-members" aria-label="关闭群成员">${icon('close')}</button></div><div class="chat-member-list">${members.map(m => {
      const p = data.profiles.find(profile => profile.id === m.profile_id);
      return `<a class="chat-member" href="#agents/${encodeURIComponent(m.profile_id)}">${agentAvatar(p || m, 'sm')}<span><strong>${escapeHTML(m.name)}</strong><small>${p ? escapeHTML(stateLabels[p.runtime?.state] || '未知状态') : '成员记录'}</small></span><span class="chat-member-dot${p && activeStates.has(p.runtime?.state) ? ' online' : ''}"></span></a>`;
    }).join('') || '<p class="blank-text">还没有受管成员加入</p>'}</div><div class="chat-about"><h3>群介绍</h3><p>${escapeHTML(t.description || '暂时没有群介绍。')}</p></div><details class="chat-technical" data-event-id="${technicalId}"${ui.expandedEvents.has(technicalId) ? ' open' : ''}><summary>话题资料</summary><dl><dt>Topic ID</dt><dd class="mono">${escapeHTML(t.topic_id)}</dd><dt>频道类型</dt><dd>${t.is_admin ? '管理员广播' : '成员交流'}</dd><dt>消息来源</dt><dd>发送事件与通知记录；多份接收记录保留在运行事件中。</dd></dl></details></aside></div>`;
  }
  function groupTraceEvents(events) {
    const runs = new Map();
    events.slice().sort((a, b) => dateNumber(a.ts) - dateNumber(b.ts)).forEach(event => {
      if (event.type === 'heartbeat') return;
      const runKey = JSON.stringify([event.profile_id || 'control', event.run_id || 'control']);
      if (!runs.has(runKey)) runs.set(runKey, { key: runKey, name: event.name || profileName(event.profile_id), profileId: event.profile_id, runId: event.run_id, events: [], spans: [], tools: new Map(), current: null, activeTool: null });
      const run = runs.get(runKey);
      run.events.push(event);
      if (event.type === 'model_started') {
        run.current = { key: event.id, kind: 'model', start: event, finish: null, children: [] };
        run.spans.push(run.current); run.activeTool = null;
      } else if (event.type === 'model_finished') {
        if (!run.current || run.current.kind !== 'model' || run.current.finish) {
          run.current = { key: event.id, kind: 'model', start: null, finish: event, children: [] };
          run.spans.push(run.current);
        } else run.current.finish = event;
      } else if (event.type === 'tool_started' || event.type === 'tool_finished') {
        const callKey = event.call_id || event.id;
        let tool = run.tools.get(callKey);
        if (!tool) {
          tool = { key: event.id, kind: 'tool', start: null, finish: null, children: [] };
          run.tools.set(callKey, tool);
          (run.current ? run.current.children : run.spans).push(tool);
        }
        if (event.type === 'tool_started') { tool.start = event; run.activeTool = tool; }
        else { tool.finish = event; if (run.activeTool === tool) run.activeTool = null; }
      } else {
        const row = { key: event.id, kind: 'event', event };
        (run.activeTool ? run.activeTool.children : run.current ? run.current.children : run.spans).push(row);
      }
    });
    return [...runs.values()].sort((a, b) => dateNumber(b.events.at(-1).ts) - dateNumber(a.events.at(-1).ts));
  }
  function traceStatus(event, complete = true) {
    const status = event?.status;
    if (status === 'error' || event?.error || ['error', 'control_error', 'topic_delete_failed'].includes(event?.type)) return badge('异常', 'red');
    if (status === 'interrupted') return badge('被新消息打断', 'amber');
    if (status === 'stopped') return badge('已停止');
    return complete ? badge('完成', 'green') : badge('未见结束事件', 'amber');
  }
  function tracePayload(event) {
    const value = event?.result || event?.output || event?.payload || event?.summary || event?.message || event?.error;
    return typeof value === 'string' ? value : value ? JSON.stringify(value, null, 2) : '';
  }
  function renderTraceSpan(span, depth = 0) {
    const event = span.event || span.finish || span.start;
    const id = `trace-span-${span.key}`;
    const labels = { ready: '进程就绪', state: `状态 · ${stateLabels[event.state] || event.state || '变化'}`, message_sent: '发送消息', message_received: '收到消息', topic_joined: '加入话题', topic_left: '退出话题', topic_deleted: '同步话题删除', topic_delete_requested: '删除话题', profile_deleted: '删除 Agent 配置', admin_joined: '加入管理员频道', notice_received: '收到管理员通知', notice_applied: '通知进入上下文', worker_exit: '进程退出', error: '执行异常', control_error: '控制通道异常' };
    const title = span.kind === 'model' ? event.model || '模型调用' : span.kind === 'tool' ? event.tool_name || span.start?.tool_name || '工具调用' : labels[event.type] || event.type;
    const category = span.kind === 'model' ? 'MODEL' : span.kind === 'tool' ? 'TOOL' : event.type?.startsWith('message') ? 'MESSAGE' : 'EVENT';
    const symbol = span.kind === 'model' ? 'agents' : span.kind === 'tool' ? 'terminal' : category === 'MESSAGE' ? 'send' : 'trace';
    const topic = data.topics.find(t => t.topic_id === event.topic_id);
    const payload = tracePayload(event);
    const rawId = `${id}-raw`;
    const children = span.children || [];
    return `<details class="trace-span trace-kind-${span.kind}" data-event-id="${attr(id)}"${ui.expandedEvents.has(id) ? ' open' : ''}><summary><span class="trace-span-symbol">${icon(symbol)}</span><span class="trace-span-label"><small>${category}</small><strong>${escapeHTML(title)}</strong></span><span class="trace-span-meta">${span.kind !== 'event' ? traceStatus(event, !!span.finish) : event.error ? badge('异常', 'red') : ''}${Number.isFinite(event.duration_ms) ? `<span>${(event.duration_ms / 1000).toFixed(2)} s</span>` : ''}${children.length ? `<span>${children.length} 个子项</span>` : ''}<time>${escapeHTML(timestamp((span.start || event).ts))}</time></span></summary><div class="trace-span-body">${span.kind !== 'event' && !span.start ? '<p class="trace-partial">起始事件不在当前采集窗口内。</p>' : ''}${topic ? `<div class="trace-topic-link">${link(`# ${topic.name}`, 'topics', topic.topic_id)}</div>` : ''}${payload ? `<pre class="trace-readable-output">${escapeHTML(payload)}</pre>${event.truncated || event.preview_truncated ? '<p class="trace-partial">此处为诊断摘要；消息全文请进入对应 Topic 查看。</p>' : ''}` : ''}${children.length ? `<div class="trace-children">${children.map(child => renderTraceSpan(child, depth + 1)).join('')}</div>` : ''}<details class="trace-raw" data-event-id="${attr(rawId)}"${ui.expandedEvents.has(rawId) ? ' open' : ''}><summary>原始事件${span.start && span.finish ? ' · 起止记录' : ''}</summary><pre>${escapeHTML(JSON.stringify(safeDetails(span.kind === 'event' ? event : { start: span.start, finish: span.finish }), null, 2))}</pre></details></div></details>`;
  }
  function renderTrace() {
    const events = data.events.filter(e => !ui.traceProfile || e.profile_id === ui.traceProfile);
    const runs = groupTraceEvents(events);
    ui.traceVisited ||= new Set();
    if (runs.length && !ui.traceVisited.has(ui.traceProfile)) {
      ui.traceVisited.add(ui.traceProfile);
      const first = runs.find(run => run.spans.some(span => span.kind === 'model')) || runs[0];
      ui.expandedEvents.add(`trace-run-${first.key}`);
      const latest = first.spans.filter(span => span.kind === 'model').at(-1);
      if (latest) ui.expandedEvents.add(`trace-span-${latest.key}`);
    }
    const people = new Map(data.profiles.map(p => [p.id, p.name]));
    data.events.forEach(event => { if (event.profile_id && !people.has(event.profile_id)) people.set(event.profile_id, `${event.name || event.profile_id.slice(0, 8)} · 历史`); });
    const modelCount = events.filter(e => e.type === 'model_started').length;
    const toolCount = events.filter(e => e.type === 'tool_started').length;
    const messageCount = events.filter(e => ['message_sent', 'message_received'].includes(e.type)).length;
    return `<div class="trace-page-intro"><div><span class="eyebrow">EXECUTION TRACES</span><h2>把每次执行，展开来看</h2><p>Agent → 运行批次 → 模型调用 → 工具与消息</p></div><label class="trace-filter" for="trace-agent-filter">筛选 Agent<select id="trace-agent-filter"><option value="">全部 Agent</option>${[...people].map(([id, name]) => `<option value="${attr(id)}"${id === ui.traceProfile ? ' selected' : ''}>${escapeHTML(name)}</option>`).join('')}</select></label></div><div class="trace-summary-strip"><span><strong>${runs.length}</strong>运行批次</span><span><strong>${modelCount}</strong>模型调用</span><span><strong>${toolCount}</strong>工具调用</span><span><strong>${messageCount}</strong>消息收发</span></div><div class="trace-run-list">${runs.map(run => {
      const id = `trace-run-${run.key}`, opened = ui.expandedEvents.has(id), profile = data.profiles.find(p => p.id === run.profileId);
      const current = profile?.runtime?.run_id === run.runId && runningStates.has(profile?.runtime?.state);
      return `<details class="panel trace-run" data-event-id="${attr(id)}"${opened ? ' open' : ''}><summary>${agentAvatar(run.name, 'sm')}<span class="trace-run-title"><strong>${escapeHTML(run.name)}</strong><small>${run.runId ? `Run ${escapeHTML(run.runId.slice(0, 8))}` : '控制台操作'} · ${escapeHTML(timestamp(run.events[0].ts, true))}</small></span><span class="trace-run-count">${run.events.length} 条事件</span>${current ? stateBadge(profile) : badge('已采集')}</summary><div class="trace-run-content">${run.spans.map(span => renderTraceSpan(span)).join('')}</div></details>`;
    }).join('') || empty('还没有执行记录', 'Agent 启动后，这里将展示模型调用、工具执行和通信过程。')}</div><p class="page-note">此视图按实际 Run ID 分组、按工具 call_id 合并起止事件。展示最近 ${data.events.length} 条全局事件中的 ${events.length} 条；窗口外的起止事件会明确标注。Topic 聊天历史独立完整保存。</p>`;
  }
  function receiptStats(notice) {
    const targets = notice.targets || [];
    const receipts = notice.receipts || {};
    return { total: targets.length, received: targets.filter(t => ['received', 'applied'].includes(receipts[t.profile_id]?.status)).length, applied: targets.filter(t => receipts[t.profile_id]?.status === 'applied').length };
  }
  function renderNotices() {
    if (ui.id) return renderNoticeDetail();
    const topic = data.admin.topic;
    const eligible = data.profiles.filter(p => activeStates.has(p.runtime?.state));
    const joined = eligible.filter(p => p.runtime?.admin_joined);
    const setup = `<div class="panel"><div class="panel-header"><div><h2>${topic ? `# ${escapeHTML(topic.name)}` : '建立管理员 Topic'}</h2><div class="cell-sub">${topic ? `${joined.length} / ${eligible.length} 个运行中的 Agent 已加入` : '管理员通知通过专用 Topic 发送'}</div></div><div class="pills">${topic ? button(icon('agents') + ' 邀请全体运行成员', 'invite-all', '', 'secondary', isBusy('invite-all') || !eligible.length) : button(icon('plus') + ' 创建 Topic', 'new-topic', '', 'primary', !connected)}</div></div><div class="panel-body"><p class="muted">${topic ? escapeHTML(topic.description || '管理员用于统一同步工作意图的 Topic。') : '先创建管理员 Topic，再邀请运行中的受管 Agent 加入，即可发送通知。'}</p>${topic ? `<div class="cell-sub mono">${escapeHTML(topic.topic_id)}</div>` : ''}</div></div>`;
    if (!data.notices.length) return setup + `<div class="spaced">${empty('还没有通知', '发送时固定目标成员快照，并分别记录运行时接收与安全检查点应用状态。', button(icon('send') + ' 创建全体通知', 'new-notice', '', 'primary', !topic || !connected))}</div>`;
    const rows = data.notices.slice().sort((a, b) => dateNumber(b.created_at) - dateNumber(a.created_at)).map(n => {
      const stats = receiptStats(n);
      const title = n.content?.length > 65 ? n.content.slice(0, 65) + '…' : n.content;
      return `<tr>${cell('通知内容', link(title || '空通知', 'notices', n.id) + `<div class="cell-sub">${escapeHTML(timestamp(n.created_at, true))} · ${escapeHTML(n.id)}</div>`)}${cell('目标快照', `${stats.total} 个 Agent`)}${cell('回执', `${stats.received} 已接收 / ${stats.applied} 已应用`)}${cell('状态', n.publish_error ? badge('发布错误', 'red') : stats.total && stats.applied === stats.total ? badge('全部应用', 'green') : badge('等待回执', 'amber'))}${cell('操作', `<div class="notice-row-actions">${link('查看 →', 'notices', n.id)}${button(icon('trash') + ' 删除记录', 'delete-notice', n.id, 'danger', isBusy(`notice:${n.id}`))}</div>`)}</tr>`;
    }).join('');
    return setup + `<div class="section-toolbar spaced"><strong>通知历史</strong><small>${data.notices.length} 条</small></div>` + table(['通知', '目标快照', '回执', '状态', '操作'], rows);
  }
  function renderNoticeDetail() {
    const notice = data.notices.find(n => n.id === ui.id);
    if (!notice) return empty('没有找到这条通知', '返回通知中心查看历史记录。', link('返回通知中心', 'notices', '', 'button secondary'));
    const stats = receiptStats(notice);
    const rows = (notice.targets || []).map(target => {
      const receipt = notice.receipts?.[target.profile_id] || { status: 'pending' };
      const received = ['received', 'applied'].includes(receipt.status);
      const applied = receipt.status === 'applied';
      return `<tr>${cell('Agent', link(target.name, 'agents', target.profile_id))}${cell('运行主机', escapeHTML(hostName(target.host_id)))}${cell('接收', `<span class="${received ? 'state-ok' : 'state-wait'}">${received ? '已接收' : '待确认'}</span>${receipt.received_at ? `<div class="cell-sub">${escapeHTML(timestamp(receipt.received_at))}</div>` : ''}`)}${cell('应用', `<span class="${applied ? 'state-ok' : 'state-wait'}">${applied ? '已应用' : '待检查点'}</span>${receipt.applied_at ? `<div class="cell-sub">${escapeHTML(timestamp(receipt.applied_at))}</div>` : ''}`)}</tr>`;
    }).join('');
    return `${back('notices', '返回通知历史')}<div class="section-toolbar"><div><h2>通知详情</h2>${badge(notice.id)}</div><div class="notice-row-actions">${button(icon('refresh') + ' 重试未确认', 'retry-notice', notice.id, 'secondary', isBusy(`notice:${notice.id}`) || (stats.total > 0 && stats.applied === stats.total && !notice.publish_error))}${button(icon('trash') + ' 删除记录', 'delete-notice', notice.id, 'danger', isBusy(`notice:${notice.id}`))}</div></div><div class="panel"><div class="notice-content">${escapeHTML(notice.content)}</div><div class="receipt-summary"><span><strong>${stats.total}</strong>保留目标</span><span><strong>${stats.received}</strong>已接收</span><span><strong>${stats.applied}</strong>已应用</span><span>创建于 ${escapeHTML(timestamp(notice.created_at, true))}</span></div></div>${notice.publish_error ? `<div class="spaced">${errorNote(notice.publish_error)}</div>` : ''}<p class="page-note">发布尝试：${escapeHTML(typeof notice.attempts === 'number' ? notice.attempts : (notice.attempts?.length ?? '—'))} 次 · Topic ${escapeHTML(notice.topic_id || '—')}</p><div class="section-toolbar spaced"><strong>逐 Agent 回执</strong><small>发送时固定成员；删除 Agent 会移除其关联</small></div>${rows ? table(['Agent', '运行主机', '已接收', '已应用'], rows) : empty('本次通知没有目标成员', '先启动并邀请 Agent，再创建新的全体通知。', '', true)}<div class="spaced">${infoNote('“已接收”表示运行时已收信；“已应用”表示已在安全检查点纳入执行上下文。发布成功不等于每个成员已经接收。')}</div>`;
  }
  function checkSummary(check) {
    if (!check) return '<span class="host-check">尚未检查</span>';
    const result = typeof check === 'object' ? check : { message: String(check) };
    const ok = result.ok ?? result.success;
    return `<span class="host-check ${ok === true ? 'success' : ok === false ? 'failure' : ''}">${escapeHTML(result.message || result.error || (ok === true ? '连接正常' : ok === false ? '连接失败' : result.status || '已检查'))}</span>${result.ts || result.checked_at ? `<div class="cell-sub">${escapeHTML(timestamp(result.ts || result.checked_at, true))}</div>` : ''}`;
  }
  function renderProviders() {
    const providers = data.model_providers || [];
    const intro = `<div class="provider-intro panel"><span class="provider-symbol">${icon('server')}</span><div><h2>一个接口，连接不同模型</h2><p>填写供应商、Base URL 和模型名称，再为 Agent 选择配置。支持多个供应商并行使用。</p></div>${badge('OpenAI 兼容', 'blue')}</div>`;
    if (!providers.length) return intro + empty('添加第一个模型配置', '可连接云端供应商、代理网关或本地兼容服务。', button(icon('plus') + ' 添加模型配置', 'new-provider', '', 'primary'));
    return intro + `<div class="provider-grid">${providers.map(provider => {
      const profiles = data.profiles.filter(p => p.provider_id === provider.id);
      return `<article class="panel provider-card"><div class="provider-card-heading"><span class="provider-symbol">${icon('server')}</span><div><h2>${escapeHTML(provider.name)}</h2><small>${provider.api_mode === 'deepseek' ? 'OpenAI 接口 · DeepSeek 扩展' : 'OpenAI 兼容接口'}</small></div>${badge(provider.credentials_configured ? 'Key 已设置' : '待设置 Key', provider.credentials_configured ? 'green' : 'amber')}</div><div class="provider-model"><span>模型</span><strong>${escapeHTML(provider.model)}</strong></div><dl class="provider-facts"><dt>Base URL</dt><dd>${escapeHTML(provider.base_url)}</dd><dt>使用此配置</dt><dd>${profiles.length ? profiles.map(p => link(p.name, 'agents', p.id)).join('、') : '尚未关联 Agent'}</dd></dl><div class="provider-card-actions">${button(icon('edit') + (provider.credentials_configured ? ' 编辑配置' : ' 配置 / 填写 Key'), 'edit-provider', provider.id, provider.credentials_configured ? 'secondary' : 'primary', !connected)}${button(icon('trash') + ' 删除', 'delete-provider', provider.id, 'ghost', !connected || !!profiles.length)}</div></article>`;
    }).join('')}</div><p class="page-note">API Key 按配置独立保存在服务端内存中，不回显、不写入浏览器存储；控制服务重启后需要重新填写。被 Agent 使用的配置需先解除关联才能删除。</p>`;
  }
  function renderHosts() {
    if (!data.hosts.length) return empty('尚未读取到运行主机', '本地主机由控制服务提供。也可以添加已配置 SSH 登录的远程主机。', button(icon('plus') + ' 添加 SSH 主机', 'new-host', '', 'primary'));
    const rows = data.hosts.map(host => `<tr>${cell('主机', `<strong>${escapeHTML(host.name)}</strong><div class="cell-sub">${host.kind === 'ssh' ? `${escapeHTML(host.ssh_target)} · SSH ${escapeHTML(host.port || 22)}` : '控制服务所在机器'}</div>`)}${cell('类型', badge(host.kind === 'ssh' ? '远程 SSH' : '本地', host.kind === 'ssh' ? 'blue' : ''))}${cell('运行环境', `<div class="mono cell-description">${escapeHTML(host.repo_path || '—')}</div><div class="cell-sub mono">Python: ${escapeHTML(host.python_path || '—')}</div><div class="cell-sub mono">工作区: ${escapeHTML(host.workspace_root || '—')}</div><div class="cell-sub mono">Comm: ${escapeHTML(host.comm_host || '自动选择')}</div>`)}${cell('最近检查', checkSummary(host.last_check))}${cell('操作', button(icon('refresh') + (busy.has(`host:${host.id}`) ? '检查中' : '检查连接'), 'check-host', host.id, 'secondary', isBusy(`host:${host.id}`)))}</tr>`).join('');
    return table(['主机', '类型', '运行环境', '最近检查', '操作'], rows) + `<div class="spaced">${infoNote('SSH 使用本机已有的配置和密钥，远程机器位于同一局域网。连接检查验证 SSH、Python 与 worker 协议；成功不代表 Agent 已加入 Topic。')}</div>`;
  }
  function drawTopology() { window.AgentTopology?.draw(document); }
  function rememberChatViewport() {
    const stream = $('#chat-stream');
    if (!stream) return;
    const bounds = stream.getBoundingClientRect();
    const nodes = [...stream.querySelectorAll('[data-message-key]')];
    const first = nodes.find(node => node.getBoundingClientRect().bottom > bounds.top);
    const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 64;
    const previous = ui.chatScroll.get(stream.dataset.scrollKey);
    ui.chatScroll.set(stream.dataset.scrollKey, {
      top: stream.scrollTop, atBottom, anchor: first?.dataset.messageKey,
      offset: first ? first.getBoundingClientRect().top - bounds.top : 0,
      maxSeq: Math.max(0, ...nodes.map(node => Number(node.dataset.messageSeq) || 0)),
      keys: new Set(nodes.map(node => node.dataset.messageKey)), unseen: atBottom ? 0 : (previous?.unseen || 0),
    });
    if (ui.chatScroll.size > 40) ui.chatScroll.delete(ui.chatScroll.keys().next().value);
  }
  function restoreChatViewport() {
    const stream = $('#chat-stream');
    if (!stream) return;
    const previous = ui.chatScroll.get(stream.dataset.scrollKey);
    if (!previous || previous.atBottom) stream.scrollTop = stream.scrollHeight;
    else {
      const nodes = [...stream.querySelectorAll('[data-message-key]')];
      const anchor = nodes.find(node => node.dataset.messageKey === previous.anchor);
      stream.scrollTop = anchor ? anchor.getBoundingClientRect().top - stream.getBoundingClientRect().top - previous.offset : previous.top;
      previous.unseen += nodes.filter(node => !previous.keys.has(node.dataset.messageKey) && (Number(node.dataset.messageSeq) || 0) > previous.maxSeq).length;
      previous.keys = new Set(nodes.map(node => node.dataset.messageKey));
    }
    updateChatJump();
  }
  function updateChatJump() {
    const stream = $('#chat-stream'), jump = $('[data-action="chat-latest"]');
    if (!stream || !jump) return;
    const bottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 64;
    const saved = ui.chatScroll.get(stream.dataset.scrollKey);
    if (bottom && saved) saved.unseen = 0;
    jump.hidden = bottom;
    jump.innerHTML = icon('arrow') + (saved?.unseen ? `${saved.unseen} 条新消息` : '回到最新消息');
  }
  function renderPage(fromPoll = false) {
    updateChrome();
    if (!data) return;
    const focused = document.activeElement;
    if (fromPoll && $('#page-content').contains(focused) && focused.matches('textarea,select,input:not([type=checkbox]):not([type=radio])')) return;
    rememberChatViewport();
    const focusIdentity = $('#main').contains(focused) ? { control: ['INPUT', 'TEXTAREA'].includes(focused.tagName) ? focused.id : null, start: focused.selectionStart, end: focused.selectionEnd, action: focused.dataset?.action, id: focused.dataset?.id, href: focused.getAttribute?.('href'), eventId: focused.tagName === 'SUMMARY' ? focused.parentElement.dataset.eventId : null } : null;
    const heading = ui.id && ui.page === 'agents' ? 'Agent 详情' : ui.id && ui.page === 'topics' ? 'Topic 详情' : ui.id && ui.page === 'notices' ? '通知详情' : pageNames[ui.page];
    $('#page-title').textContent = heading;
    $('#page-description').textContent = descriptions[ui.page];
    $('#breadcrumb').textContent = `工作区 / ${pageNames[ui.page]}${ui.id ? ' / 详情' : ''}`;
    const actions = {
      overview: button(icon('plus') + ' 创建 Agent', 'new-profile', '', 'secondary', !connected),
      agents: ui.id ? '' : button(icon('plus') + ' 创建 Agent', 'new-profile', '', 'primary', !connected),
      topics: data.admin.topic ? '' : button(icon('plus') + ' 创建管理员 Topic', 'new-topic', '', 'secondary', !connected),
      trace: '',
      notices: ui.id ? '' : button(icon('send') + ' 新建通知', 'new-notice', '', 'primary', !connected),
      models: button(icon('plus') + ' 添加模型配置', 'new-provider', '', 'primary', !connected),
      hosts: button(icon('plus') + ' 添加 SSH 主机', 'new-host', '', 'primary', !connected),
    };
    $('#page-actions').innerHTML = actions[ui.page];
    $('#page-content').innerHTML = renderCleanupStatus() + ({ overview: renderOverview, agents: renderAgents, topics: renderTopics, trace: renderTrace, notices: renderNotices, hosts: renderHosts, models: renderProviders })[ui.page]();
    $('#main').classList.toggle('is-chat-page', ui.page === 'topics' && !!ui.id);
    const selectAll = $('#select-all-agents');
    if (selectAll) selectAll.indeterminate = ui.selectedAgents.size > 0 && ui.selectedAgents.size < data.profiles.filter(canStartProfile).length;
    if (focusIdentity?.control) {
      const input = document.getElementById(focusIdentity.control);
      input?.focus({ preventScroll: true });
      if (input && focusIdentity.start !== null && ['text', 'search', 'textarea'].includes(input.type)) input.setSelectionRange(focusIdentity.start, focusIdentity.end);
    }
    if (focusIdentity && (focusIdentity.action || focusIdentity.href || focusIdentity.eventId)) {
      const matching = [...$('#main').querySelectorAll('button,a,summary')].find(element => focusIdentity.eventId ? element.tagName === 'SUMMARY' && element.parentElement.dataset.eventId === focusIdentity.eventId : focusIdentity.action ? element.dataset.action === focusIdentity.action && element.dataset.id === focusIdentity.id : element.getAttribute('href') === focusIdentity.href);
      matching?.focus({ preventScroll: true });
    }
    requestAnimationFrame(() => { drawTopology(); restoreChatViewport(); });
  }

  function field(label, name, value = '', { type = 'text', required = false, full = false, hint = '', placeholder = '', textarea = false, rows = 3, options = null, autocomplete = 'off' } = {}) {
    const attrs = `name="${attr(name)}" id="form-${attr(name)}"${required ? ' required' : ''}`;
    const control = options ? `<select ${attrs}>${options.map(option => `<option value="${attr(option.value)}"${String(option.value) === String(value) ? ' selected' : ''}>${escapeHTML(option.label)}</option>`).join('')}</select>` : textarea ? `<textarea ${attrs} rows="${rows}" placeholder="${attr(placeholder)}">${escapeHTML(value)}</textarea>` : `<input ${attrs} type="${type}" value="${attr(value)}" placeholder="${attr(placeholder)}" autocomplete="${autocomplete}"${type === 'password' ? ' spellcheck="false"' : ''}>`;
    return `<label class="field${full ? ' full' : ''}" for="form-${attr(name)}"><span>${escapeHTML(label)}${required ? ' <span class="required">*</span>' : ''}</span>${control}${hint ? `<small>${escapeHTML(hint)}</small>` : ''}</label>`;
  }
  function checkbox(label, name, checked) { return `<label class="checkbox-field"><input type="checkbox" name="${attr(name)}"${checked ? ' checked' : ''}>${escapeHTML(label)}</label>`; }
  function openForm(kind, title, body, submitLabel, id = null, intro = '') {
    ui.formKind = kind;
    ui.editingId = id;
    $('#dialog-title').textContent = title;
    $('#dialog-kicker').textContent = kind === 'credentials' ? 'SESSION CREDENTIALS' : 'WORKSPACE CONFIGURATION';
    $('#dialog-content').innerHTML = `<form id="editor-form"><div class="dialog-body">${intro ? `<div class="notification-description">${intro}</div>` : ''}<div class="form-grid">${body}</div><div class="form-error" id="form-error" role="alert" hidden></div></div><div class="dialog-footer"><button type="button" class="button secondary" data-action="close-dialog">取消</button><button type="submit" class="button primary" id="form-submit">${escapeHTML(submitLabel)}</button></div></form>`;
    if (!dialog.open) dialog.showModal();
    const first = dialog.querySelector('input,textarea,select');
    if (first) first.focus();
  }
  function openProvider(id = null) {
    const provider = (data?.model_providers || []).find(item => item.id === id);
    if (id && !provider) return toast('模型配置已不存在，请刷新。', true);
    const body = field('供应商 / 配置名称', 'name', provider?.name || '', { required: true, full: true, placeholder: '例如：团队模型网关' })
      + field('Base URL', 'base_url', provider?.base_url || '', { type: 'url', required: true, full: true, placeholder: 'https://your-provider.example/v1', hint: '填写 API 根地址（保留所需 /v1），不要填写 /chat/completions。更换地址后需重新填写 Key。' })
      + field('模型名称', 'model', provider?.model || '', { required: true, full: true, placeholder: '填写供应商提供的完整模型 ID' })
      + field('API Key', 'api_key', '', { type: 'password', full: true, autocomplete: 'new-password', placeholder: provider?.credentials_configured ? '已设置；留空保持原 Key' : '填写该配置的 API Key', hint: '仅保存在当前服务会话中。本地无鉴权服务可填写其要求的占位值。' })
      + (provider?.credentials_configured ? checkbox('清除此配置当前保存的 API Key', 'clear_api_key', false) : '')
      + `<details class="field full provider-advanced"><summary>兼容选项</summary>${field('请求参数', 'api_mode', provider?.api_mode || 'openai', { options: [{ value: 'openai', label: '标准 OpenAI 兼容参数' }, { value: 'deepseek', label: 'DeepSeek 扩展（thinking / user_id）' }], hint: '默认使用标准参数；只有支持这些扩展的接口才选择 DeepSeek 模式。' })}</details>`;
    openForm('provider', provider ? '编辑模型配置' : '添加模型配置', body, '保存配置', id);
  }
  function openDeleteProvider(id) {
    const provider = (data?.model_providers || []).find(item => item.id === id);
    if (!provider) return;
    if (data.profiles.some(p => p.provider_id === id)) return toast('请先为使用此配置的 Agent 选择其他模型配置。', true);
    openForm('delete-provider', '删除模型配置', `<div class="field full"><p>删除 <strong>${escapeHTML(provider.name)}</strong> 及其会话凭据？</p></div>`, '确认删除', id);
    $('#form-submit').className = 'button danger';
  }
  function syncProviderOptions() {
    const selected = $('#form-provider_id');
    const input = dialog.querySelector('input[name="thinking"]');
    if (!selected || !input) return;
    const deepseek = (data.model_providers || []).find(p => p.id === selected.value)?.api_mode === 'deepseek';
    input.disabled = !deepseek;
    if (!deepseek) input.checked = false;
    $('#provider-thinking').classList.toggle('provider-option-disabled', !deepseek);
  }
  function openProfile(id = null) {
    if (!data) return toast('请等待控制服务连接。', true);
    const p = data.profiles.find(profile => profile.id === id);
    if (p?.runtime?.cleanup_pending) return toast('这个 Agent 正在清理关联数据，请等待清理完成。', true);
    if (p && runningStates.has(p.runtime?.state)) return toast('请先停止 Agent，再编辑配置。', true);
    if (!(data.model_providers || []).length) { toast('请先添加一个模型配置，再创建 Agent。'); return openProvider(); }
    const hosts = data.hosts.map(host => ({ value: host.id, label: `${host.name} · ${host.kind === 'ssh' ? 'SSH' : '本地'}` }));
    if (!hosts.length) return toast('没有可用的运行主机，请先检查控制服务配置。', true);
    const body = field('Agent 名称', 'name', p?.name || '', { required: true, placeholder: '例如：alice' }) + field('运行主机', 'host_id', p?.host_id || hosts[0].value, { options: hosts, required: true, hint: p ? '更换运行主机不会迁移已有历史或记忆。' : '' }) + field('人设 Persona', 'persona', p?.persona || '', { full: true, textarea: true, required: true, placeholder: '角色、职责、工作风格和长期边界', hint: '人设作为 Agent 的持续行为配置，与本次启动任务分开。' }) + field('启动任务 Initial prompt', 'initial_prompt', p?.initial_prompt || '完成当前任务后等待 topic 消息。', { full: true, textarea: true, required: true, placeholder: '本次启动后先做什么，以及完成后的协作要求' }) + '<div class="field-group-title">模型与记忆</div>' + field('模型配置', 'provider_id', p?.provider_id || data.model_providers[0].id, { full: true, required: true, options: data.model_providers.map(provider => ({ value: provider.id, label: `${provider.name} · ${provider.model}${provider.credentials_configured ? '' : ' · 待设置 Key'}` })), hint: '在“模型配置”页面统一维护供应商、地址和凭据。更换配置在下次启动时生效。' }) + checkbox('启用长期记忆', 'enable_memory', p ? !!p.enable_memory : true) + `<div class="field full" id="provider-thinking">${checkbox('启用 DeepSeek 思考模式', 'thinking', !!p?.thinking)}<small>标准兼容接口使用模型默认的思考策略。</small></div>`;
    openForm('profile', p ? `编辑 ${p.name}` : '创建 Agent', body, p ? '保存配置' : '创建 Agent', id);
    syncProviderOptions();
  }
  function openDeleteProfile(id) {
    const profile = data?.profiles.find(item => item.id === id);
    if (!profile) return toast('这个 Agent 配置已不存在，请刷新列表。', true);
    if (runningStates.has(profile.runtime?.state) || profile.runtime?.pid) return toast('请先停止 Agent，再删除配置。', true);
    const body = `<div class="field full"><p>删除 <strong>${escapeHTML(profile.name)}</strong> 及其关联数据？</p></div><div class="field full">${infoNote('将清理人设配置、本机工作目录（历史、记忆、定时任务和生成文件）、运行日志、该 Agent 的聊天与执行记录，以及通知目标和回执。此操作不可恢复。', true)}</div><div class="field full"><small>共享 Topic 和其他成员的记录保留。离线也会清理；运行中的 Agent 必须先停止。远程工作目录无法确认清理时，会明确提示原因。</small></div>`;
    openForm('delete-profile', '删除 Agent 及关联数据', body, '确认删除', id);
    $('#form-submit').className = 'button danger';
  }
  function openDeleteTopic(id) {
    const topic = data?.topics.find(item => item.topic_id === id);
    if (!topic) return toast('这个 Topic 已不存在，请刷新列表。', true);
    if (!canDeleteTopic(topic)) return toast(topic.delete_reason || '管理员频道用于全体通知，暂不支持删除。', true);
    const body = `<div class="field full"><p>删除 <strong># ${escapeHTML(topic.name)}</strong> 及其关联数据？平台管理的 Agent 会退出，之后也不能重新加入这个话题 ID。</p></div><div class="field full">${infoNote('将清理平台保存的该话题聊天、执行记录、成员关系，以及关联通知和回执。此操作不可恢复。', true)}</div><div class="field full"><small>运行中的成员会退出并丢弃未读消息；离线成员下次启动前同步。保留 Agent 人设、已形成的记忆和其他话题。独立运行、未受平台管理的节点不受此操作控制。</small></div>`;
    openForm('delete-topic', '删除 Topic', body, '确认删除话题', id);
    $('#form-submit').className = 'button danger';
  }
  function openDeleteNotice(id) {
    const notice = data?.notices.find(item => item.id === id);
    if (!notice) return toast('这条通知记录已不存在。', true);
    const body = `<div class="field full"><p>删除这条通知的中心记录和回执？</p><blockquote class="notice-delete-preview">${escapeHTML(notice.content)}</blockquote></div><div class="field full">${infoNote('这不是撤回：Agent 已收到的内容、执行结果和 Topic 聊天历史仍保留。')}</div><div class="field full"><small>删除后停止此记录的后续自动补发，已经在传输或排队中的通知仍可能送达。</small></div>`;
    openForm('delete-notice', '仅删除通知记录', body, '删除记录', id);
    $('#form-submit').className = 'button danger';
  }
  function openHost() {
    const body = field('主机名称', 'name', '', { required: true, placeholder: '例如：GPU Worker' }) + field('SSH 目标', 'ssh_target', '', { required: true, placeholder: 'user@host 或 SSH 配置别名' }) + field('SSH 端口', 'port', '22', { type: 'number', required: true }) + field('Comm 地址（可选）', 'comm_host', '', { placeholder: '自动选择局域网网卡', hint: '默认自动选择远程主机的局域网网卡；多网卡时可指定可达 IPv4。' }) + field('远程仓库路径', 'repo_path', '', { full: true, required: true, placeholder: '/srv/agent-group', hint: '远程机器上已部署此仓库的绝对路径。' }) + field('远程 Python 路径', 'python_path', '', { full: true, required: true, placeholder: '/srv/agent-group/.venv/bin/python' }) + field('远程工作区根目录', 'workspace_root', '', { full: true, required: true, placeholder: '/srv/agent-workspaces' });
    openForm('host', '添加局域网 SSH 主机', body, '添加主机', null, '添加同一局域网内的机器，使用本机已有的 SSH 配置与密钥。SSH 检查成功不代表 Agent 已加入 Topic，加入状态在 Agent 详情中查看。');
  }
  function openCredentials() { location.hash = '#models'; }
  function openTopic() {
    if (data?.admin.topic) { location.hash = '#notices'; return toast('管理员 Topic 已存在，可直接邀请成员。'); }
    openForm('topic', '创建管理员 Topic', field('Topic 名称', 'name', 'admin-announcements', { full: true, required: true }) + field('用途说明', 'description', '管理员向受管 Agent 同步任务意图、协作要求与工作进展。', { full: true, textarea: true }), '创建管理员 Topic');
  }
  function openNotice() {
    if (!data) return toast('请等待控制服务连接。', true);
    if (!data.admin.topic) { location.hash = '#notices'; return openTopic(); }
    const members = data.profiles;
    const running = members.filter(p => activeStates.has(p.runtime?.state));
    const intro = `<strong># ${escapeHTML(data.admin.topic.name)}</strong><div class="target-list">${members.map(p => badge(`${p.name} · ${stateLabels[p.runtime?.state] || '已停止'}`)).join('') || '<span>尚无受管 Agent</span>'}</div><p class="page-note">当前 ${members.length} 个受管成员，${running.length} 个运行中。实际目标快照以服务端发送结果为准，后加入的 Agent 不自动纳入本次。</p>`;
    const body = field('通知内容', 'content', '', { full: true, textarea: true, rows: 6, required: true, placeholder: '向全体 Agent 同步本轮目标、优先级或协作要求…' }) + `<div class="field full">${infoNote('运行时收信后报告“已接收”；Agent 在下一个安全检查点纳入执行上下文后报告“已应用”。')}</div>`;
    openForm('notice', '新建全体通知', body, '发送通知', null, intro);
  }
  function closeDialog() {
    if (busy.has('form')) return;
    const keyInput = dialog.querySelector('input[name="api_key"]');
    if (keyInput) keyInput.value = '';
    dialog.close();
    $('#dialog-content').replaceChildren();
    ui.formKind = null;
    ui.editingId = null;
  }
  async function submitChatNotice(event) {
    event.preventDefault();
    const content = String(ui.chatDraft || '').trim();
    if (!content || isBusy('chat-notice') || !event.target.reportValidity()) return;
    try {
      await mutate('/api/notices', { content }, 'POST', 'chat-notice');
      ui.chatDraft = '';
      const stream = $('#chat-stream');
      if (stream) stream.scrollTop = stream.scrollHeight;
      renderPage();
      toast('公告已提交，消息下方会更新各成员的回执。');
    } catch (error) { toast(error.message, true); }
  }
  async function submitForm(event) {
    event.preventDefault();
    const form = event.target;
    if (!form.reportValidity() || busy.has('form')) return;
    const fields = new FormData(form);
    const value = name => String(fields.get(name) || '').trim();
    let path, payload, method = 'POST', success;
    const kind = ui.formKind;
    if (kind === 'credentials') { path = '/api/credentials'; payload = { api_key: value('api_key') }; success = 'API 凭据已保存在控制服务会话中。'; }
    if (kind === 'provider') {
      path = `/api/model-providers${ui.editingId ? `/${encodeURIComponent(ui.editingId)}` : ''}`; method = ui.editingId ? 'PUT' : 'POST';
      payload = { name: value('name'), base_url: value('base_url'), model: value('model'), api_mode: value('api_mode') || 'openai' };
      if (value('api_key')) payload.api_key = value('api_key');
      if (fields.has('clear_api_key')) payload.clear_api_key = true;
      if (payload.api_key && payload.clear_api_key) return showFormError('更换 Key 和清除 Key 请选择一项。');
      success = '模型配置已保存，将用于 Agent 下次启动。';
      const previous = (data.model_providers || []).find(provider => provider.id === ui.editingId);
      if (previous?.credentials_configured && previous.base_url.replace(/\/+$/, '') !== payload.base_url.replace(/\/+$/, '') && !payload.api_key) success = '接口地址已更新，原 API Key 已清除；请为新地址填写 Key。';
    }
    if (kind === 'delete-provider') { path = `/api/model-providers/${encodeURIComponent(ui.editingId)}`; method = 'DELETE'; payload = {}; success = '模型配置已删除。'; }
    if (kind === 'host') {
      const port = Number(value('port'));
      if (!Number.isInteger(port) || port < 1 || port > 65535) return showFormError('SSH 端口必须是 1–65535 的整数。');
      for (const name of ['repo_path', 'python_path', 'workspace_root']) if (!value(name).startsWith('/')) return showFormError('远程仓库、Python 和工作区都需要填写绝对路径。');
      path = '/api/hosts'; payload = { name: value('name'), kind: 'ssh', ssh_target: value('ssh_target'), port, repo_path: value('repo_path'), python_path: value('python_path'), workspace_root: value('workspace_root'), comm_host: value('comm_host') }; success = 'SSH 主机已添加，可继续检查连接。';
    }
    if (kind === 'profile') { path = `/api/profiles${ui.editingId ? `/${encodeURIComponent(ui.editingId)}` : ''}`; method = ui.editingId ? 'PUT' : 'POST'; payload = { name: value('name'), persona: value('persona'), initial_prompt: value('initial_prompt'), provider_id: value('provider_id'), host_id: value('host_id'), enable_memory: fields.has('enable_memory'), thinking: fields.has('thinking') }; success = ui.editingId ? 'Agent 配置已保存。' : 'Agent 配置已创建。'; }
    if (kind === 'delete-profile') { path = `/api/profiles/${encodeURIComponent(ui.editingId)}`; method = 'DELETE'; payload = {}; success = 'Agent 及其关联数据已删除。'; }
    if (kind === 'delete-topic') { path = `/api/topics/${encodeURIComponent(ui.editingId)}`; method = 'DELETE'; payload = {}; success = '话题及其关联记录已删除。'; }
    if (kind === 'topic') { path = '/api/admin/topic'; payload = { name: value('name'), description: value('description') }; success = '管理员 Topic 已创建。'; }
    if (kind === 'delete-notice') { path = `/api/notices/${encodeURIComponent(ui.editingId)}`; method = 'DELETE'; payload = {}; success = '通知记录已删除，已送达的内容不会撤回。'; }
    if (kind === 'notice') { path = '/api/notices'; payload = { content: value('content') }; success = '通知已提交，正在等待逐 Agent 回执。'; }
    if (!path) return;
    if ((kind === 'credentials' && !payload.api_key) || (kind === 'notice' && !payload.content)) return showFormError('请填写有效内容。');
    $('#form-error').hidden = true;
    form.querySelectorAll('button').forEach(element => { element.disabled = true; });
    const submit = $('#form-submit'), oldLabel = submit.textContent;
    submit.textContent = '处理中…';
    try {
      const result = await mutate(path, payload, method, 'form');
      if (kind === 'credentials' || kind === 'provider') { form.querySelector('[name="api_key"]').value = ''; payload.api_key = ''; }
      closeDialog();
      if (kind === 'provider' || kind === 'delete-provider') location.hash = '#models';
      if (kind === 'host') location.hash = '#hosts';
      if (kind === 'profile' && method === 'POST') location.hash = '#agents';
      if (kind === 'delete-profile') { if (ui.traceProfile === result?.deleted) ui.traceProfile = ''; location.hash = '#agents'; }
      if (kind === 'topic' || kind === 'delete-notice') location.hash = '#notices';
      if (kind === 'delete-topic') {
        location.hash = '#topics';
        if (result?.pending_profiles?.length) success = `话题关联记录已清理，正在等待 ${result.pending_profiles.length} 个 Agent 确认退出。`;
      }
      if (result?.cleanup_pending) success = `删除已登记，关联数据尚未清理完成，控制台会自动重试。${result.cleanup_error || ''}`;
      if (kind === 'notice') {
        const id = result?.id || result?.notice?.id;
        location.hash = id ? `#notices/${encodeURIComponent(id)}` : '#notices';
      }
      toast(success, !!result?.cleanup_pending);
    } catch (error) { showFormError(error.message); }
    finally { if (dialog.open) { form.querySelectorAll('button').forEach(element => { element.disabled = false; }); submit.textContent = oldLabel; } }
  }
  function showFormError(message) { const error = $('#form-error'); error.textContent = message; error.hidden = false; }
  async function handleAction(action, id) {
    if (action === 'chat-history-older') return refreshTopicHistory('older');
    if (action === 'chat-history-retry') return refreshTopicHistory(chatHistory().lastMode || 'newer');
    if (action === 'start-selected-agents') return startSelectedAgents();
    if (action === 'clear-agent-selection') { if (!ui.batch?.running) ui.selectedAgents.clear(); renderPage(); return; }
    if (action === 'dismiss-batch') { if (!ui.batch?.running) ui.batch = null; renderPage(); return; }
    if (action === 'select-failed-agents') {
      if (!ui.batch?.running) ui.selectedAgents = new Set((ui.batch?.items || []).filter(item => item.status === 'error' && canStartProfile(data.profiles.find(p => p.id === item.id))).map(item => item.id));
      renderPage(); return;
    }
    if (['start-profile', 'stop-profile', 'edit-profile', 'delete-profile'].includes(action) && busy.has(`profile:${id}`)) return;
    if (['start-profile', 'edit-profile'].includes(action) && data?.profiles.find(p => p.id === id)?.runtime?.cleanup_pending) return toast('这个 Agent 正在清理关联数据，请等待清理完成。', true);
    if (action === 'chat-messages' || action === 'chat-events') { ui.chatTab = action === 'chat-messages' ? 'messages' : 'events'; renderPage(); return; }
    if (action === 'toggle-chat-members') { ui.chatMembers = !ui.chatMembers; renderPage(); return; }
    if (action === 'chat-latest') { const stream = $('#chat-stream'); if (stream) stream.scrollTop = stream.scrollHeight; updateChatJump(); return; }
    if (action === 'credentials') return openCredentials();
    if (action === 'new-provider') return openProvider();
    if (action === 'edit-provider') return openProvider(id);
    if (action === 'delete-provider') return openDeleteProvider(id);
    if (action === 'close-dialog') return closeDialog();
    if (action === 'refresh') return loadState(true);
    if (action === 'new-profile') return openProfile();
    if (action === 'edit-profile') return openProfile(id);
    if (action === 'delete-profile') return openDeleteProfile(id);
    if (action === 'new-host') return openHost();
    if (action === 'new-topic') return openTopic();
    if (action === 'delete-topic') return openDeleteTopic(id);
    if (action === 'new-notice') return openNotice();
    if (action === 'delete-notice') return openDeleteNotice(id);
    if (action === 'go-hosts') { location.hash = '#hosts'; return; }
    if (action === 'view-agent') { location.hash = `#agents/${encodeURIComponent(id)}`; return; }
    if (action === 'view-topic') { location.hash = `#topics/${encodeURIComponent(id)}`; return; }
    if (action === 'trace-agent') { ui.traceProfile = id; location.hash = '#trace'; renderPage(); return; }
    try {
      if (action === 'start-profile') { const profile = data.profiles.find(p => p.id === id); if (!modelReady(profile)) { toast('请先为该 Agent 的模型配置填写 API Key。'); return profile?.provider_id ? openProvider(profile.provider_id) : openCredentials(); } await mutate(`/api/profiles/${encodeURIComponent(id)}/start`, {}, 'POST', `profile:${id}`); toast('启动请求已提交，状态会自动更新。'); }
      if (action === 'stop-profile') { await mutate(`/api/profiles/${encodeURIComponent(id)}/stop`, {}, 'POST', `profile:${id}`); toast('停止请求已提交。'); }
      if (action === 'check-host') { const result = await mutate(`/api/hosts/${encodeURIComponent(id)}/check`, {}, 'POST', `host:${id}`); const check = result?.last_check || result; toast(check?.message || check?.error || (check?.ok === false || check?.success === false ? '主机检查未通过，请查看检查结果。' : '主机检查已完成。'), check?.ok === false || check?.success === false); }
      if (action === 'invite-all') { await mutate('/api/admin/invite', {}, 'POST', 'invite-all'); toast('已请求邀请所有运行中的受管 Agent。'); }
      if (action === 'retry-notice') { await mutate(`/api/notices/${encodeURIComponent(id)}/retry`, {}, 'POST', `notice:${id}`); toast('已重试当前通知的未确认目标。'); }
    } catch (error) { toast(error.message, true); }
  }
  function route() {
    const oldRoute = `${ui.page}/${ui.id}`;
    const raw = location.hash.slice(1).split('/');
    ui.page = Object.hasOwn(pageNames, raw[0]) ? raw[0] : 'overview';
    try { ui.id = raw[1] ? decodeURIComponent(raw[1]) : null; } catch (_) { ui.id = null; }
    if (oldRoute !== `${ui.page}/${ui.id}`) { ui.chatQuery = ''; ui.chatTab = 'messages'; ui.chatMembers = false; }
    renderPage();
    if (ui.page === 'topics' && ui.id) refreshTopicHistory();
    $('#announcer').textContent = `已进入${pageNames[ui.page]}`;
  }
  document.addEventListener('click', event => {
    const element = event.target.closest('[data-action]');
    if (element && !element.disabled) handleAction(element.dataset.action, element.dataset.id);
  });
  document.addEventListener('submit', event => { if (event.target.id === 'editor-form') submitForm(event); if (event.target.id === 'chat-notice-form') submitChatNotice(event); });
  document.addEventListener('input', event => {
    if (event.target.id === 'chat-search') { ui.chatQuery = event.target.value; renderPage(); clearTimeout(chatSearchTimer); chatSearchTimer = setTimeout(() => refreshTopicHistory(), 250); }
    if (event.target.id === 'chat-notice-content') ui.chatDraft = event.target.value;
  });
  document.addEventListener('scroll', event => { if (event.target.id === 'chat-stream') updateChatJump(); }, true);
  document.addEventListener('change', event => {
    if (event.target.id === 'form-provider_id') syncProviderOptions();
    if (event.target.id === 'trace-agent-filter') { ui.traceProfile = event.target.value; renderPage(); $('#trace-agent-filter')?.focus(); }
    if ((event.target.id === 'select-all-agents' || event.target.dataset.agentSelect) && !ui.batch?.running && connected) {
      if (event.target.id === 'select-all-agents') ui.selectedAgents = new Set(event.target.checked ? data.profiles.filter(canStartProfile).map(p => p.id) : []);
      else {
        const id = event.target.dataset.agentSelect;
        if (event.target.checked && canStartProfile(data.profiles.find(p => p.id === id))) ui.selectedAgents.add(id);
        else ui.selectedAgents.delete(id);
      }
      renderPage();
    }
  });
  document.addEventListener('toggle', event => {
    if (event.target.isConnected && event.target.matches?.('details[data-event-id]')) {
      if (event.target.open) ui.expandedEvents.add(event.target.dataset.eventId); else ui.expandedEvents.delete(event.target.dataset.eventId);
    }
  }, true);
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(); });
  window.addEventListener('hashchange', route);
  window.addEventListener('resize', drawTopology);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadState(true); });
  paintIcons();
  route();
  loadState();
  setInterval(() => { if (!document.hidden) loadState(); }, 2000);
  setInterval(() => { if (data) updateChrome(); }, 2000);
})();
