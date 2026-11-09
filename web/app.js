/* 医疗知识中枢 · 核心：状态、接口、路由、上下文、实体抽屉 */
(() => {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const esc = s => String(s == null ? '' : s)
    .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const fmt = n => {
    if (n == null) return '—';
    if (n >= 10000) return (n / 10000).toFixed(n >= 100000 ? 0 : 1) + ' 万';
    return n.toLocaleString('zh-CN');
  };

  /* ─────────────────────────── 状态 ─────────────────────────── */
  const S = {
    view: 'ask',
    boot: null,
    ctx: [],                 // 当前上下文实体 [{id,name,type,type_label}]
    ask: null,               // 最近一次问答结果
    asking: false,
    activeCite: null,
    kb: { q: '', kind: 'all', field: '', result: null, activeId: null, doc: null, loading: false },
    graph: { focus: null, selected: null, sub: null, skipMount: false },
    // 会话列表：每个会话 = 一组连续问答 [{id, title, messages:[{q,res}]}]
    // 只存内存，刷新/关停服务即清空。
    sessions: [],
    currentId: null,
    msgSeq: 0,                // 当前会话的消息序号（右侧锚点导航用）
  };

  const typeColor = id => {
    const t = (S.boot && S.boot.types || []).find(x => x.id === id);
    return t ? t.color : '#6b7a8f';
  };
  const typeLabel = id => {
    const t = (S.boot && S.boot.types || []).find(x => x.id === id);
    return t ? t.label : id;
  };

  /* ─────────────────────────── 接口 ─────────────────────────── */
  const OFFLINE = '无法连接后端服务。请在项目目录执行 python server.py，然后访问终端里给出的地址。';

  async function get(path, params) {
    if (location.protocol === 'file:') throw new Error(OFFLINE);
    const u = new URL(path, location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== '' && v != null) u.searchParams.set(k, v);
    });
    const r = await fetch(u, { headers: { Accept: 'application/json' } });
    if (!r.ok) {
      let m = '请求失败';
      try { m = (await r.json()).error || m; } catch (e) { /* ignore */ }
      throw new Error(m);
    }
    return r.json();
  }

  async function post(path, body) {
    if (location.protocol === 'file:') throw new Error(OFFLINE);
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      let m = '请求失败';
      try { m = (await r.json()).error || m; } catch (e) { /* ignore */ }
      throw new Error(m);
    }
    return r.json();
  }

  // 流式 POST：解析 SSE（event: <kind>\ndata: <json>）逐事件回调。
  // onEvent(kind, obj) 每收到一个事件调用一次；返回 Promise，流结束后 resolve。
  // signal 传入 AbortSignal 时，abort 会中断流（用于暂停）。
  async function postStream(path, body, onEvent, signal) {
    if (location.protocol === 'file:') throw new Error(OFFLINE);
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal,
    });
    if (!r.ok) {
      let m = '请求失败';
      try { m = (await r.json()).error || m; } catch (e) { /* ignore */ }
      throw new Error(m);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      // 按空行分隔事件块
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let kind = 'message';
        let data = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) kind = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;
        let obj;
        try { obj = JSON.parse(data); } catch (e) { continue; }
        onEvent(kind, obj);
      }
    }
  }

  /* ─────────────────────────── 提示 ─────────────────────────── */
  let toastT = 0;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(toastT);
    toastT = setTimeout(() => el.classList.remove('on'), 2200);
  }

  /* ─────────────────────── 通用弹窗（确认/输入） ───────────────────────
     openModal({ title, text, input, value, okText }) → Promise
       · input 为真时显示输入框（用于重命名、改问题），resolve 输入文本
       · 否则是二次确认，resolve true / false
  */
  function openModal(opt) {
    return new Promise(resolve => {
      const mask = $('#modalMask');
      const titleEl = $('#modalTitle');
      const textEl = $('#modalText');
      const inputEl = $('#modalInput');
      const okBtn = $('#modalOk');
      const cancelBtn = $('#modalCancel');
      if (!mask) { resolve(false); return; }

      titleEl.textContent = opt.title || '确认';
      textEl.textContent = opt.text || '';
      textEl.hidden = !opt.text;
      const withInput = !!opt.input;
      inputEl.hidden = !withInput;
      inputEl.value = opt.value || '';
      okBtn.textContent = opt.okText || '确定';
      okBtn.classList.toggle('danger', !!opt.danger);

      mask.hidden = false;
      if (withInput) { setTimeout(() => { inputEl.focus(); inputEl.select(); }, 30); }
      else { setTimeout(() => okBtn.focus(), 30); }

      const done = val => {
        mask.hidden = true;
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        mask.removeEventListener('click', onMask);
        document.removeEventListener('keydown', onKey);
        resolve(val);
      };
      const onOk = () => {
        if (withInput) {
          const v = inputEl.value.trim();
          if (!v) { inputEl.focus(); return; }
          done(v);
        } else {
          done(true);
        }
      };
      const onCancel = () => done(withInput ? null : false);
      const onMask = e => { if (e.target === mask) onCancel(); };
      const onKey = e => {
        if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
        else if (e.key === 'Enter' && withInput && !e.shiftKey) { e.preventDefault(); onOk(); }
      };

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      mask.addEventListener('click', onMask);
      document.addEventListener('keydown', onKey);
    });
  }

  /* ─────────────────────────── 路由 ─────────────────────────── */
  function go(view, silent) {
    if (!['ask', 'kb', 'graph'].includes(view)) view = 'ask';
    S.view = view;
    closeDrawer();
    if (!silent && location.hash !== '#' + view) {
      try { history.replaceState(null, '', '#' + view); } catch (e) { /* file:// 下忽略 */ }
    }

    $$('#viewNav button').forEach(b => b.classList.toggle('on', b.dataset.view === view));
    moveThumb();

    $$('.view').forEach(v => { v.hidden = v.id !== 'view-' + view; });

    // 知识库页面左侧已有专属检索框，顶栏全局搜索框在这里收起，避免两个搜索框并列。
    const gs = $('#gsearch');
    if (gs) gs.hidden = (view === 'kb');

    // 切走问答视图时如果还在录音，直接停掉，避免麦克风一直开着
    if (view !== 'ask' && window.Voice) window.Voice.silence();

    if (view === 'kb' && window.KB.render) window.KB.render();
    // 深链直接点名了实体时不要先画一遍全览：那会先铺满节点再收缩到目标，
    // 视觉上等于闪两下。让 focus() 一次性把目标图取回来。
    if (view === 'graph' && !S.graph.skipMount) GraphView.mount();
    if (view === 'ask') scrollStream();
  }

  function moveThumb() {
    const nav = $('#viewNav');
    const on = $('button.on', nav);
    const th = $('.vthumb', nav);
    if (!on || !th) return;
    th.style.width = on.offsetWidth + 'px';
    th.style.transform = `translateX(${on.offsetLeft - 3}px)`;
  }

  /* ─────────────────────────── 上下文实体 ─────────────────────────── */
  function addCtx(ent) {
    if (!ent || !ent.id) return;
    const i = S.ctx.findIndex(x => x.id === ent.id);
    if (i >= 0) {
      S.ctx[i] = Object.assign({}, S.ctx[i], ent);
    } else {
      S.ctx.unshift({
        id: ent.id,
        name: ent.name || ent.n || ent.id,
        type: ent.type || ent.t || '',
        type_label: ent.type_label || typeLabel(ent.type || ent.t || ''),
      });
      if (S.ctx.length > 14) S.ctx.length = 14;
    }
    renderCtx();
  }

  function dropCtx(id) {
    S.ctx = S.ctx.filter(x => x.id !== id);
    renderCtx();
  }

  function renderCtx() {
    const list = $('#ctxList');
    list.innerHTML = S.ctx.map(e => `
      <div class="ent-chip" data-ent="${esc(e.id)}">
        <span class="cd" style="background:${typeColor(e.type)}"></span>
        <span class="cn">${esc(e.name)}</span>
        <span class="cx" data-drop="${esc(e.id)}" title="从上下文移除">✕</span>
      </div>`).join('');
    $('#ctxEmpty').hidden = S.ctx.length > 0;
    const st = S.boot ? S.boot.stats : null;
    $('#ctxFoot').innerHTML = st
      ? `<b>${fmt(st.entities)}</b> 实体 · <b>${fmt(st.edges)}</b> 关系<br><b>${fmt(st.docs)}</b> 段正文 · <b>${fmt(Math.round(st.doc_chars / 10000))}</b> 万字`
      : '';
  }

  /* ─────────────────────────── 实体抽屉 ─────────────────────────── */
  let drawerToken = 0;

  /* 顶栏搜索是唯一的搜索框：选中后直接进「以它为中心」的图谱，
     右侧详情同步。以前这里弹的是抽屉，和图谱里那个搜索框功能重复。 */
  async function openInGraph(id) {
    const sug = $('#gsug');
    if (sug) sug.hidden = true;
    const inp = $('#gq');
    if (inp) inp.blur();
    S.graph.skipMount = true;
    go('graph');
    S.graph.skipMount = false;
    if (window.GraphView && window.GraphView.enterEgo) {
      await window.GraphView.enterEgo(id);
    } else {
      toast('图谱还没准备好，稍后再试');
    }
  }

  async function openEntity(id) {
    const dr = $('#drawer');
    dr.hidden = false;
    $('#drHead').innerHTML = `<div class="dt"><span class="pill neutral">加载中</span></div><h2>${esc(id)}</h2>`;
    $('#drBody').innerHTML = `<div class="loading"><span class="spin"></span>正在读取实体与关联正文…</div>`;
    $('#drFoot').innerHTML = '';

    const token = ++drawerToken;
    let d;
    try {
      d = await get('/api/entity', { id });
    } catch (err) {
      $('#drBody').innerHTML = `<div class="banner danger"><b>读取失败</b><span>${esc(err.message)}</span></div>`;
      return;
    }
    if (token !== drawerToken) return;

    addCtx({ id: d.id, name: d.name, type: d.type, type_label: d.type_label });

    const props = Object.entries(d.props || {});
    const propLabel = {
      legal_class: '法定分类', cfr: '病死率', incubation: '潜伏期', zoonotic: '动物源性',
      vaccine_preventable: '疫苗可预防', department: '就诊科室', icd10: 'ICD-10',
      en_name: '英文名', drug_class: '药物类别', route: '给药途径', form: '剂型',
      brand_name: '商品名', manufacturer: '生产商', has_label: '有说明书',
    };

    $('#drHead').innerHTML = `
      <button class="dr-x" data-close aria-label="关闭">✕</button>
      <div class="dt">
        <span class="pill" style="background:${typeColor(d.type)}18;color:${typeColor(d.type)}">${esc(d.type_label)}</span>
        ${d.system ? `<span class="pill ghost">${esc(d.system)}</span>` : ''}
        ${d.clinic ? `<span class="pill ghost">临床图谱</span>` : ''}
      </div>
      <h2>${esc(d.name)}</h2>
      ${d.aliases && d.aliases.length
        ? `<div class="dsub">又称：${esc(d.aliases.slice(0, 6).join('、'))}</div>` : ''}
      ${d.summary ? `<div class="dsub" style="margin-top:6px">${esc(d.summary)}</div>` : ''}`;

    const relHtml = (d.relations || []).slice(0, 8).map(g => `
      <div class="sec">
        <div class="sec-h">${esc(g.label)}<span class="cnt">${g.total}</span></div>
        ${g.items.map(it => `
          <div class="rel-row" data-ent="${esc(it.other)}">
            <span class="rp">${it.dir === 'out' ? '→' : '←'}</span>
            <span class="rn">${esc(it.other_name)}</span>
            <span class="rt">${esc(typeLabel(it.other_type))}</span>
          </div>`).join('')}
      </div>`).join('');

    const docsHtml = (d.docs || []).length
      ? d.docs.slice(0, 8).map(x => `
        <div class="doc-mini" data-doc="${esc(x.id)}">
          <b>${esc(x.title)}</b>
          <i>${esc(x.field_label)} · ${esc(x.source)} · ${x.chars} 字</i>
          ${x.excerpt ? `<p>${esc(x.excerpt)}</p>` : ''}
        </div>`).join('')
      : `<div class="ctx-empty" style="padding:0 2px">该实体在正文库中没有专门章节，问答时会使用它的「图谱档案」。</div>`;

    $('#drBody').innerHTML = `
      ${props.length ? `<div class="sec">
        <div class="sec-h">属性</div>
        <dl class="kv">${props.slice(0, 10).map(([k, v]) => `
          <dt>${esc(propLabel[k] || k)}</dt><dd>${esc(typeof v === 'boolean' ? (v ? '是' : '否') : v)}</dd>`).join('')}
        </dl></div>` : ''}
      <div class="sec">
        <div class="sec-h">关联正文<span class="cnt">${d.doc_total || 0} 段</span></div>
        ${docsHtml}
      </div>
      ${relHtml}
      ${d.source ? `<div class="sec"><div class="sec-h">来源</div>
        <div class="dsub">${esc(d.source)}${d.url ? ` · <a href="${esc(d.url)}" target="_blank" rel="noopener">原始链接</a>` : ''}</div></div>` : ''}`;

    $('#drFoot').innerHTML = `
      <button class="btn primary" data-act="ask">用这个实体提问</button>
      <button class="btn" data-act="graph">在图谱中定位</button>`;

    $('#drFoot').dataset.ent = d.id;
    $('#drFoot').dataset.name = d.name;
  }

  function closeDrawer() { $('#drawer').hidden = true; }

  /* ─────────────────────────── 全局搜索 ─────────────────────────── */
  function bindSearch(input, box, onPick) {
    let t = 0, items = [], act = -1;

    function paint() {
      if (!items.length) {
        box.innerHTML = `<div class="s-empty">没有匹配的实体</div>`;
        box.hidden = false;
        return;
      }
      box.innerHTML = items.map((x, i) => `
        <div class="s-item${i === act ? ' act' : ''}" data-i="${i}">
          <span class="cd" style="width:7px;height:7px;border-radius:2px;background:${typeColor(x.type)}"></span>
          <b>${esc(x.name)}</b><i>${esc(x.type_label)}${x.sys ? ' · ' + esc(x.sys) : ''}</i>
        </div>`).join('');
      box.hidden = false;
    }

    async function search() {
      const q = input.value.trim();
      if (!q) { box.hidden = true; return; }
      try {
        const r = await get('/api/suggest', { q, limit: 8 });
        items = r.items || [];
        act = items.length ? 0 : -1;
        paint();
      } catch (e) { box.hidden = true; }
    }

    input.addEventListener('input', () => { clearTimeout(t); t = setTimeout(search, 110); });
    input.addEventListener('focus', () => { if (input.value.trim()) search(); });
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); act = Math.min(items.length - 1, act + 1); paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); act = Math.max(0, act - 1); paint(); }
      else if (e.key === 'Enter') {
        if (act >= 0 && items[act]) { e.preventDefault(); pick(items[act]); }
      } else if (e.key === 'Escape') { box.hidden = true; input.blur(); }
    });
    box.addEventListener('mousedown', e => {
      const it = e.target.closest('[data-i]');
      if (it) { e.preventDefault(); pick(items[+it.dataset.i]); }
    });
    document.addEventListener('click', e => {
      if (!input.contains(e.target) && !box.contains(e.target)) box.hidden = true;
    });

    function pick(x) {
      box.hidden = true;
      input.value = '';
      onPick(x);
    }
  }

  /* ─────────────────────────── 初始化 ─────────────────────────── */
  async function boot() {
    bindSearch($('#gq'), $('#gsug'), x => openInGraph(x.id));

    // 背景抽屉由 bg-theme.js 自己渲染，这里只负责把按钮接上
    const bgBtn = $('#bgBtn');
    if (bgBtn) bgBtn.onclick = () => { if (window.BGTheme) window.BGTheme.open(); };

    $('#viewNav').addEventListener('click', e => {
      const b = e.target.closest('button[data-view]');
      if (b) go(b.dataset.view);
    });
    window.addEventListener('resize', () => { moveThumb(); GraphView.resize(); });

    $('#ctxToggle').addEventListener('click', () => {
      $('#ctx').classList.toggle('min');
      setTimeout(moveThumb, 280);
    });

    $('#ctxList').addEventListener('click', e => {
      const drop = e.target.closest('[data-drop]');
      if (drop) { e.stopPropagation(); dropCtx(drop.dataset.drop); return; }
      const chip = e.target.closest('[data-ent]');
      if (chip) openEntity(chip.dataset.ent);
    });

    $('#drawer').addEventListener('click', e => {
      if (e.target.closest('[data-close]')) { closeDrawer(); return; }
      const rel = e.target.closest('[data-ent]');
      if (rel && rel.closest('#drBody')) { openEntity(rel.dataset.ent); return; }
      const doc = e.target.closest('[data-doc]');
      if (doc) {
        closeDrawer();
        go('kb');
        KB.openDoc(doc.dataset.doc, true);
        return;
      }
      const act = e.target.closest('[data-act]');
      if (act) {
        const id = $('#drFoot').dataset.ent;
        const name = $('#drFoot').dataset.name;
        if (act.dataset.act === 'ask') {
          closeDrawer();
          go('ask');
          $('#q').value = `${name}是什么？`;
          $('#q').focus();
        } else {
          closeDrawer();
          go('graph');
          GraphView.focus(id, true);
        }
      }
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') { closeDrawer(); $('#gsug').hidden = true; }
      if (e.key === '/' && !/input|textarea/i.test(document.activeElement.tagName)) {
        e.preventDefault();
        $('#gq').focus();
      }
    });

    // 滚动定位：用户上滑翻历史时，流式输出不再强行把页面顶回底部
    const streamEl = $('#stream');
    const scrollDownBtn = $('#scrollDown');
    const msgNav = $('#msgNav');

    // 向下箭头定位到 composer 上方
    function placeScrollDown() {
      if (!scrollDownBtn) return;
      const composer = document.querySelector('#view-ask .composer');
      if (composer) scrollDownBtn.style.bottom = (composer.offsetHeight + 10) + 'px';
    }

    if (streamEl) {
      streamEl.addEventListener('scroll', () => {
        const nearBottom = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 40;
        streamEl.dataset.pinned = nearBottom ? 'true' : 'false';
        // 向下箭头：不在底部时显示
        if (scrollDownBtn) scrollDownBtn.classList.toggle('show', !nearBottom);
        // 高亮当前消息锚点
        if (msgNav) highlightMsgDot();
      });
    }

    // 高亮当前可见区域对应的消息锚点
    function highlightMsgDot() {
      if (!msgNav || !streamEl) return;
      const dots = msgNav.querySelectorAll('.msg-dot');
      if (!dots.length) return;
      const streamTop = streamEl.getBoundingClientRect().top;
      let active = null;
      dots.forEach(d => {
        const target = document.getElementById(d.dataset.target);
        if (!target) return;
        // 目标消息顶部已滚到视口顶部上方即视为当前（最后一条满足的即最新）
        if (target.getBoundingClientRect().top <= streamTop + 120) active = d;
      });
      dots.forEach(d => d.classList.toggle('on', d === active));
    }

    if (scrollDownBtn) {
      scrollDownBtn.addEventListener('click', () => {
        // 强制滚到底部（绕过 pinned 检查），并恢复跟随滚动
        if (streamEl) {
          streamEl.dataset.pinned = 'true';
          streamEl.scrollTop = streamEl.scrollHeight;
        }
      });
    }

    // 锚点导航点击跳转
    if (msgNav) {
      msgNav.addEventListener('click', e => {
        const dot = e.target.closest('.msg-dot');
        if (!dot) return;
        const target = document.getElementById(dot.dataset.target);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }

    placeScrollDown();
    window.addEventListener('resize', () => { placeScrollDown(); });
    // composer 高度变化（chips 隐藏、录音条显示）时重新定位向下箭头
    const composerEl = document.querySelector('#view-ask .composer');
    if (composerEl && typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => placeScrollDown()).observe(composerEl);
    }

    // 连后端。原来这里还有个「已连接」小绿灯，但本地单机应用里页面能开就说明服务在跑，
    // 这个灯的信息量等于 0，已经去掉。
    try {
      S.boot = await get('/api/bootstrap');
      renderCtx();
      renderExamples();
      renderWelcome();
      moveThumb();
      routeFromHash();
      window.addEventListener('hashchange', () => routeFromHash());

    } catch (err) {
      $('#stream').innerHTML = `<div class="stream-inner"><div class="banner danger">
        <b>后端未连接</b><span>${esc(err.message)}</span></div></div>`;
      console.warn(err);
    }
  }

  function renderExamples() {    const box = $('#examples');
    if (!box || !S.boot) return;
    box.innerHTML = (S.boot.examples || []).map(x =>
      `<button data-q="${esc(x.q)}" title="${esc(x.tag)}">${esc(x.q)}</button>`).join('');
  }

  function scrollStream() {
    const s = $('#stream');
    if (!s) return;
    // 用户主动上滑查看历史时不强行拉回底部；只有本来就贴在底部时才跟随滚动
    if (s.dataset.pinned === 'false') return;
    s.scrollTop = s.scrollHeight;
  }

  function renderWelcome() { Views.renderWelcome(); }

  /* 深链接：#kb?q=二甲双胍&src=db / #graph?center=登革热 / #ask?q=…
     首次加载和手动改 hash 都走同一套解析，保证链接可分享、可复现。 */
  function routeFromHash() {
    const raw = (location.hash || '').replace(/^#/, '');
    const [v0, qs0] = raw.split('?');
    const p0 = new URLSearchParams(qs0 || '');
    const view = ['ask', 'kb', 'graph'].includes(v0) ? v0 : 'ask';
    const center = p0.get('center') || '';
    // #graph?ego=DIS_偏头痛：直接进「以它为中心」的关系图，链接可分享
    const ego = p0.get('ego') || '';
    if (view === 'kb') {
      S.kb.q = p0.get('q') || '';
      const inp = $('#kbq');
      if (inp) inp.value = S.kb.q;
    }
    S.graph.skipMount = view === 'graph' && !!(center || ego);
    go(view, true);
    S.graph.skipMount = false;
    const q0 = p0.get('q') || '';
    // #kb?doc=db:drug:DIS_X:dosage 直接打开某一节，方便分享与核对
    const docId = p0.get('doc') || '';
    if (view === 'kb' && docId && window.KB.openDoc) window.KB.openDoc(docId);
    if (view === 'ask' && q0 && window.Views.ask) window.Views.ask(q0);
    if (view === 'graph' && ego && window.GraphView.enterEgo) window.GraphView.enterEgo(ego);
    else if (view === 'graph' && center) window.GraphView.focus(center, true);
  }

  window.H = {
    S, $, $$, esc, fmt, get, post, postStream, toast, openModal, go, addCtx, dropCtx, renderCtx,
    openEntity, closeDrawer, typeColor, typeLabel, scrollStream, OFFLINE,
  };

  window.Views = {};
  window.KB = {};
  window.GraphView = { mount() {}, resize() {}, focus() {} };

  document.addEventListener('DOMContentLoaded', boot);
})();
