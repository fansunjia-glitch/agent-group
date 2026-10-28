/* Exercise the shipped console's batch and topic workflows without a browser,
 * dependencies, external model calls, or changes to the user's workspace. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const bootstrap = source.lastIndexOf('\n  paintIcons();');
assert.ok(bootstrap > 0, 'The app bootstrap must be present');

function profile(id, state = 'stopped', pid = null) {
  return { id, name: `Resident ${id}`, runtime: { state, pid } };
}

function harness(profiles, { credentials = true, request = async () => ({ state: 'starting', pid: 100 }) } = {}) {
  const listeners = new Map();
  const observations = { requests: [], credentialsOpened: 0, refreshes: [], toasts: [], renders: 0, forms: [] };
  const controls = new Map();
  const dialog = { addEventListener() {}, querySelector: selector => controls.get(selector) || null,
    close() { this.open = false; }, replaceChildren() {} };
  const context = vm.createContext({
    document: {
      querySelector: selector => controls.get(selector) || dialog,
      addEventListener: (name, listener) => {
        const callbacks = listeners.get(name) || [];
        callbacks.push(listener); listeners.set(name, callbacks);
      },
    },
    window: { addEventListener() {} },
    location: { hash: '#agents' },
    URLSearchParams,
    FormData: class {
      constructor(form) { this.fields = form.fields; }
      get(name) { return this.fields.get(name); }
      has(name) { return this.fields.has(name); }
    },
  });
  vm.runInContext(source.slice(0, bootstrap) + `
    globalThis.consoleTest = {
      ui, busy, chatHistories, observedTopics,
      get data() { return data; },
      setup(state, hooks) {
        data = state; connected = true;
        request = hooks.request;
        renderPage = hooks.render;
        loadState = hooks.load;
        toast = hooks.toast;
        openCredentials = hooks.credentials;
        openForm = hooks.form;
        updateChrome = () => {};
      },
      start: startSelectedAgents,
      action: handleAction,
      canStart: canStartProfile,
      reconcile: reconcileAgentSelection,
      renderBatch: renderBatchStart,
      refresh: loadState,
      canDeleteTopic,
      topicDeleteAction,
      renderTopicDeletionStatus,
      renderCleanupStatus,
      profileActions,
      history: chatHistory,
      refreshHistory: refreshTopicHistory,
      historyControls: renderHistoryControls,
      topicMessages,
      topicDetail: renderTopicDetail,
      groupTrace: groupTraceEvents,
      traceSpan: renderTraceSpan,
      providerFor, modelReady, renderProviders, openProvider, openDeleteProvider,
      syncProviderOptions,
      submit: submitForm,
    };
  })();`, context, { filename: 'control/static/app.js' });
  const app = context.consoleTest;
  app.setup({ profiles, hosts: [], topics: [], events: [], notices: [], admin: {}, credentials_configured: credentials, csrf_token: 'test-csrf' }, {
    request: async (url, options) => {
      observations.requests.push({ url, options });
      return request(url, options);
    },
    render: () => { observations.renders++; },
    load: async force => { observations.refreshes.push(force); },
    toast: (message, error) => observations.toasts.push({ message, error }),
    credentials: () => { observations.credentialsOpened++; },
    form: (...args) => observations.forms.push(args),
  });
  return {
    app, observations, controls,
    select: (...ids) => ids.forEach(id => app.ui.selectedAgents.add(id)),
    change: target => listeners.get('change').forEach(callback => callback({ target })),
  };
}

function deferredRequests() {
  const calls = [];
  let active = 0, maximum = 0;
  return {
    calls,
    get maximum() { return maximum; },
    request(url) {
      active++; maximum = Math.max(maximum, active);
      return new Promise((resolve, reject) => calls.push({
        id: decodeURIComponent(url.split('/')[3]),
        resolve: (runtime = { state: 'starting', pid: 100 }) => { active--; resolve(runtime); },
        reject: message => { active--; reject(new Error(message)); },
      }));
    },
  };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const selected = app => Array.from(app.ui.selectedAgents).sort();

test('selection ignores active/busy profiles and survives ordinary state refreshes', () => {
  const records = [profile('a'), profile('b', 'failed'), profile('c', 'working'),
    profile('d', 'starting'), profile('e', 'stopping'), profile('f', 'failed', 101), profile('g')];
  const { app, select } = harness(records);
  app.busy.add('profile:g');
  select(...records.map(p => p.id), 'removed');
  app.reconcile();
  assert.deepEqual(selected(app), ['a', 'b']);
  assert.equal(app.canStart(undefined), false);
  app.data.profiles = records.map(p => ({ ...p, runtime: { ...p.runtime } }));
  app.reconcile();
  assert.deepEqual(selected(app), ['a', 'b']);
  app.ui.batch = { running: true };
  app.busy.add('profile:a');
  app.data.profiles = app.data.profiles.filter(p => p.id !== 'b');
  app.reconcile();
  assert.deepEqual(selected(app), ['a'], 'A reservation stays selected, a deleted profile does not');
});

test('missing credentials opens credentials once without submitting or losing selection', async () => {
  const { app, select, observations } = harness([profile('a'), profile('b')], { credentials: false });
  select('a', 'b');
  await app.start();
  assert.equal(observations.credentialsOpened, 1);
  assert.equal(observations.requests.length, 0);
  assert.deepEqual(selected(app), ['a', 'b']);
  assert.equal(app.ui.batch, null);
  assert.equal(app.busy.size, 0);
});

test('batch reserves queued profiles, caps concurrency at three and isolates failures', async () => {
  const deferred = deferredRequests();
  const { app, select, observations } = harness('abcdef'.split('').map(id => profile(id)), { request: deferred.request });
  select(...'abcdef');
  const completion = app.start();
  assert.deepEqual(deferred.calls.map(c => c.id), ['a', 'b', 'c']);
  assert.equal(app.busy.size, 6, 'Queued profiles are reserved before requests resolve');
  assert.match(app.renderBatch(), /本次启动 <strong>6<\/strong> 个/);
  assert.doesNotMatch(app.renderBatch(), /已选 <strong>6<\/strong> \/ 0/);
  await app.start();
  await app.action('start-profile', 'd');
  await app.action('edit-profile', 'e');
  await app.action('delete-profile', 'f');
  assert.equal(deferred.calls.length, 3, 'Double-click and queued single-item actions cannot duplicate work');
  deferred.calls[0].resolve();
  await flush();
  assert.equal(deferred.calls[3].id, 'd');
  deferred.calls[1].reject('Worker unavailable');
  await flush();
  assert.equal(deferred.calls[4].id, 'e', 'A failed item must not stop its queue worker');
  deferred.calls[2].resolve();
  await flush();
  assert.equal(deferred.calls[5].id, 'f');
  deferred.calls.slice(3).forEach(call => call.resolve());
  await completion;
  assert.equal(deferred.maximum, 3);
  assert.equal(app.ui.batch.running, false);
  assert.equal(app.busy.size, 0);
  assert.deepEqual(Array.from(app.ui.batch.items, i => [i.id, i.status]), [
    ['a', 'submitted'], ['b', 'error'], ['c', 'submitted'],
    ['d', 'submitted'], ['e', 'submitted'], ['f', 'submitted'],
  ]);
  assert.deepEqual(selected(app), ['b'], 'Only the failed item stays selected');
  assert.deepEqual(observations.refreshes, [true]);
  assert.equal(observations.toasts.at(-1).error, true);
  observations.requests.forEach(({ options }) => {
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['X-Control-Token'], 'test-csrf');
  });
});

test('queued items recheck fresh state and skip profiles started elsewhere or removed', async () => {
  const deferred = deferredRequests();
  const { app, select } = harness('abcdef'.split('').map(id => profile(id)), { request: deferred.request });
  select(...'abcdef');
  const completion = app.start();
  app.data.profiles.find(p => p.id === 'd').runtime = { state: 'idle', pid: 200 };
  app.data.profiles.find(p => p.id === 'e').runtime = { state: 'failed', pid: 201 };
  app.data.profiles = app.data.profiles.filter(p => p.id !== 'f');
  deferred.calls.forEach(call => call.resolve());
  await completion;
  assert.deepEqual(deferred.calls.map(c => c.id), ['a', 'b', 'c']);
  assert.deepEqual(Array.from(app.ui.batch.items.slice(3), item => item.status), ['skipped', 'skipped', 'skipped']);
  assert.match(app.ui.batch.items[5].error, /移除/);
  assert.equal(app.ui.selectedAgents.size, 0);
  assert.equal(app.busy.size, 0);
});

test('checkbox interactions select only eligible profiles and freeze during an active batch', () => {
  const { app, change } = harness([profile('a'), profile('b', 'working'), profile('c')]);
  change({ id: 'select-all-agents', checked: true, dataset: {} });
  assert.deepEqual(selected(app), ['a', 'c']);
  change({ id: 'select-agent-a', checked: false, dataset: { agentSelect: 'a' } });
  assert.deepEqual(selected(app), ['c']);
  change({ id: 'select-agent-b', checked: true, dataset: { agentSelect: 'b' } });
  assert.deepEqual(selected(app), ['c']);
  app.ui.batch = { running: true };
  change({ id: 'select-all-agents', checked: false, dataset: {} });
  assert.deepEqual(selected(app), ['c']);
});

test('retry selects only failures and never resubmits successful profiles', async () => {
  let shouldFail = true;
  const { app, select, observations } = harness([profile('a'), profile('b')], {
    request: async url => {
      if (url.includes('/b/') && shouldFail) throw new Error('SSH unavailable <retry>');
      return { state: 'starting', pid: 100 };
    },
  });
  select('a', 'b');
  await app.start();
  assert.match(app.renderBatch(), /SSH unavailable &lt;retry&gt;/, 'Error text must be escaped');
  await app.action('clear-agent-selection');
  await app.action('select-failed-agents');
  assert.deepEqual(selected(app), ['b']);
  shouldFail = false;
  await app.start();
  assert.deepEqual(observations.requests.map(r => r.url), [
    '/api/profiles/a/start', '/api/profiles/b/start', '/api/profiles/b/start',
  ]);
  assert.equal(app.ui.batch.items[0].status, 'submitted');
  assert.equal(app.ui.selectedAgents.size, 0);
});

function snapshot(extra = {}) {
  return { profiles: [], hosts: [], topics: [], events: [], notices: [], admin: {},
    deleted_topic_ids: [], deleted_profile_ids: [], csrf_token: 'test-csrf', ...extra };
}

test('deleted topics stay absent despite stale discovery, events, or cached metadata', async () => {
  const deleted = { topic_id: 'deleted', name: 'Former room', description: 'Cached description' };
  let response = snapshot({ topics: [deleted] });
  const { app, observations } = harness([], { request: async () => response });
  await app.refresh(true);
  assert.equal(app.data.topics[0].name, 'Former room');
  const events = [
    { id: 'sent-1', type: 'message_sent', topic_id: 'deleted', payload: 'Old chat' },
    { id: 'sent-2', type: 'message_sent', topic_id: 'ordinary-history', topic_name: 'Other chat' },
  ];
  response = snapshot({ topics: [deleted, { topic_id: 'live', name: 'Active room' }],
    events, deleted_topic_ids: ['deleted'] });
  await app.refresh(true);
  assert.deepEqual(Array.from(app.data.topics, topic => topic.topic_id), ['live', 'ordinary-history']);
  assert.equal(app.data.topics[1].is_historical, true, 'Non-deleted history remains available');
  assert.deepEqual(Array.from(app.data.events, event => event.id), ['sent-2'],
    'Events belonging to the deleted topic must not remain in trace or resurrect a room');
  response = snapshot({ events, deleted_topic_ids: ['deleted'] });
  await app.refresh(true);
  assert.deepEqual(Array.from(app.data.topics, topic => topic.topic_id), ['ordinary-history'],
    'A cached topic name must not resurrect a deleted historical topic');
  assert.ok(observations.requests.every(item => item.url === '/api/state'));
});

test('an older state response cannot resurrect a topic after a newer deletion snapshot', async () => {
  const pending = [];
  const { app } = harness([], { request: () => new Promise(resolve => pending.push(resolve)) });
  const oldRequest = app.refresh(true);
  const newRequest = app.refresh(true);
  pending[1](snapshot({ deleted_topic_ids: ['room'] }));
  await newRequest;
  pending[0](snapshot({ topics: [{ topic_id: 'room', name: 'Stale room' }] }));
  await oldRequest;
  assert.equal(app.data.topics.length, 0);
  assert.deepEqual(Array.from(app.data.deleted_topic_ids), ['room']);
});

test('the current administrator topic is protected while a same-name ordinary topic is deletable', async () => {
  const { app, observations } = harness([]);
  const admin = { topic_id: 'admin', name: 'Town announcements', is_admin: true, can_delete: true };
  const ordinary = { topic_id: 'ordinary', name: 'Town announcements', is_admin: false, can_delete: true };
  app.data.topics = [admin, ordinary];
  app.data.admin = { topic: admin };
  assert.equal(app.canDeleteTopic(admin), false);
  assert.equal(app.canDeleteTopic(ordinary), true);
  assert.equal(app.canDeleteTopic(null), false);
  assert.equal(app.canDeleteTopic({ ...ordinary, can_delete: false }), false);
  assert.doesNotMatch(app.topicDeleteAction(admin), /data-action="delete-topic"/);
  assert.match(app.topicDeleteAction(ordinary), /data-action="delete-topic"/);
  await app.action('delete-topic', 'admin');
  assert.equal(observations.forms.length, 0, 'Protected topic must not open a deletion form');
  await app.action('delete-topic', 'ordinary');
  assert.equal(observations.forms.length, 1);
  assert.equal(observations.forms[0][0], 'delete-topic');
  assert.equal(observations.forms[0][4], 'ordinary');
  assert.equal(observations.requests.length, 0, 'Opening confirmation must not perform deletion');
});

test('pending deletion status lists affected agents and safely renders names and errors', () => {
  const person = { ...profile('a'), name: '<b>alice</b> & bobe' };
  const { app } = harness([person]);
  app.data.topic_deletions = [
    { topic_id: 'pending', name: '<svg onload=evil()>', pending_profiles: ['a'],
      errors: { a: 'Connection <failed> & retry' } },
    { topic_id: 'complete', name: 'Completed deletion', pending_profiles: [] },
  ];
  const html = app.renderTopicDeletionStatus();
  assert.match(html, /&lt;svg onload=evil\(\)&gt;/);
  assert.match(html, /&lt;b&gt;alice&lt;\/b&gt; &amp; bobe/);
  assert.match(html, /Connection &lt;failed&gt; &amp; retry/);
  assert.match(html, /确认退出/);
  assert.doesNotMatch(html, /<svg onload=evil|<b>alice|Completed deletion/);
  app.data.topic_deletions[0].pending_profiles = [];
  assert.equal(app.renderTopicDeletionStatus(), '', 'Completed deletions no longer show as pending');
});

test('pending cascade cleanup disables start and edit while exposing an escaped retry status', async () => {
  const pending = { ...profile('pending'), runtime: { state: 'stopped', cleanup_pending: true } };
  const { app, select, observations } = harness([pending, profile('ready')]);
  assert.equal(app.canStart(pending), false);
  select('pending', 'ready');
  app.reconcile();
  assert.deepEqual(selected(app), ['ready'], 'A pending deletion must not join a batch start');
  const actions = app.profileActions(pending);
  assert.match(actions, /<button[^>]*data-action="start-profile"[^>]* disabled>/);
  assert.match(actions, /<button[^>]*data-action="edit-profile"[^>]* disabled>/);
  assert.match(actions, /<button[^>]*data-action="delete-profile"[^>]*>[^]*?重试清理/);
  assert.doesNotMatch(actions, /<button[^>]*data-action="delete-profile"[^>]* disabled>/);
  assert.match(actions, /清理中/);
  await app.action('start-profile', 'pending');
  await app.action('edit-profile', 'pending');
  assert.equal(observations.requests.length, 0, 'Even a stale action must not start a pending deletion');
  assert.equal(observations.forms.length, 0);
  assert.equal(observations.toasts.length, 2);
  assert.ok(observations.toasts.every(item => item.error && item.message.includes('正在清理关联数据')));
  app.data.cleanup_jobs = [
    { kind: 'profile', id: 'pending', name: '<img src=x onerror=evil()> & alice', error: 'Workspace <locked> & retry' },
    { kind: 'topic', id: 'room', name: 'Town room', error: 'Archive <unavailable>' },
  ];
  const status = app.renderCleanupStatus();
  assert.match(status, /role="status"/);
  assert.match(status, /Agent 删除尚未完成/);
  assert.match(status, /Topic 删除尚未完成/);
  assert.match(status, /自动重试；重启服务后也会继续/);
  assert.match(status, /&lt;img src=x onerror=evil\(\)&gt; &amp; alice/);
  assert.match(status, /Workspace &lt;locked&gt; &amp; retry/);
  assert.match(status, /Archive &lt;unavailable&gt;/);
  assert.doesNotMatch(status, /<img src=x|<locked>|<unavailable>/);
  app.data.cleanup_jobs = [];
  assert.equal(app.renderCleanupStatus(), '');
});

test('agent and topic deletion confirmations disclose cascade scope without sending a request', async () => {
  const person = { ...profile('a'), name: '<script>alice</script>' };
  const { app, observations } = harness([person]);
  app.data.topics = [{ topic_id: 'room', name: '<img src=x> & town', members: [] }];
  await app.action('delete-profile', 'a');
  await app.action('delete-topic', 'room');
  assert.equal(observations.requests.length, 0, 'Confirmations must precede destructive requests');
  assert.equal(observations.forms.length, 2);
  const [agentKind, , agentBody, , agentId] = observations.forms[0];
  assert.equal(agentKind, 'delete-profile'); assert.equal(agentId, 'a');
  assert.match(agentBody, /&lt;script&gt;alice&lt;\/script&gt;/);
  assert.doesNotMatch(agentBody, /<script>/);
  assert.match(agentBody, /历史、记忆、定时任务和生成文件/);
  assert.match(agentBody, /运行日志、该 Agent 的聊天与执行记录，以及通知目标和回执/);
  assert.match(agentBody, /共享 Topic 和其他成员的记录保留/);
  assert.match(agentBody, /离线也会清理/);
  assert.match(agentBody, /此操作不可恢复/);
  const [topicKind, , topicBody, , topicId] = observations.forms[1];
  assert.equal(topicKind, 'delete-topic'); assert.equal(topicId, 'room');
  assert.match(topicBody, /&lt;img src=x&gt; &amp; town/);
  assert.doesNotMatch(topicBody, /<img src=x>/);
  assert.match(topicBody, /聊天、执行记录、成员关系，以及关联通知和回执/);
  assert.match(topicBody, /离线成员下次启动前同步/);
  assert.match(topicBody, /保留 Agent 人设、已形成的记忆和其他话题/);
  assert.match(topicBody, /此操作不可恢复/);
});

function archived(seq, content = `Message ${seq}`, extra = {}) {
  return { seq, key: `event-${seq}`, topic_id: 'town', profile_id: 'a', author: 'alice',
    ts: new Date(1700000000000 + seq * 1000).toISOString(), content, truncated: false, ...extra };
}

// Implements the documented HTTP page shape, independently of the frontend's
// cache/merge algorithm. Pages use exclusive cursors and oldest-first messages.
function historyServer(records) {
  return async url => {
    const params = new URL(url, 'http://fixture').searchParams;
    const query = (params.get('q') || '').toLowerCase();
    const matching = records.filter(m => `${m.author} ${m.content}`.toLowerCase().includes(query));
    const before = params.has('before') ? Number(params.get('before')) : null;
    const after = params.has('after') ? Number(params.get('after')) : null;
    assert.ok(before === null || after === null, 'A history request must use only one cursor direction');
    const limit = Number(params.get('limit'));
    const eligible = matching.filter(m => (before === null || m.seq < before) && (after === null || m.seq > after));
    const page = after === null ? eligible.slice(-limit) : eligible.slice(0, limit);
    return { messages: page.map(m => ({ ...m })), total: matching.length,
      has_more: eligible.length > limit, next_before: page[0]?.seq ?? null,
      latest_cursor: page.at(-1)?.seq ?? after ?? 0 };
  };
}

function openChat(app, extra = {}) {
  const topic = { topic_id: 'town', name: 'Town chat', members: [], ...extra };
  app.data.topics = [topic];
  app.ui.page = 'topics'; app.ui.id = topic.topic_id; app.ui.chatQuery = '';
  return topic;
}

test('profile deletion invalidates all cached chat pages and searches while preserving shared topics', async () => {
  const town = { topic_id: 'town', name: 'Town chat', members: [] };
  let revision = 1;
  let records = [archived(1, 'alice former message'), archived(2, 'bobe retained message', { profile_id: 'b', author: 'bobe' })];
  let state = snapshot({ history_revision: revision, profiles: [profile('a'), profile('b')],
    topics: [town, { topic_id: 'retired', name: 'Old discovery' }] });
  const { app, observations } = harness([], { request: async url => url === '/api/state' ? state
    : { ...await historyServer(records)(url), history_revision: revision } });
  await app.refresh(true);
  openChat(app);
  await app.refreshHistory();
  const oldHistory = app.history();
  app.ui.chatQuery = 'alice';
  await app.refreshHistory();
  assert.equal(app.history().messages[0].profile_id, 'a');
  assert.equal(app.chatHistories.size, 2);
  app.ui.chatScroll.set('town', { top: 200, height: 1000 });
  app.ui.expandedEvents.add('alice-run');
  app.ui.traceProfile = 'a';
  app.ui.page = 'overview';
  revision = 2;
  records = records.filter(message => message.profile_id !== 'a');
  state = snapshot({ history_revision: revision, profiles: [profile('b')], topics: [town],
    deleted_profile_ids: ['a'], events: [
      { id: 'removed-author', type: 'message_sent', profile_id: 'a', topic_id: 'town' },
      { id: 'removed-trace', type: 'model_started', profile_id: 'a' },
      { id: 'removed-discovery', type: 'message_sent', profile_id: 'a', topic_id: 'retired' },
      { id: 'retained', type: 'message_sent', profile_id: 'b', topic_id: 'town' },
    ] });
  await app.refresh(true);
  assert.equal(app.chatHistories.size, 0, 'Both ordinary and search pages must be invalidated');
  assert.deepEqual(Array.from(app.observedTopics.keys()), ['town']);
  assert.equal(app.ui.chatScroll.size, 0);
  assert.equal(app.ui.expandedEvents.size, 0);
  assert.equal(app.ui.traceProfile, '');
  assert.deepEqual(Array.from(app.data.events, event => event.id), ['retained']);
  assert.deepEqual(Array.from(app.data.topics, topic => topic.topic_id), ['town']);
  const topic = openChat(app);
  await app.refreshHistory();
  assert.notEqual(app.history(), oldHistory);
  assert.deepEqual(Array.from(app.topicMessages(topic), message => message.profileId), ['b']);
  assert.doesNotMatch(observations.requests.at(-1).url, /before=|after=/,
    'After a purge the shared topic must restart from a complete fresh page');
});

test('an in-flight history response cannot restore a deleted author or overwrite a replacement cache', async () => {
  let resolveOldPage;
  let historyRequests = 0;
  let state = snapshot({ history_revision: 1, profiles: [profile('a'), profile('b')],
    topics: [{ topic_id: 'town', name: 'Town chat', members: [] }] });
  const retained = archived(2, 'bobe retained message', { profile_id: 'b', author: 'bobe' });
  const { app, observations } = harness([], { request: url => {
    if (url === '/api/state') return Promise.resolve(state);
    if (++historyRequests === 1) return new Promise(resolve => { resolveOldPage = resolve; });
    return Promise.resolve({ messages: [retained], total: 1, has_more: false,
      next_before: 2, latest_cursor: 2, history_revision: 2 });
  } });
  await app.refresh(true);
  const topic = openChat(app);
  const pending = app.refreshHistory();
  const oldHistory = app.history();
  app.ui.page = 'overview';
  state = { ...state, history_revision: 2, profiles: [profile('b')], deleted_profile_ids: ['a'] };
  await app.refresh(true);
  openChat(app);
  await app.refreshHistory();
  const freshHistory = app.history();
  const renders = observations.renders;
  resolveOldPage({ messages: [archived(1, 'alice deleted message')], total: 2, has_more: true,
    next_before: 1, latest_cursor: 1, history_revision: 1 });
  await pending;
  assert.equal(app.history(), freshHistory);
  assert.notEqual(freshHistory, oldHistory);
  assert.equal(oldHistory.messages.length, 0, 'A detached cache must not consume late pages');
  assert.deepEqual(Array.from(app.topicMessages(topic), message => message.profileId), ['b']);
  assert.equal(freshHistory.total, 1);
  assert.equal(freshHistory.newest, 2);
  assert.equal(freshHistory.moreBefore, false);
  assert.equal(observations.renders, renders, 'A detached request must not repaint the replacement view');
});

test('topic tombstones evict every cached query and reject a pending page without a revision change', async () => {
  let resolveOldPage;
  let historyRequests = 0;
  let state = snapshot({ topics: [{ topic_id: 'town', name: 'Town chat', members: [] }] });
  const { app, observations } = harness([], { request: url => {
    if (url === '/api/state') return Promise.resolve(state);
    if (++historyRequests === 3) return new Promise(resolve => { resolveOldPage = resolve; });
    return Promise.resolve({ messages: [archived(1, 'Old room message')], total: 1,
      has_more: false, next_before: 1, latest_cursor: 1 });
  } });
  await app.refresh(true);
  openChat(app);
  await app.refreshHistory();
  app.ui.chatQuery = 'Old';
  await app.refreshHistory();
  app.ui.chatQuery = '';
  const pending = app.refreshHistory();
  const unrelated = app.history('other', '');
  app.ui.page = 'overview';
  state = snapshot({ topics: state.topics, deleted_topic_ids: ['town'], events: [
    { id: 'stale', type: 'message_sent', topic_id: 'town', topic_name: 'Old room' },
    { id: 'kept', type: 'message_sent', topic_id: 'other', topic_name: 'Other room' },
  ] });
  await app.refresh(true);
  assert.deepEqual(Array.from(app.chatHistories.values(), history => history.topicId), ['other']);
  assert.equal(app.history('other', ''), unrelated, 'Unrelated legacy caches remain usable');
  assert.equal(app.observedTopics.has('town'), false);
  assert.deepEqual(Array.from(app.data.topics, topic => topic.topic_id), ['other']);
  assert.deepEqual(Array.from(app.data.events, event => event.id), ['kept']);
  const renders = observations.renders;
  resolveOldPage({ messages: [archived(2, 'Late deleted room message')], total: 2,
    has_more: false, next_before: 2, latest_cursor: 2 });
  await pending;
  assert.equal(observations.renders, renders);
  assert.deepEqual(Array.from(app.chatHistories.values(), history => history.topicId), ['other']);
  app.ui.page = 'topics'; app.ui.id = 'town';
  const requests = observations.requests.length;
  await app.refreshHistory();
  assert.equal(observations.requests.length, requests, 'A deleted topic must not fetch or recreate its cache');
  assert.equal(app.chatHistories.size, 1);
});

for (const responseRevision of [1, 3]) {
  test(`history page revision ${responseRevision} cannot merge into state revision 2`, async () => {
    let historyRequests = 0;
    const { app, observations } = harness([], { request: async () => {
      const initial = ++historyRequests === 1;
      return { messages: [archived(initial ? 2 : 3, initial ? 'Current message' : 'Mismatched page')],
        total: initial ? 1 : 99, has_more: false, next_before: initial ? 2 : 3,
        latest_cursor: initial ? 2 : 3, history_revision: initial ? 2 : responseRevision };
    } });
    app.data.history_revision = 2;
    const topic = openChat(app);
    await app.refreshHistory();
    await app.refreshHistory();
    assert.deepEqual(Array.from(app.topicMessages(topic), message => message.content), ['Current message']);
    assert.equal(app.history().total, 1);
    assert.equal(app.history().newest, 2);
    assert.equal(app.history().before, 2);
    assert.equal(app.history().loading, false);
    assert.deepEqual(observations.refreshes, responseRevision > 2 ? [true] : [],
      'Only a page from a newer archive revision should request a fresh state snapshot');
  });
}

test('history pages retain more than 400 full messages while new arrivals merge without duplicates', async () => {
  const tail = '尾部独有关键词 <tail> & complete';
  const fullText = 'Long message '.repeat(500) + tail;
  const records = Array.from({ length: 451 }, (_, i) => archived(i + 1,
    i === 6 ? fullText : [19, 20].includes(i) ? 'Repeated text' : `Message ${i + 1}`));
  const { app, observations } = harness([profile('a')], { request: historyServer(records) });
  const topic = openChat(app);
  await app.refreshHistory();
  assert.equal(app.history().messages.length, 50);
  assert.equal(app.history().before, 402);
  await app.refreshHistory('older');
  assert.equal(app.history().before, 352);
  records.push(...Array.from({ length: 129 }, (_, i) => archived(i + 452)));
  await app.refreshHistory();
  assert.equal(app.history().newest, 580);
  assert.equal(app.history().before, 352, 'Newer pages must not move the earlier-history cursor');
  while (app.history().moreBefore) await app.refreshHistory('older');
  await app.refreshHistory();
  const messages = app.topicMessages(topic);
  assert.equal(messages.length, 580);
  assert.equal(new Set(messages.map(m => m.key)).size, 580);
  assert.deepEqual(Array.from(messages, m => m.seq), Array.from({ length: 580 }, (_, i) => i + 1));
  assert.equal(messages.find(m => m.seq === 7).content, fullText);
  assert.equal(messages.filter(m => m.content === 'Repeated text').length, 2);
  assert.match(app.historyControls(app.history()), /共 580 条 · 已加载 580 条/);
  assert.match(app.topicDetail(), /尾部独有关键词 &lt;tail&gt; &amp; complete/);
  app.ui.chatQuery = '  尾部独有关键词  ';
  await app.refreshHistory();
  assert.equal(app.history().total, 1);
  assert.equal(app.topicMessages(topic)[0].content, fullText, 'Search must preserve the entire matching body');
  assert.match(observations.requests.at(-1).url, /q=/);
  assert.equal(new URL(observations.requests.at(-1).url, 'http://fixture').searchParams.get('q'), '尾部独有关键词');
});

test('slow previous search results cannot replace the currently displayed query', async () => {
  const pending = [];
  const { app, observations } = harness([], { request: url => new Promise(resolve => pending.push({ url, resolve })) });
  const topic = openChat(app);
  app.ui.chatQuery = 'old';
  const oldSearch = app.refreshHistory();
  app.ui.chatQuery = 'new';
  const newSearch = app.refreshHistory();
  pending[1].resolve({ messages: [archived(2, 'New result')], total: 1, has_more: false, next_before: 2, latest_cursor: 2 });
  await newSearch;
  const renders = observations.renders;
  pending[0].resolve({ messages: [archived(1, 'Old result')], total: 1, has_more: false, next_before: 1, latest_cursor: 1 });
  await oldSearch;
  assert.equal(app.topicMessages(topic)[0].content, 'New result');
  assert.equal(observations.renders, renders, 'A completed inactive query must not rerender the current view');
  assert.equal(app.history('town', 'old').messages[0].content, 'Old result', 'The old result can remain in its own cache');
});

test('an older-page failure preserves loaded history and retries the same earlier cursor', async () => {
  const records = Array.from({ length: 100 }, (_, i) => archived(i + 1));
  const serve = historyServer(records);
  let failOlder = true;
  const { app, observations } = harness([], { request: async url => {
    if (url.includes('before=') && failOlder) { failOlder = false; throw new Error('History <unavailable>'); }
    return serve(url);
  } });
  openChat(app);
  await app.refreshHistory();
  const keys = Array.from(app.history().messages, m => m.key);
  await app.refreshHistory('older');
  assert.deepEqual(Array.from(app.history().messages, m => m.key), keys);
  assert.equal(app.history().before, 51);
  assert.equal(app.history().newest, 100);
  assert.equal(app.history().moreBefore, true);
  assert.equal(app.history().lastMode, 'older');
  assert.match(app.historyControls(app.history()), /History &lt;unavailable&gt;/);
  await app.action('chat-history-retry');
  assert.equal(observations.requests.at(-1).url, observations.requests.at(-2).url);
  assert.equal(app.history().messages.length, 100);
  assert.equal(app.history().before, 1);
  assert.equal(app.history().moreBefore, false);
  assert.equal(app.history().error, '');
});

test('an initially empty topic picks up its first message with an after-zero request', async () => {
  const records = [];
  const { app, observations } = harness([], { request: historyServer(records) });
  const topic = openChat(app);
  await app.refreshHistory();
  assert.equal(app.history().initialized, true);
  assert.equal(app.history().messages.length, 0);
  assert.equal(app.history().newest, 0);
  records.push(archived(17, 'First message'));
  await app.refreshHistory();
  assert.match(observations.requests.at(-1).url, /after=0/);
  assert.equal(app.topicMessages(topic)[0].content, 'First message');
  assert.equal(app.history().total, 1);
  assert.equal(app.history().newest, 17);
});

test('background refresh success or failure preserves an older-page error and its retry direction', async () => {
  const records = Array.from({ length: 100 }, (_, i) => archived(i + 1));
  const serve = historyServer(records);
  let failOlder = true, failNewer = false;
  const { app, observations } = harness([], { request: async url => {
    if (url.includes('before=') && failOlder) { failOlder = false; throw new Error('Earlier history unavailable'); }
    if (url.includes('after=') && failNewer) throw new Error('Newer poll unavailable');
    return serve(url);
  } });
  openChat(app);
  await app.refreshHistory();
  await app.refreshHistory('older');
  const retryUrl = observations.requests.at(-1).url;
  records.push(archived(101, 'New message while the earlier page is unavailable'));
  await app.refreshHistory();
  assert.equal(app.history().messages.at(-1).seq, 101);
  assert.equal(app.history().error, 'Earlier history unavailable');
  assert.equal(app.history().lastMode, 'older');
  assert.equal(app.history().before, 51);
  failNewer = true;
  await app.refreshHistory();
  assert.equal(app.history().error, 'Earlier history unavailable');
  assert.equal(app.history().lastMode, 'older');
  assert.match(app.historyControls(app.history()), /Earlier history unavailable/);
  await app.action('chat-history-retry');
  assert.equal(observations.requests.at(-1).url, retryUrl);
  assert.equal(app.history().error, '');
  assert.equal(app.history().messages.length, 101);
  assert.equal(app.history().newest, 101);
  assert.equal(app.history().before, 1);
});

test('archived notices remain administrator bubbles after their center record is deleted', async () => {
  const records = [archived(1, '<announcement> Remains delivered', { notice_id: 'removed-notice', profile_id: null, author: '管理员' })];
  const { app } = harness([], { request: historyServer(records) });
  const topic = openChat(app, { is_admin: true });
  await app.refreshHistory();
  assert.equal(app.topicMessages(topic)[0].isNotice, true);
  assert.equal(app.topicMessages(topic)[0].notice, undefined);
  const html = app.topicDetail();
  assert.match(html, /chat-message from-admin/);
  assert.match(html, /&lt;announcement&gt; Remains delivered/);
  assert.doesNotMatch(html, /href="#notices\/removed-notice"|chat-message-receipt/);
});

function traceEvent(id, type, extra = {}) {
  return { id: String(id), type, profile_id: 'a', run_id: 'run-one', name: 'alice',
    ts: new Date(1700000000000 + id * 1000).toISOString(), ...extra };
}
function representedEvents(spans) {
  return spans.flatMap(span => [span.event, span.start, span.finish].filter(Boolean)
    .concat(representedEvents(span.children || [])));
}

test('trace hierarchy pairs model and tool spans in time order without losing repeated messages', () => {
  const events = [traceEvent(1, 'ready'), traceEvent(2, 'model_started'), traceEvent(3, 'model_finished'),
    traceEvent(4, 'tool_started', { call_id: 'call-one', tool_name: 'send_message' }),
    traceEvent(5, 'message_sent', { payload: 'Same text' }), traceEvent(6, 'message_sent', { payload: 'Same text' }),
    traceEvent(7, 'tool_finished', { call_id: 'call-one', result: 'Delivered' }),
    traceEvent(8, 'state', { state: 'idle' }), traceEvent(9, 'model_started'), traceEvent(10, 'model_finished'),
    traceEvent(11, 'heartbeat')];
  const { app } = harness([profile('a')]);
  const runs = app.groupTrace(events.slice().reverse());
  assert.equal(runs.length, 1);
  const spans = runs[0].spans;
  assert.deepEqual(Array.from(spans, s => s.kind), ['event', 'model', 'model']);
  const model = spans[1], tool = model.children[0];
  assert.equal(model.start.id, '2'); assert.equal(model.finish.id, '3');
  assert.equal(tool.start.id, '4'); assert.equal(tool.finish.id, '7');
  assert.deepEqual(Array.from(tool.children, s => s.event.id), ['5', '6']);
  assert.equal(model.children[1].event.id, '8');
  assert.deepEqual(Array.from(representedEvents(spans), e => Number(e.id)).sort((a, b) => a - b),
    Array.from({ length: 10 }, (_, i) => i + 1));
  assert.match(app.traceSpan(model), /trace-children/);
});

test('trace call IDs are isolated by agent and run, even when reused', () => {
  const events = [traceEvent(1, 'tool_started', { call_id: 'reused' }),
    traceEvent(2, 'tool_started', { call_id: 'reused', run_id: 'run-two' }),
    traceEvent(3, 'tool_finished', { call_id: 'reused' }),
    traceEvent(4, 'tool_finished', { call_id: 'reused', run_id: 'run-two' }),
    traceEvent(5, 'tool_started', { call_id: 'reused', profile_id: 'b' }),
    traceEvent(6, 'tool_finished', { call_id: 'reused', profile_id: 'b' })];
  const { app } = harness([]);
  const runs = app.groupTrace(events);
  assert.equal(runs.length, 3);
  assert.deepEqual(Array.from(runs, run => [run.profileId, run.runId, run.spans[0].start.id, run.spans[0].finish.id]),
    [['b', 'run-one', '5', '6'], ['a', 'run-two', '2', '4'], ['a', 'run-one', '1', '3']]);
});

test('orphan trace spans show missing-boundary markers and retain their original events', () => {
  const events = [traceEvent(1, 'tool_finished', { call_id: 'orphan', result: '<done>' }),
    traceEvent(2, 'model_finished'), traceEvent(3, 'model_started'),
    traceEvent(4, 'tool_started', { call_id: 'unfinished' })];
  const { app } = harness([]);
  const spans = app.groupTrace(events)[0].spans;
  assert.match(app.traceSpan(spans[0]), /起始事件不在当前采集窗口内/);
  assert.match(app.traceSpan(spans[0]), /&lt;done&gt;/);
  assert.match(app.traceSpan(spans[1]), /起始事件不在当前采集窗口内/);
  assert.match(app.traceSpan(spans[2]), /未见结束事件/);
  assert.deepEqual(Array.from(representedEvents(spans), e => e.id).sort(), ['1', '2', '3', '4']);
});

test('notice deletion opens a safe explicit confirmation without performing a destructive request', async () => {
  const { app, observations } = harness([]);
  app.data.notices = [{ id: 'notice-one', content: '<img src=x onerror=evil()> & message' }];
  await app.action('delete-notice', 'notice-one');
  assert.equal(observations.requests.length, 0);
  assert.equal(observations.forms.length, 1);
  const [kind, , body, , id] = observations.forms[0];
  assert.equal(kind, 'delete-notice'); assert.equal(id, 'notice-one');
  assert.match(body, /&lt;img src=x onerror=evil\(\)&gt; &amp; message/);
  assert.doesNotMatch(body, /<img src=x/);
  assert.match(body, /不是撤回/);
  assert.match(body, /停止此记录的后续自动补发/);
  await app.action('delete-notice', 'absent');
  assert.equal(observations.forms.length, 1);
  assert.equal(observations.requests.length, 0);
});

function provider(id, credentials_configured = true, extra = {}) {
  return { id, name: `Vendor ${id}`, base_url: `https://${id}.example/v1`, model: `model-${id}`,
    api_mode: 'openai', credentials_configured, ...extra };
}

function formEvent(h, kind, fields, id = null) {
  const keyInput = { value: fields.api_key || '' };
  const buttons = [{ disabled: false }];
  const form = { fields: new Map(Object.entries(fields)), reportValidity: () => true,
    querySelectorAll: () => buttons, querySelector: selector => selector === '[name="api_key"]' ? keyInput : null };
  h.app.ui.formKind = kind;
  h.app.ui.editingId = id;
  h.controls.set('input[name="api_key"]', keyInput);
  h.controls.set('#form-error', { hidden: true, textContent: '' });
  h.controls.set('#form-submit', { textContent: 'Save' });
  return { event: { preventDefault() {}, target: form }, keyInput, buttons };
}

test('provider credentials are resolved per agent and cannot borrow a global or another provider key', () => {
  const records = [{ ...profile('a'), provider_id: 'one' }, { ...profile('b'), provider_id: 'two' },
    { ...profile('c'), provider_id: 'missing' }];
  const { app } = harness(records, { credentials: false });
  app.data.model_providers = [provider('one'), provider('two', false)];
  assert.equal(app.modelReady(records[0]), true, 'A configured provider does not depend on the legacy global key');
  app.data.credentials_configured = true;
  assert.equal(app.modelReady(records[1]), false, 'Another configured provider must not make this one ready');
  assert.equal(app.modelReady(records[2]), false);
  assert.equal(app.modelReady(undefined), false);
  app.data.model_providers = [];
  assert.equal(app.modelReady(records[0]), false, 'An explicitly empty provider list must not use the legacy fallback');
});

test('batch starts ready providers while retaining missing-key members for retry', async () => {
  const records = [{ ...profile('a'), provider_id: 'one' }, { ...profile('b'), provider_id: 'two' },
    { ...profile('c'), provider_id: 'three' }];
  const h = harness(records, { credentials: false });
  h.app.data.model_providers = [provider('one'), provider('two', false), provider('three')];
  h.select('a', 'b', 'c');
  await h.app.start();
  assert.deepEqual(h.observations.requests.map(r => r.url), ['/api/profiles/a/start', '/api/profiles/c/start']);
  assert.deepEqual(Array.from(h.app.ui.batch.items, i => [i.id, i.status]), [['a', 'submitted'], ['b', 'error'], ['c', 'submitted']]);
  assert.match(h.app.ui.batch.items[1].error, /API Key/);
  assert.deepEqual(selected(h.app), ['b']);
  assert.equal(h.observations.credentialsOpened, 0);
  assert.equal(h.app.busy.size, 0);
});

test('all missing provider keys preserve selection and individual start opens the correct blank-key form', async () => {
  const records = [{ ...profile('a'), provider_id: 'one' }, { ...profile('b'), provider_id: 'two' }];
  const h = harness(records, { credentials: true });
  h.app.data.model_providers = [provider('one', false), provider('two', false)];
  h.select('a', 'b');
  await h.app.start();
  assert.equal(h.observations.credentialsOpened, 1);
  assert.equal(h.app.ui.batch, null);
  assert.deepEqual(selected(h.app), ['a', 'b']);
  await h.app.action('start-profile', 'b');
  assert.equal(h.observations.requests.length, 0);
  assert.equal(h.observations.forms.at(-1)[0], 'provider');
  assert.equal(h.observations.forms.at(-1)[4], 'two');
  assert.match(h.observations.forms.at(-1)[2], /name="api_key"[^>]*value=""/);
});

test('queued batch items recheck provider readiness before submitting', async () => {
  const deferred = deferredRequests();
  const records = 'abcd'.split('').map(id => ({ ...profile(id), provider_id: id === 'd' ? 'two' : 'one' }));
  const h = harness(records, { request: deferred.request });
  h.app.data.model_providers = [provider('one'), provider('two')];
  h.select('a', 'b', 'c', 'd');
  const completion = h.app.start();
  assert.equal(deferred.calls.length, 3);
  h.app.data.model_providers[1].credentials_configured = false;
  deferred.calls[0].resolve();
  await flush();
  assert.equal(deferred.calls.length, 3, 'A key removed while queued must prevent submission');
  deferred.calls.slice(1).forEach(c => c.resolve());
  await completion;
  assert.equal(h.app.ui.batch.items[3].status, 'error');
  assert.deepEqual(selected(h.app), ['d']);
});

test('provider rendering escapes untrusted fields, shows only key status, and guards referenced deletion', async () => {
  const h = harness([{ ...profile('a'), name: '<alice>', provider_id: 'one' }]);
  h.app.data.model_providers = [provider('one', true, { name: '<img src=x>', model: '<model>', api_key: 'never-render-this-key' }), provider('two', false)];
  const html = h.app.renderProviders();
  assert.match(html, /&lt;img src=x&gt;/);
  assert.match(html, /&lt;model&gt;/);
  assert.match(html, /&lt;alice&gt;/);
  assert.doesNotMatch(html, /never-render-this-key|<img src=x>|<model>|<alice>/);
  assert.match(html, /Key 已设置/);
  assert.match(html, /待设置 Key/);
  assert.match(html, /data-action="delete-provider" data-id="one" disabled/);
  assert.doesNotMatch(html, /data-action="delete-provider" data-id="two" disabled/);
  await h.app.action('delete-provider', 'one');
  assert.equal(h.observations.forms.length, 0);
  assert.equal(h.observations.requests.length, 0);
  await h.app.action('delete-provider', 'two');
  assert.equal(h.observations.forms.at(-1)[0], 'delete-provider');
});

test('provider and agent editors use generic configuration without prefilled vendor credentials', async () => {
  const h = harness([]);
  h.app.data.model_providers = [];
  await h.app.action('new-profile');
  let form = h.observations.forms.at(-1);
  assert.equal(form[0], 'provider', 'A new agent first needs a provider configuration');
  assert.match(form[2], /name="base_url"[^>]*value=""/);
  assert.match(form[2], /name="model"[^>]*value=""/);
  assert.match(form[2], /value="openai" selected/);
  assert.doesNotMatch(form[2], /value="deepseek-flash"|value="https:\/\/api\.deepseek\.com"/);
  h.app.data.model_providers = [provider('one', true, { api_key: 'must-not-prefill' })];
  h.app.openProvider('one');
  form = h.observations.forms.at(-1);
  assert.match(form[2], /留空保持原 Key/);
  assert.match(form[2], /name="api_key"[^>]*value=""/);
  assert.doesNotMatch(form[2], /must-not-prefill/);
  h.app.data.hosts = [{ id: 'local', name: '本机', kind: 'local' }];
  await h.app.action('new-profile');
  form = h.observations.forms.at(-1);
  assert.equal(form[0], 'profile');
  assert.match(form[2], /name="provider_id"/);
  assert.match(form[2], /Vendor one · model-one/);
  assert.doesNotMatch(form[2], /name="base_url"|name="model"|name="api_key"/);
});

test('provider submission uses its own endpoint and clears the entered key after saving', async () => {
  const h = harness([], { request: async () => ({ id: 'new-provider' }) });
  const form = formEvent(h, 'provider', { name: 'My gateway', base_url: 'https://gateway.example/v1', model: 'custom-model', api_key: 'test-provider-secret' });
  await h.app.submit(form.event);
  assert.equal(h.observations.requests.length, 1);
  const { url, options } = h.observations.requests[0];
  assert.equal(url, '/api/model-providers');
  assert.equal(options.method, 'POST');
  assert.equal(options.headers['X-Control-Token'], 'test-csrf');
  assert.deepEqual(JSON.parse(options.body), { name: 'My gateway', base_url: 'https://gateway.example/v1', model: 'custom-model', api_mode: 'openai', api_key: 'test-provider-secret' });
  assert.equal(form.keyInput.value, '');
  assert.equal(h.app.ui.formKind, null);
  assert.ok(h.observations.toasts.every(t => !t.message.includes('test-provider-secret')));
});

test('blank provider key preserves it, explicit clearing is separate, and conflicting actions are rejected', async () => {
  const fields = { name: 'Gateway', base_url: 'https://gateway.example/v1', model: 'custom-model', api_mode: 'openai', api_key: '' };
  for (const clear of [false, true]) {
    const h = harness([], { request: async () => ({ id: 'one' }) });
    const form = formEvent(h, 'provider', { ...fields, ...(clear ? { clear_api_key: 'on' } : {}) }, 'one');
    await h.app.submit(form.event);
    const { url, options } = h.observations.requests[0];
    assert.equal(url, '/api/model-providers/one');
    assert.equal(options.method, 'PUT');
    const payload = JSON.parse(options.body);
    assert.equal(Object.hasOwn(payload, 'api_key'), false);
    assert.equal(payload.clear_api_key, clear ? true : undefined);
  }
  const h = harness([]);
  const form = formEvent(h, 'provider', { ...fields, api_key: 'test-secret', clear_api_key: 'on' }, 'one');
  await h.app.submit(form.event);
  assert.equal(h.observations.requests.length, 0);
  assert.match(h.controls.get('#form-error').textContent, /请选择一项/);
});

test('agent submission references the selected provider without copying model settings or keys', async () => {
  const h = harness([], { request: async () => ({ id: 'agent-one' }) });
  const form = formEvent(h, 'profile', { name: 'alice', persona: 'Resident', initial_prompt: 'Say hello', host_id: 'local', provider_id: 'vendor-two', enable_memory: 'on' });
  await h.app.submit(form.event);
  const { url, options } = h.observations.requests[0];
  assert.equal(url, '/api/profiles');
  assert.deepEqual(JSON.parse(options.body), { name: 'alice', persona: 'Resident', initial_prompt: 'Say hello', host_id: 'local', provider_id: 'vendor-two', enable_memory: true, thinking: false });
});

test('switching from DeepSeek to generic clears and disables its private thinking option', () => {
  const h = harness([]);
  h.app.data.model_providers = [provider('generic'), provider('deep', true, { api_mode: 'deepseek' })];
  const selectedProvider = { value: 'generic' }, thinking = { disabled: false, checked: true };
  const classes = new Map();
  h.controls.set('#form-provider_id', selectedProvider);
  h.controls.set('input[name="thinking"]', thinking);
  h.controls.set('#provider-thinking', { classList: { toggle: (name, on) => classes.set(name, on) } });
  h.app.syncProviderOptions();
  assert.equal(thinking.checked, false);
  assert.equal(thinking.disabled, true);
  assert.equal(classes.get('provider-option-disabled'), true);
  selectedProvider.value = 'deep';
  h.app.syncProviderOptions();
  assert.equal(thinking.disabled, false);
  assert.equal(thinking.checked, false, 'Switching back must not silently opt into thinking');
});
