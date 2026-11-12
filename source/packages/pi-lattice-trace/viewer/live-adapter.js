/* Live trace adapter (KernelBridge /trace and the pi-lattice-trace plugin).
   Injected by the trace server inside the vendored Lattice viewer, just before
   it boots. It replaces the demo session (DS_INV) with the live session (a
   KernelBridge Journal or a Pi session) and follows updates over Server-Sent
   Events. T.labels carries host wording; without it the KernelBridge text applies. */
var KB_LIVE = (function(){
  var source = document.getElementById('kb-trace'), T = JSON.parse(source.textContent), base = location.pathname.replace(/[^/]*$/, '');
  var connected = false, pending = null, lastApply = 0, timer = null;
  KIND_LABEL.input = '用户输入'; KIND_LABEL.goal = KIND_LABEL.goal || 'Goal 续跑';
  ST_LABEL.running = '进行中';

  function copy(s){ var c = {}; for (var k in s) c[k] = s[k]; return c; }
  function esc2(v){ return String(v == null ? '' : v).replace(/[&<>"]/g, function(c){ return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;'}[c]; }); }
  function kfmt(n){ return n >= 1000 ? (n / 1000).toFixed(n >= 100000 ? 0 : 1) + 'k' : String(n); }
  function stamp(ns){ if (!ns) return '—'; var d = new Date(ns / 1e6); function p(n){ return String(n).padStart(2, '0'); } return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()); }

  function label(key, fallback){ return T.labels && T.labels[key] ? T.labels[key] : fallback; }

  function dataset(T){
    var files = T.files && Object.keys(T.files).length ? T.files : {'（本会话未写文件）': [['这次会话没有成功的写入或修改调用。', '—']]};
    return {
      id:'invoice', live:true, root:'M', AG:T.agents, ROWS:T.rows, BR:T.branches, LIMIT:T.limit, THRESH:T.thresh, BUDGET:T.budget || 0, minPps:0,
      build:function(){ return {steps:T.steps.map(copy), commits:T.commits.map(copy), FORK:null, RESULT:T.result}; },
      files:files, lanes:T.rows.slice(), txt:{TXT:{}, THOUGHT:{}, PATCH:{}, SUMMARY:{}}, attr:[], decDef:[], stratMock:{},
      ckpt:'—', baseCommit:'—', baseLabel:'会话开始前的工作区', forkHead:'—', mainBase:0, subBase:0, histStep:30, cmpStep:15,
      prefixNote:'', meta:{session:T.session.id || '—', harness:label('harness', 'KernelBridge · Pi'), model:T.session.model || '—'},
      // The first request's prompt minus what the lane adds itself (inputs, the child's brief).
      baseCtx:function(k){
        var lane = T.steps.filter(function(s){ return s.agent === k; }), first = null, before = 0;
        for (var i = 0; i < lane.length && !first; i++){ if (lane[i].src === 'model') first = lane[i]; else before += lane[i].addTok || 0; }
        var total = first ? first.tokIn : 0;
        if (k === 'M/main') return [['system', Math.max(0, total - before), '系统提示、工具定义与项目说明']];
        var brief = T.agents[k].task || '', tok = Math.min(total, Math.max(1, Math.ceil(brief.length / 4)));
        return [['system', Math.max(0, total - tok), '子代理系统提示与工具定义（' + T.agents[k].short + '）'], ['task', tok, brief]];
      },
      configHTML:function(){
        var c = T.config || {}, kids = T.rows.length - 1;
        return '<div class="cfg"><b>Harness · ' + esc2(label('harness', 'KernelBridge')) + '</b><div>' + esc2(c.adapter || 'pi') + ' · 模型 ' + esc2(T.session.model || '—') + (c.thinking ? ' · 思考 ' + esc2(c.thinking) : '') + '</div><div>上下文上限 ' + kfmt(T.limit) + '，' + esc2(c.compaction || '80% 时压缩') + '</div><div>工具 ' + ((c.tools || []).length) + ' 个 · 执行 ' + esc2(c.execution || '—') + '</div><div>' + (T.labels && T.labels.childNote ? esc2(T.labels.childNote) : '子代理 ' + kids + ' 个（由宿主准入与记账）') + '</div></div>'
          + '<div class="cfg"><b>工作区</b><div class="row2"><span>模式</span><span>' + esc2(c.workspace === 'project' ? label('workspace', '项目目录（直接修改）') : c.workspace || '—') + '</span></div><div class="row2"><span>写入 / 修改</span><span>' + T.commits.length + ' 次</span></div><div class="row2"><span>涉及文件</span><span>' + Object.keys(T.files || {}).length + ' 个</span></div><div class="row2"><span>来源</span><span>' + esc2(label('source', 'Journal（只读）')) + '</span></div></div>';
      },
      summaryFor:function(s){ return s.note || '摘要'; },
      compareNote:function(){ return '实时会话只有一条分支；/fork、/new 作为标记出现在时间线上。'; }, compareNote2:'', noForkHint:'实时会话没有可对比的 fork 分支。',
      filesNote:label('filesNote', '项目模式不快照工作区；文件内容由本会话的写入与修改调用重建，只覆盖这些调用涉及的部分。'),
      sideBoxes:function(){ return ''; },
      initialSel:function(){ return STEPS.length ? STEPS[STEPS.length - 1].id : null; }
    };
  }

  function bar(){
    var s = T.session, kids = T.rows.length - 1;
    ISB.crumbs = '<span>' + esc2((s.id || '').replace(/^application-/, '').slice(0, 8)) + '</span><span>' + esc2(label('harness', 'KernelBridge · Pi')) + '</span><span>' + esc2(s.model || '—') + '</span>'
      + '<span class="kb-live' + (connected ? ' on' : '') + '">' + (connected ? (s.status === 'running' ? '● 实时 · 运行中' : '● 实时 · 空闲') : '○ 已断开 · 重连中') + '</span>';
    ISB.eyebrow = label('eyebrow', 'KernelBridge · 实时会话轨迹');
    ISB.title = s.title;
    ISB.meta = ['开始 ' + stamp(s.started_ns), s.cwd ? '目录 ' + s.cwd.replace(/^\/Users\/[^/]+/, '~') : null, '上下文上限 ' + kfmt(T.limit), kids ? kids + ' 个子代理' : null, T.steps.length + ' 步']
      .filter(Boolean).map(function(x){ return '<span>' + esc2(x) + '</span>'; }).join('');
    if (state.project === 'invoice') renderISessionbar();
  }

  function apply(next){
    var follow = !state.sel || !STEPS.length || state.sel === STEPS[STEPS.length - 1].id;
    var keep = {sel:state.sel, vis:state.vis, zoom:state.zoom, open:state.open, repOpen:state.repOpen, scope:state.scope};
    T = next; DS_INV = dataset(T); bar();
    if (state.project !== 'invoice') return;
    loadSession(DS_INV);
    state.zoom = keep.zoom; state.open = keep.open || {}; state.repOpen = keep.repOpen || {};
    T.rows.forEach(function(r){ if (keep.vis && r in keep.vis) state.vis[r] = keep.vis[r]; });
    if (!follow && BYID[keep.sel]) state.sel = keep.sel;
    renderAll();
    if (follow && typeof scrollRow === 'function' && state.tab === 'timeline') try { scrollRow(state.sel); } catch (e) {}
  }
  // Coalesce bursts: at most two renders per second, always ending on the newest trace.
  function schedule(next){
    pending = next; if (timer) return;
    var wait = Math.max(0, 500 - (Date.now() - lastApply));
    timer = setTimeout(function(){ timer = null; lastApply = Date.now(); var t = pending; pending = null; try { apply(t); } catch (err) { console.error('[kernelbridge] live update', err); } }, wait);
  }

  function connect(){
    if (!window.EventSource) return;
    var es = new EventSource(base + 'events');
    es.addEventListener('trace', function(e){ connected = true; schedule(JSON.parse(e.data)); });
    es.onopen = function(){ connected = true; bar(); };
    es.onerror = function(){ connected = false; bar(); };
  }

  /* Provider-reported prompt-cache hits replace the viewer's prefix simulation;
     without a report the simulated prefix is capped at the real prompt size. */
  var baseLoad = loadSession;
  loadSession = function(d){
    baseLoad(d);
    if (d && d.live) STEPS.forEach(function(s){ if (s.src === 'model') s.cacheRead = s.cacheReal != null ? s.cacheReal : Math.min(s.cacheRead || 0, s.tokIn || 0); });
  };

  /* The live page shows one session: drop the demo studies from the switchers. */
  runSwitchHTML = function(){ return ''; };
  var style = document.createElement('style');
  style.textContent = '.sh-railsw [data-project="intent"], .sh-railsw [data-project="kernel"], [data-project="intent"], [data-project="kernel"] { display:none !important; }'
    + '.kb-live { color: var(--warn, #b26b00); } .kb-live.on { color: var(--ok, #2f7d4f); }';
  document.head.appendChild(style);
  if (!/^#invoice(--|$)/.test(location.hash)) { try { history.replaceState(null, '', '#invoice--timeline'); } catch (e) {} }

  DS_INV = dataset(T); bar();
  setTimeout(connect, 0);
  return {trace:function(){ return T; }, apply:apply};
})();
