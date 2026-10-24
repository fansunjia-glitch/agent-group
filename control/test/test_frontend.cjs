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
  const dialog = { addEventListener() {} };
  const context = vm.createContext({
    document: {
      querySelector: () => dialog,
      addEventListener: (name, listener) => {
        const callbacks = listeners.get(name) || [];
        callbacks.push(listener); listeners.set(name, callbacks);
      },
    },
    window: { addEventListener() {} },
    location: { hash: '#agents' },
    URLSearchParams,
  });
  vm.runInContext(source.slice(0, bootstrap) + `
    globalThis.consoleTest = {
      ui, busy,
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
      history: chatHistory,
      refreshHistory: refreshTopicHistory,
      historyControls: renderHistoryControls,
      topicMessages,
      topicDetail: renderTopicDetail,
      groupTrace: groupTraceEvents,
      traceSpan: renderTraceSpan,
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
    app, observations,
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
    deleted_topic_ids: [], csrf_token: 'test-csrf', ...extra };
}

test('deleted topics stay absent despite stale discovery and retained history or cached metadata', async () => {
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
  assert.equal(app.data.events.length, 2, 'Deletion must not erase retained trace events');
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
