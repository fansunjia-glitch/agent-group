/* Stable membership diagram: live topics, no force simulation or invented links. */
(() => {
  'use strict';
  const svgNS = 'http://www.w3.org/2000/svg';
  const activeStates = new Set(['idle', 'working']);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const compare = (a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN') || String(a.id || a.topic_id).localeCompare(String(b.id || b.topic_id));
  const tone = id => [...String(id)].reduce((sum, c) => (sum * 31 + c.codePointAt(0)) % 6, 0);
  let focusedNode = null;

  function layout(data) {
    const profiles = [...(data.profiles || [])].sort(compare);
    const byId = new Map(profiles.map(p => [p.id, p]));
    const deleted = new Set(data.deleted_topic_ids || []);
    const live = (data.topics || []).filter(t => !t.is_historical && !deleted.has(t.topic_id));
    const topics = live.filter(t => !t.is_admin).map(t => ({ ...t,
      members: [...new Set((t.members || []).map(m => m.profile_id))].filter(id => byId.has(id)).map(id => ({profile_id:id,name:byId.get(id).name})),
    })).sort(compare);
    const linked = new Map(profiles.map(p => [p.id, topics.filter(t => t.members.some(m => m.profile_id === p.id))]));
    // Alternating barycentres keeps neighbours close; stable name/ID ties make
    // snapshots deterministic instead of moving nodes on every poll.
    for (let pass = 0; pass < 4; pass++) {
      const positions = new Map(profiles.map((p, i) => [p.id, i]));
      const center = t => t.members.length ? t.members.reduce((sum, m) => sum + positions.get(m.profile_id), 0) / t.members.length : Number.MAX_SAFE_INTEGER;
      topics.sort((a, b) => center(a) - center(b) || compare(a, b));
      if (pass === 3) break;
      const topicPositions = new Map(topics.map((t, i) => [t.topic_id, i]));
      const agentCenter = p => linked.get(p.id).length ? linked.get(p.id).reduce((sum, t) => sum + topicPositions.get(t.topic_id), 0) / linked.get(p.id).length : Number.MAX_SAFE_INTEGER;
      profiles.sort((a, b) => agentCenter(a) - agentCenter(b) || compare(a, b));
    }
    return {profiles, topics, admin:live.filter(t => t.is_admin), linked,
      connections:topics.reduce((sum,t) => sum+t.members.length,0),
      historical:(data.topics || []).filter(t => t.is_historical && !deleted.has(t.topic_id)).length};
  }

  function render(data, helpers = {}) {
    const h = helpers.escapeHTML || escape, icon = helpers.icon || (() => ''), avatar = helpers.agentAvatar || (p => `<span class="atlas-fallback-avatar">${h(String(p.name || '?')[0])}</span>`);
    const stateBadge = helpers.stateBadge || (() => ''), model = layout(data);
    const admin = model.admin.map(t => {
      const ids = new Set((t.members || []).map(m => m.profile_id));
      const members = model.profiles.filter(p => ids.has(p.id));
      return `<div class="atlas-admin"><span class="atlas-admin-icon">${icon('radio')}</span><div class="atlas-admin-copy"><span>管理员频道</span><button type="button" data-action="view-topic" data-id="${h(t.topic_id)}">${h(t.name)}</button><small>${members.length} / ${model.profiles.length} 位成员已加入 · 广播关系单独展示</small></div><div class="atlas-admin-members">${members.slice(0,6).map(p=>avatar(p,'sm')).join('')}${members.length>6?`<span>+${members.length-6}</span>`:''}</div></div>`;
    }).join('');
    const agentNodes = model.profiles.map(p => {
      const joined = model.linked.get(p.id);
      return `<button type="button" class="atlas-node atlas-agent" data-action="view-agent" data-id="${h(p.id)}" data-atlas-agent="${h(p.id)}" aria-label="查看 ${h(p.name)}，已加入 ${joined.length} 个普通话题">${avatar(p,'md')}<span class="atlas-node-copy"><strong>${h(p.name)}</strong><small>${joined.length ? `${joined.length} 个普通话题` : '未加入普通话题'}</small></span>${stateBadge(p)}<span class="atlas-port" aria-hidden="true"></span></button>`;
    }).join('');
    const topicNodes = model.topics.map(t => `<button type="button" class="atlas-node atlas-topic atlas-tone-${tone(t.topic_id)}" data-action="view-topic" data-id="${h(t.topic_id)}" data-atlas-topic="${h(t.topic_id)}" data-atlas-members="${h(JSON.stringify(t.members.map(m=>m.profile_id)))}" aria-label="查看话题 ${h(t.name)}，${t.members.length} 位受管成员"><span class="atlas-port" aria-hidden="true"></span><span class="atlas-topic-symbol">${icon('hash')}</span><span class="atlas-node-copy"><strong>${h(t.name)}</strong><small>${t.members.length} 位受管成员</small><span class="atlas-member-names">${h(t.members.map(m=>m.name).join(' · ') || '暂无受管成员加入')}</span></span><span class="atlas-topic-members">${t.members.slice(0,3).map(m=>avatar(m,'xs')).join('')}${t.members.length>3?`<small>+${t.members.length-3}</small>`:''}</span></button>`).join('');
    return `<section class="panel atlas" aria-label="协作成员关系图"><header class="atlas-header"><div><span class="atlas-kicker">LIVE MEMBERSHIPS</span><h2>谁正在和谁聊</h2><p>沿着连线找到话题，悬停或聚焦节点突出它的关系。</p></div><div class="atlas-metrics"><span><strong>${model.topics.length}</strong>普通话题</span><span><strong>${model.connections}</strong>成员关系</span></div></header>${admin}<div class="atlas-map-scroll"><div class="atlas-map"><svg class="atlas-lines" aria-hidden="true"></svg><section class="atlas-column atlas-agents" aria-label="Agent 节点"><h3><span>${icon('agents')}AGENTS</span><small>${model.profiles.filter(p=>activeStates.has(p.runtime?.state)).length} / ${model.profiles.length} 运行中</small></h3><div class="atlas-node-list">${agentNodes || `<div class="atlas-empty">创建 Agent 后，在这里查看成员关系。</div>`}</div></section><div class="atlas-map-label" aria-hidden="true"><span>加入</span>${icon('arrow')}</div><section class="atlas-column atlas-topics" aria-label="普通 Topic 节点"><h3><span>${icon('hash')}TOPICS</span><small>${model.topics.length} 个当前话题</small></h3><div class="atlas-node-list">${topicNodes || `<div class="atlas-empty">${icon('hash')}<strong>对话还没有开始</strong><span>Agent 加入普通话题后会出现连线。</span></div>`}</div></section></div></div><footer class="atlas-footer"><span><i></i>仅展示真实的当前成员关系</span>${model.historical ? `<a href="#topics">${model.historical} 个历史话题在 Topics 中查看 ${icon('arrow')}</a>` : '<span>点击节点查看详情</span>'}</footer></section>`;
  }

  function draw(root = document) {
    const maps = root.matches?.('.atlas-map') ? [root] : [...root.querySelectorAll('.atlas-map')];
    for (const map of maps) {
      const svg = map.querySelector('.atlas-lines');
      if (!svg) continue;
      svg.replaceChildren();
      const box = map.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      svg.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
      const agentNodes = new Map([...map.querySelectorAll('[data-atlas-agent]')].map(n=>[n.dataset.atlasAgent,n]));
      const topicNodes = [...map.querySelectorAll('[data-atlas-topic]')];
      const nodes = [...agentNodes.values(), ...topicNodes];
      if (box.width >= 640) {
        for (const topic of topicNodes) {
          let members;
          try { members = JSON.parse(topic.dataset.atlasMembers); } catch (_) { members = []; }
          const end = topic.querySelector('.atlas-port').getBoundingClientRect();
          for (const id of members) {
            const agent = agentNodes.get(id);
            if (!agent) continue;
            const start = agent.querySelector('.atlas-port').getBoundingClientRect();
            const x1=start.x+start.width/2-box.x,y1=start.y+start.height/2-box.y,x2=end.x+end.width/2-box.x,y2=end.y+end.height/2-box.y;
            const curve=Math.max(24,(x2-x1)*.52), path=document.createElementNS(svgNS,'path');
            path.setAttribute('d',`M ${x1} ${y1} C ${x1+curve} ${y1}, ${x2-curve} ${y2}, ${x2} ${y2}`);
            path.setAttribute('class',`atlas-edge atlas-tone-${tone(topic.dataset.atlasTopic)}`);
            path.dataset.agent=id;path.dataset.topic=topic.dataset.atlasTopic;
            svg.append(path);
          }
        }
      }
      const highlight = key => {
        const target = nodes.find(n => (n.dataset.atlasAgent ? `agent:${n.dataset.atlasAgent}` : `topic:${n.dataset.atlasTopic}`) === key);
        const agentId=target?.dataset.atlasAgent,topicId=target?.dataset.atlasTopic;
        const edges=[...svg.querySelectorAll('.atlas-edge')];
        const relatedAgents=new Set(),relatedTopics=new Set();
        for (const topic of topicNodes) {
          const members=JSON.parse(topic.dataset.atlasMembers);
          if ((topicId && topic.dataset.atlasTopic===topicId) || (agentId && members.includes(agentId))) {
            relatedTopics.add(topic.dataset.atlasTopic);
            members.forEach(id=>relatedAgents.add(id));
          }
        }
        if(agentId) {relatedAgents.clear();relatedAgents.add(agentId);}
        nodes.forEach(n=>n.classList.toggle('is-dimmed',!!target && !(n.dataset.atlasAgent ? relatedAgents.has(n.dataset.atlasAgent) : relatedTopics.has(n.dataset.atlasTopic))));
        edges.forEach(edge=>{
          const selected=!!target && (agentId ? edge.dataset.agent===agentId : edge.dataset.topic===topicId);
          edge.classList.toggle('is-highlighted',selected);edge.classList.toggle('is-dimmed',!!target&&!selected);
        });
      };
      const identify = node => node?.dataset.atlasAgent ? `agent:${node.dataset.atlasAgent}` : node?.dataset.atlasTopic ? `topic:${node.dataset.atlasTopic}` : null;
      const enter = event => {
        const node=event.target.closest?.('[data-atlas-agent],[data-atlas-topic]');
        if (node) {focusedNode=identify(node);highlight(focusedNode);}
      };
      map.onpointerover=enter;map.onfocusin=enter;
      map.onpointerleave=()=>{focusedNode=identify(document.activeElement);highlight(focusedNode);};
      map.onfocusout=event=>{if(!map.contains(event.relatedTarget)){focusedNode=null;highlight(null);}};
      highlight(identify(document.activeElement) || focusedNode);
    }
  }
  window.AgentTopology = { render, draw, layout };
})();
