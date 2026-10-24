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
    };
  })();`, context, { filename: 'control/static/app.js' });
  const app = context.consoleTest;
  app.setup({ profiles, hosts: [], topics: [], events: [], admin: {}, credentials_configured: credentials, csrf_token: 'test-csrf' }, {
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
