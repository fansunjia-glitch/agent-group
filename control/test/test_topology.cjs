/* Real layout/render functions: no browser, network, or live workspace data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const context = vm.createContext({window:{}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/topology.js'),'utf8'),context);
const topology = context.window.AgentTopology;
const fixture = () => ({
  profiles:[{id:'c',name:'cancy'},{id:'a',name:'alice'},{id:'b',name:'bobe'}],
  topics:[
    {topic_id:'admin',name:'Town notices',is_admin:true,members:[{profile_id:'a'},{profile_id:'b'}]},
    {topic_id:'old',name:'Finished conversation',is_historical:true,members:[{profile_id:'a'}]},
    {topic_id:'retired',name:'Deleted conversation',members:[{profile_id:'c'}]},
    {topic_id:'flowers',name:'Flowers',members:[{profile_id:'c'},{profile_id:'a'},{profile_id:'a'},{profile_id:'unknown'}]},
    {topic_id:'wood',name:'Woodworking',members:[{profile_id:'b'}]},
  ],deleted_topic_ids:['retired'],
});
const IDs = items => Array.from(items, item=>item.id || item.topic_id);

test('only real current managed memberships become edges; admin is a separate rail',()=>{
  const model=topology.layout(fixture());
  assert.deepEqual(IDs(model.topics).sort(),['flowers','wood']);
  assert.deepEqual(IDs(model.admin),['admin']);
  assert.equal(model.historical,1);
  assert.equal(model.connections,3,'Duplicate/unknown members and historical/admin relations are not edges');
  assert.equal(model.linked.get('a').length,1);
  assert.equal(model.linked.get('b').length,1);
  assert.equal(model.linked.get('c').length,1);
});

test('server ordering changes do not make nodes jump between refreshes',()=>{
  const original=fixture();
  const first=topology.layout(original);
  const shuffled={...original,profiles:[...original.profiles].reverse(),topics:[...original.topics].reverse()};
  const second=topology.layout(shuffled);
  assert.deepEqual(IDs(first.profiles),IDs(second.profiles));
  assert.deepEqual(IDs(first.topics),IDs(second.topics));
  assert.deepEqual(IDs(original.profiles),['c','a','b'],'Layout never mutates source state');
});

test('labels and membership JSON remain escaped and every node uses existing navigation actions',()=>{
  const data=fixture();data.profiles[0].name='<img src=x onerror=alert(1)>';
  data.topics[3].name='"Topic <script>alert(1)</script>';
  const html=topology.render(data);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.equal((html.match(/data-action="view-agent"/g)||[]).length,3);
  assert.equal((html.match(/data-action="view-topic"/g)||[]).length,3);
  assert.ok(!html.includes('data-id="old"'));
  assert.ok(!html.includes('data-id="retired"'));
});
