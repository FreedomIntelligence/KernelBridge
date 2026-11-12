/* Live trace adapter (KernelBridge /trace and the pi-lattice-trace plugin).
   Injected by the trace server inside the vendored Lattice viewer, just before
   it boots. It replaces the demo session (DS_INV) with the live session (a
   KernelBridge Journal or a Pi session) and follows updates over Server-Sent
   Events. T.labels carries host wording; without it the KernelBridge text applies. */
/* Timeline order of two steps (the viewer compares ids as strings, which only its demo ids satisfy). */
var kbAtOrBefore = function(a, b){
  if (!DS || !DS.live) return a <= b;
  if (kbAtOrBefore.steps !== STEPS){ kbAtOrBefore.steps = STEPS; kbAtOrBefore.order = {}; STEPS.forEach(function(s, i){ kbAtOrBefore.order[s.id] = i; }); }
  var x = kbAtOrBefore.order[a], y = kbAtOrBefore.order[b];
  return x == null || y == null ? a <= b : x <= y;
};

/* Live inputs and markers have no model turn; divergence compares model and tool steps only. */
var kbComparable = function(s){ return !DS || !DS.live || s.src !== 'harness'; };

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
      build:function(){ return {steps:T.steps.map(copy), commits:T.commits.map(copy), FORK:T.fork ? copy(T.fork) : null, RESULT:T.result}; },
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
        return '<div class="cfg"><b>Harness · ' + esc2(label('harness', 'KernelBridge')) + '</b><div>' + esc2(c.adapter || 'pi') + ' · 模型 ' + esc2(T.session.model || '—') + (c.thinking ? ' · 思考 ' + esc2(c.thinking) : '') + '</div><div>上下文上限 ' + kfmt(T.limit) + '，' + esc2(c.compaction || '80% 时压缩') + '</div><div>工具 ' + ((c.tools || []).length) + ' 个 · 执行 ' + esc2(c.execution || '—') + '</div><div>' + '子代理 ' + kids + ' 个（' + esc2(label('childNote', '由宿主准入与记账')) + '）' + '</div></div>'
          + '<div class="cfg"><b>工作区</b><div class="row2"><span>模式</span><span>' + esc2(c.workspace === 'project' ? label('workspace', '项目目录（直接修改）') : c.workspace || '—') + '</span></div><div class="row2"><span>写入 / 修改</span><span>' + T.commits.length + ' 次</span></div><div class="row2"><span>涉及文件</span><span>' + Object.keys(T.files || {}).length + ' 个</span></div><div class="row2"><span>来源</span><span>' + esc2(label('source', 'Journal（只读）')) + '</span></div></div>';
      },
      summaryFor:function(s){ return s.note || '摘要'; },
      compareNote:function(){ return T.fork ? '其他分支从 ' + T.fork.step + '（' + fmtT(T.fork.t) + '）分叉。表中只统计分叉之后的部分。' : '实时会话只有一条分支；/fork、/new 作为标记出现在时间线上。'; }, compareNote2:'', noForkHint:'实时会话没有可对比的 fork 分支。',
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

  /* 进展 › 目标与计划: Goal, plan checklist and its history, completion reviews and
     progress over time, from the live trace. Registered on the viewer's shell. */
  var GOAL_STATUS = {active:'进行中', completed:'已完成', awaiting_user:'等待输入', paused:'已暂停', cleared:'已清除', budget_limit:'达到上限', blocked:'受阻'};
  var REVIEW_STATUS = {met:'达成', 'continue':'继续', blocked:'受阻'};
  function dur(ms){ var s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? s + 's' : Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's'; }
  function checklist(items){
    return '<div class="cfg">' + items.map(function(it){
      var mark = it.status === 'completed' ? '✔' : it.status === 'in_progress' ? '■' : '□';
      var style = it.status === 'completed' ? 'color:var(--muted);text-decoration:line-through' : it.status === 'in_progress' ? 'font-weight:600' : 'color:var(--faint)';
      return '<div style="font-family:var(--mono);font-size:12.5px;padding:2px 0;' + style + '">' + mark + ' ' + esc2(it.step) + '</div>';
    }).join('') + '</div>';
  }
  function curve(P){
    var end = Math.max(MAXT || 0, 1), W = 640, H = 120, L = 34, R = 10, Tm = 10, B = 22;
    function x(t){ return L + Math.min(1, Math.max(0, t / end)) * (W - L - R); }
    function y(f){ return Tm + (1 - f) * (H - Tm - B); }
    function series(points, cls, label){
      if (!points.length) return '';
      var d = 'M' + x(0).toFixed(1) + ' ' + y(0).toFixed(1); var last = 0;
      points.forEach(function(p){ d += ' L' + x(p[0]).toFixed(1) + ' ' + y(last).toFixed(1) + ' L' + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1); last = p[1]; });
      d += ' L' + x(end).toFixed(1) + ' ' + y(last).toFixed(1);
      return '<path d="' + d + '" fill="none" stroke="' + cls + '" stroke-width="1.6"><title>' + esc2(label) + '</title></path>';
    }
    var models = STEPS.filter(function(s){ return s.src === 'model' && s.agent === MAIN; });
    var edits = STEPS.filter(function(s){ return (s.kind === 'commit' || s.kind === 'merge') && s.agent === MAIN; });
    var cum = function(list){ return list.map(function(s, i){ return [s.t1, (i + 1) / list.length]; }); };
    var planPts = P.plans.map(function(p){ return [p.t, p.total ? p.done / p.total : 0]; });
    var g = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="进度曲线">';
    g += '<line x1="' + L + '" y1="' + y(0) + '" x2="' + (W - R) + '" y2="' + y(0) + '" stroke="var(--line)"/><line x1="' + L + '" y1="' + y(1) + '" x2="' + (W - R) + '" y2="' + y(1) + '" stroke="var(--line)" stroke-dasharray="3 3"/>';
    g += '<text x="' + (L - 6) + '" y="' + (y(1) + 4) + '" text-anchor="end" font-size="10" fill="var(--faint)">100%</text><text x="' + (L - 6) + '" y="' + (y(0) + 4) + '" text-anchor="end" font-size="10" fill="var(--faint)">0</text>';
    g += '<text x="' + L + '" y="' + (H - 6) + '" font-size="10" fill="var(--faint)">0:00</text><text x="' + (W - R) + '" y="' + (H - 6) + '" text-anchor="end" font-size="10" fill="var(--faint)">' + fmtT(end) + '</text>';
    g += series(cum(models), 'var(--ink-2)', '模型调用（累计占比）') + series(cum(edits), 'var(--sbx)', '文件改动（累计占比）') + series(planPts, 'var(--good)', '计划完成比例');
    g += '</svg>';
    var legend = [['var(--good)', '计划完成比例', P.plans.length], ['var(--ink-2)', '模型调用 ' + models.length + ' 次', models.length], ['var(--sbx)', '文件改动 ' + edits.length + ' 次', edits.length]]
      .filter(function(l){ return l[2]; }).map(function(l){ return '<span style="margin-right:14px"><span style="display:inline-block;width:14px;height:2px;background:' + l[0] + ';vertical-align:middle;margin-right:5px"></span>' + l[1] + '</span>'; }).join('');
    return g + '<div class="hint" style="margin-top:4px">' + (legend || '还没有模型调用') + '</div>';
  }
  function renderProgress(){
    var el = document.getElementById('p-kbprog'); if (!el) return;
    var P = T.progress || {goal:null, goals:[], reviews:[], plans:[], usage:{modelCalls:0, tokens:0}};
    var G = P.goal, h = '<div class="pad">';
    h += '<p class="subh">目标</p>';
    if (G){
      var ended = G.ended_ns != null, started = Number(G.started_ns);
      var elapsed = isFinite(started) && started > 0 ? dur(ended ? (Number(G.ended_ns) - started) / 1e6 : Date.now() - started / 1e6) : '—';
      var live = G.status === 'active' && !ended && isFinite(started) && started > 0 ? ' data-kb-since="' + started / 1e6 + '"' : '';
      h += '<div class="cfg"><b>' + esc2(G.objective || '（无目标描述）') + '</b>'
        + '<div class="row2"><span>状态</span><span>' + esc2(GOAL_STATUS[G.status] || G.status) + '</span></div>'
        + '<div class="row2"><span>已用时</span><span><span' + live + '>' + elapsed + '</span>' + (live ? '（进行中）' : '') + '</span></div>'
        + '<div class="row2"><span>模型调用</span><span>' + P.usage.modelCalls + (G.max_model_turns ? ' / ' + G.max_model_turns : '') + '</span></div>'
        + '<div class="row2"><span>tokens</span><span>' + kfmt(P.usage.tokens) + (G.token_budget ? ' / ' + kfmt(G.token_budget) : '') + '</span></div>'
        + (G.summary ? '<div class="row2"><span>说明</span><span>' + esc2(G.summary) + '</span></div>' : '') + '</div>';
      var ids = new Set(P.goals.map(function(g){ return g.goal_id; })).size;
      if (ids > 1) h += '<p class="note" style="margin-top:8px">这个会话设置过 ' + ids + ' 个 Goal，这里显示最近一个。</p>';
    } else h += '<p class="note">这个会话没有设置 Goal。下面按模型调用和文件改动显示进度。</p>';
    h += '<p class="subh">计划</p>';
    if (P.plans.length){
      var last = P.plans[P.plans.length - 1];
      h += '<p class="note">当前计划 ' + last.done + '/' + last.total + (last.explanation ? ' · ' + esc2(last.explanation) : '') + '</p>' + checklist(last.items);
      h += '<div class="chain" style="margin-top:8px">' + P.plans.map(function(p, i){
        return '<div class="chain-i" data-goto="' + esc2(p.step) + '"><span class="m">' + (i + 1) + '</span><span>' + fmtT(p.t) + ' · ' + p.done + '/' + p.total + '</span><span class="hint">跳到步骤</span><span>' + esc2(p.explanation || '') + '</span></div>';
      }).join('') + '</div>';
    } else h += '<p class="note">' + (G ? '模型还没有用 update_plan 记录计划。' : '没有计划；计划只在 Goal 模式下由模型记录。') + '</p>';
    if (P.reviews.length){
      h += '<p class="subh">完成审查</p><div class="chain">' + P.reviews.map(function(r){
        return '<div class="chain-i" style="cursor:default"><span class="m">' + fmtT(r.t) + '</span><span>' + esc2(REVIEW_STATUS[r.status] || r.status) + '</span><span></span><span>' + esc2(r.summary) + '</span></div>';
      }).join('') + '</div>';
    }
    h += '<p class="subh">进度曲线</p>' + curve(P);
    var recent = STEPS.filter(function(s){ return s.agent === MAIN && s.src !== 'harness'; }).slice(-6).reverse();
    if (recent.length) h += '<p class="subh">最近活动</p><div class="chain">' + recent.map(function(s){
      return '<div class="chain-i" data-goto="' + esc2(s.id) + '"><span class="m">' + fmtT(s.t0) + '</span><span>' + esc2(s.title) + '</span><span class="hint">' + esc2(ST_LABEL[s.status] || s.status) + '</span></div>';
    }).join('') + '</div>';
    el.innerHTML = h + '</div>';
  }
  (function(){
    PANEL['live-progress'] = 'p-kbprog'; TAB_MODE['live-progress'] = 'analyze';
    var host = document.getElementById('p-overview');
    if (host && !document.getElementById('p-kbprog')){
      var el = document.createElement('div'); el.id = 'p-kbprog'; el.setAttribute('role', 'tabpanel'); el.setAttribute('aria-label', '进展'); el.hidden = true;
      host.parentNode.insertBefore(el, host.nextSibling);
    }
    // The shell has run by now (the adapter is injected just before boot).
    if (window.SHELL && SHELL.SH && SHELL.SH.page) SHELL.SH.page['live-progress'] = renderProgress;
    // A running Goal's elapsed time advances between Journal updates.
    setInterval(function(){
      [].forEach.call(document.querySelectorAll('#p-kbprog:not([hidden]) [data-kb-since]'), function(e){ e.textContent = dur(Date.now() - Number(e.getAttribute('data-kb-since'))); });
    }, 1000);
  })();

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
