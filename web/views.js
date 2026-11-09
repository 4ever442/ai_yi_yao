/* 医疗知识中枢 · 视图渲染：问答 / 知识库 / 概览 */
(() => {
  'use strict';

  const H = window.H;
  const { S, $, $$, esc, fmt, get, post, postStream, toast, go, addCtx, typeColor, typeLabel } = H;

  /* ══════════════════════ 通用 ══════════════════════ */

  // sqrt 缩放：类型分布里"疾病"是 1132、最少的只有 4，线性画出来其他全看不见
  function bars(items, colorFn, sqrt) {
    const top = Math.max(...items.map(x => x.count), 1);
    const norm = c => (sqrt ? Math.sqrt(c) / Math.sqrt(top) : c / top);
    return `<div class="bars">${items.map(x => `
      <div class="bar">
        <span class="nm" title="${esc(x.label || x.name)}">${esc(x.label || x.name)}</span>
        <span class="tr"><i style="width:${Math.max(2, norm(x.count) * 100)}%;background:${colorFn(x)}"></i></span>
        <span class="vv">${fmt(x.count)}</span>
      </div>`).join('')}</div>`;
  }

  const Views = window.Views;
  const KB = window.KB;

  /* ══════════════════════ 问答 ══════════════════════ */

  Views.renderWelcome = function () {
    const st = S.boot ? S.boot.stats : null;
    // is-welcome 让整块欢迎区在对话区里垂直居中，否则它贴在顶部、下面留一大片空
    $('#stream').classList.add('is-welcome');
    $('#stream').innerHTML = `
      <div class="stream-inner">
        <div class="welcome">
          <h2>问一个具体的健康问题</h2>
          <p>系统会先把问题里的词锚定到知识图谱的实体上，再按问题类型去知识库里找对应章节，
             最后逐句校验每一个结论有没有证据支撑。没有证据的时候它宁可不答。</p>
          <div class="wgrid">
            <div class="wcard" data-sample="二甲双胍一天吃多少？有哪些禁忌？">
              <b>用药细节</b><i>图谱锚定到药物，再取说明书里的用法与禁忌章节</i></div>
            <div class="wcard" data-sample="最近老是口渴、多尿、体重下降，是不是糖尿病？">
              <b>症状鉴别</b><i>只有症状没有病名时，用图谱关系反推候选疾病</i></div>
            <div class="wcard" data-sample="登革热是怎么传播的？">
              <b>图谱事实</b><i>传染病实体没有正文章节，直接用图谱结构作答</i></div>
          </div>
          ${st ? `<p class="wfoot">
            对话检索语料：${fmt(st.entities)} 个实体 · ${fmt(st.edges)} 条关系 ·
            ${fmt(st.docs)} 段正文 / ${fmt(Math.round(st.doc_chars / 10000))} 万字</p>` : ''}
        </div>
      </div>`;
  };

  // 行内 markdown：**加粗**、*斜体*、`行内代码`、[文字](链接)
  function mdInline(s) {
    let out = esc(s);
    out = out.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    out = out.replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, '$1<i>$2</i>');
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return out;
  }

  function renderBody(text) {
    const DISCLAIMERS = ['以上内容整理自公开医学资料', '以上为资料摘录', '本系统仅'];
    const lines = String(text || '').split('\n');
    const out = [];
    let listBuf = null;   // 当前列表类型 'ul' | 'ol'
    let para = [];
    let table = null;     // 当前表格 { rows: [[cell,...],...] }
    let quote = [];       // 当前引用块内容行
    let codeBuf = null;   // 代码块缓冲

    const flushList = () => {
      if (listBuf) { out.push(`<${listBuf.tag}>${listBuf.items.join('')}</${listBuf.tag}>`); listBuf = null; }
    };
    const flushPara = () => {
      if (para.length) { out.push(`<p class="ans-p">${para.map(mdInline).join('<br>')}</p>`); para = []; }
    };
    const flushTable = () => {
      if (table && table.rows.length) {
        const [head, ...body] = table.rows;
        out.push(`<div class="md-table"><table><thead><tr>${head.map(c => `<th>${mdInline(c)}</th>`).join('')}</tr></thead>`
          + `<tbody>${body.map(r => `<tr>${r.map(c => `<td>${mdInline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      }
      table = null;
    };
    const flushQuote = () => {
      if (quote.length) {
        out.push(`<blockquote class="md-quote">${quote.map(l => `<p>${mdInline(l)}</p>`).join('')}</blockquote>`);
        quote = [];
      }
    };
    const flushAll = () => { flushPara(); flushList(); flushTable(); flushQuote(); };

    const isTableSep = l => /^\|?[\s:|-]+\|[\s:|-]*$/.test(l) && l.includes('-') && l.includes('|');
    const splitRow = l => l.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());

    for (const raw of lines) {
      // 代码块 ``` ... ```
      if (raw.trim().startsWith('```')) {
        if (codeBuf === null) { flushAll(); codeBuf = []; }
        else { out.push(`<pre class="md-code">${esc(codeBuf.join('\n'))}</pre>`); codeBuf = null; }
        continue;
      }
      if (codeBuf !== null) { codeBuf.push(raw); continue; }

      const l = raw.trim();
      if (!l) { flushAll(); continue; }
      if (DISCLAIMERS.some(d => l.startsWith(d))) { flushAll(); out.push(`<div class="ans-tail">${mdInline(l)}</div>`); continue; }
      // 水平分割线
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(l)) { flushAll(); out.push(`<hr class="md-hr">`); continue; }
      // 标题
      const h = l.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flushAll(); out.push(`<div class="ans-h${h[1].length}">${mdInline(h[2])}</div>`); continue; }
      // 引用块
      if (l.startsWith('>')) { flushPara(); flushList(); quote.push(l.replace(/^>\s?/, '')); continue; }
      // 表格行
      if (l.startsWith('|') && l.endsWith('|')) {
        if (isTableSep(l)) { /* 分隔行：忽略 */ continue; }
        flushPara(); flushList(); flushQuote();
        if (!table) table = { rows: [] };
        table.rows.push(splitRow(l));
        continue;
      }
      // 无序列表
      const ul = l.match(/^[-*•]\s+(.*)$/);
      if (ul) { flushPara(); flushQuote(); if (!listBuf || listBuf.tag !== 'ul') { flushList(); listBuf = { tag: 'ul', items: [] }; } listBuf.items.push(`<li>${mdInline(ul[1])}</li>`); continue; }
      // 有序列表
      const ol = l.match(/^\d+[.、)]\s+(.*)$/);
      if (ol) { flushPara(); flushQuote(); if (!listBuf || listBuf.tag !== 'ol') { flushList(); listBuf = { tag: 'ol', items: [] }; } listBuf.items.push(`<li>${mdInline(ol[1])}</li>`); continue; }
      // 普通段落
      para.push(l);
    }
    flushAll();
    if (codeBuf !== null) out.push(`<pre class="md-code">${esc(codeBuf.join('\n'))}</pre>`);
    return out.join('');
  }

  function hl(text, hits) {
    let out = esc(text);
    (hits || []).forEach(t => {
      if (!t || t.length < 2) return;
      out = out.split(t).join(`\u0001${t}\u0002`);
    });
    return out.replace(/\u0001/g, '<mark>').replace(/\u0002/g, '</mark>');
  }

  Views.renderAnswer = function (res, idx, m) {
    const banners = [];
    if (res.emergency) {
      banners.push(`<div class="banner danger"><b>急症优先</b><span>识别到警示症状（${esc((res.red_flags || []).join('、'))}），已用急救指引替换常规回答</span></div>`);
    }
    // 超出知识库（模型自行作答）时提示用户自行甄别：顶部 + 底部各一处
    const oobBanner = `<div class="banner warn"><b>超出知识库</b><span>此问题未在知识库中找到相关资料，回答由模型自行生成，请自行甄别</span></div>`;
    if (res.out_of_kb) {
      banners.push(oobBanner);
    }

    const marks = (res.mentions || []).length
      ? `<div class="ans-flags">${res.mentions.slice(0, 8).map(m =>
        `<span class="pill ghost" style="cursor:pointer" data-ent="${esc(m.id)}">
          <span style="width:6px;height:6px;border-radius:2px;background:${typeColor(m.type)};display:inline-block"></span>
          ${esc(m.name)}</span>`).join('')}</div>` : '';

    const struct = (res.structure || []).length ? `
        <div class="ans-struct">
          ${res.structure.map(g => `
            <div class="sg">
              <span class="sg-l">${esc(g.label)}</span>
              <div class="sg-i">${g.items.map(it =>
                `<span class="sg-t" data-ent="${esc(it.id)}">${esc(it.name)}</span>`).join('')}
                ${g.total > g.items.length
                  ? `<span class="sg-m">另 ${g.total - g.items.length} 项</span>` : ''}</div>
            </div>`).join('')}
        </div>` : '';

    const tl = (res.timeline && res.timeline.available) ? `
        <div class="ans-tl">
          <div class="tl-h">月度监测 · ${esc(res.timeline.metric)}
            <span>${res.timeline.items.length} 个数据点 · 来自国家卫健委通报</span></div>
          <div class="tl-chart" data-tl-chart></div>
        </div>` : '';

    // 「在图谱中查看」只在锚定到图谱实体时才显示
    const hasMentions = (res.mentions || []).length > 0;
    const graphBtn = hasMentions
      ? `<button class="btn tiny" data-act="graph">在图谱中查看</button>` : '';

    // 每条对话的操作（修改/删除）挂在用户提问气泡上，不在答案区重复

    // 版本切换器：同一条被改过多次时，可在历史版本间切换
    const vers = (m && m.versions && m.versions.length > 1)
      ? `<div class="ans-vers">
           <button class="ver-btn" data-act="ver" data-idx="${idx}" data-step="-1" ${m.active <= 0 ? 'disabled' : ''}>‹</button>
           <span class="ver-lab">版本 ${m.active + 1}/${m.versions.length}</span>
           <button class="ver-btn" data-act="ver" data-idx="${idx}" data-step="1" ${m.active >= m.versions.length - 1 ? 'disabled' : ''}>›</button>
         </div>` : '';

    return `
      <div class="ans" data-ans data-idx="${idx != null ? idx : ''}" data-q="${esc(res.question || '')}" data-out-of-kb="${res.out_of_kb ? '1' : '0'}">
        ${banners.join('')}
        <div class="ans-body">${renderBody(res.answer)}</div>
        ${res.out_of_kb ? `<div class="ans-oob-foot">${oobBanner}</div>` : ''}
        ${struct}
        ${tl}
        ${marks}
        ${vers}
        <div class="ans-acts">
          <div class="ans-vote">
            <button class="vote-btn" data-vote="good" title="有用">👍</button>
            <button class="vote-btn" data-vote="bad" title="没用">👎</button>
          </div>
          ${graphBtn}
        </div>
      </div>`;
  };

  /* 趋势图必须在 DOM 插入之后再初始化，所以单独留一个后置钩子 */
  Views.paintTimeline = function (root, res) {
    if (!root || !res || !res.timeline || !res.timeline.available) return;
    const box = root.querySelector('[data-tl-chart]');
    if (!box || !window.echarts) return;
    const items = res.timeline.items;
    const chart = echarts.init(box);
    const unit = (items[0] && items[0].unit) || '';

    // 通报数据里偶尔有个把数量级异常的月份（登革热 2026-01 报过 102 万例），
    // 线性刻度下其余 60 多个点会被压成一条贴地直线。数量级差太多时切对数刻度，
    // 并在标题上写明，避免读者以为值被改过。
    const vals = items.map(i => Number(i.value)).filter(v => v > 0).sort((a, b) => a - b);
    const med = vals.length ? vals[Math.floor(vals.length / 2)] : 0;
    const useLog = vals.length > 6 && med > 0 && vals[vals.length - 1] > med * 30;
    const hint = box.parentElement && box.parentElement.querySelector('.tl-h span');
    if (useLog && hint) hint.textContent += ' · 对数刻度';

    chart.setOption({
      grid: { left: 8, right: 12, top: 14, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,.98)', borderColor: '#e2e8f1',
        textStyle: { color: '#141d2b', fontSize: 12 },
        formatter: p => {
          const it = p[0];
          return `${it.axisValue}<br/>${res.timeline.metric}：<b>${Number(it.value).toLocaleString('zh-CN')}</b> ${unit}`;
        },
      },
      xAxis: {
        type: 'category',
        data: items.map(i => i.period),
        axisLine: { lineStyle: { color: '#e2e8f1' } },
        axisTick: { show: false },
        axisLabel: { color: '#9ba7ba', fontSize: 10, interval: Math.max(0, Math.floor(items.length / 6) - 1) },
      },
      yAxis: {
        type: useLog ? 'log' : 'value',
        splitLine: { lineStyle: { color: '#eef2f7' } },
        axisLabel: { color: '#9ba7ba', fontSize: 10 },
        min: useLog ? 1 : null,
      },
      series: [{
        type: 'line', smooth: true, symbol: 'circle', symbolSize: 4, showSymbol: false,
        data: items.map(i => i.value),
        lineStyle: { width: 2, color: '#0f6ab4' },
        itemStyle: { color: '#0f6ab4' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(15,106,180,.22)' },
            { offset: 1, color: 'rgba(15,106,180,0)' },
          ]),
        },
      }],
    });
    const onResize = () => chart.resize();
    window.addEventListener('resize', onResize);
  };

  function streamBox() {
    let box = $('.stream-inner');
    if (!box || box.querySelector('.welcome')) {
      $('#stream').classList.remove('is-welcome');   // 有消息就不该再垂直居中
      $('#stream').innerHTML = '<div class="stream-inner"></div>';
      box = $('.stream-inner');
    }
    return box;
  }

  Views.pushUser = function (q) {
    const box = streamBox();
    const d = document.createElement('div');
    d.className = 'msg-user';
    // 右侧锚点导航定位用：给每条消息加序号 id
    S.msgSeq = (S.msgSeq || 0) + 1;
    d.id = 'msg-' + S.msgSeq;
    d.setAttribute('data-msg', S.msgSeq);
    d.innerHTML = `<div class="mu-text">${esc(q)}</div>`;
    box.appendChild(d);
    // 流式结束后（done）才知道这条的会话下标，先记住这个气泡
    S.lastUserEl = d;
    return box;
  };

  Views.pushLoading = function (label) {
    const box = streamBox();
    const d = document.createElement('div');
    d.className = 'ans';
    d.id = 'pendingAns';
    d.innerHTML = `<div class="loading" style="padding:16px 18px"><span class="spin"></span>`
      + `<span class="load-lab">${esc(label || '正在检索相关资料…')}</span></div>`;
    box.appendChild(d);
    H.scrollStream();
    return d;
  };

  /* ── 会话侧边栏（类似 DeepSeek）──
     一个会话 = 一组连续问答。会话列表只存内存（S.sessions），
     刷新/关停服务即清空。当前会话 id 记在 S.currentId。

     每条消息支持多版本（改了问题重新生成后旧版保留）：
       m = { uid, versions: [{q,res,brief}], active: 当前下标, from: {uid, v} }
       m.q / m.res / m.brief 是「当前激活版本」的镜像，兼容旧代码取用。

     from 记录这条追问是在「哪条消息的第几个版本」下问的，构成一棵对话树：
       根消息 from=null；改过版本的消息，每个版本可以有自己的一条后续链。
     所以切回版本 1，只在版本 2 里追问过的那几条就不再显示、也不再进上下文——
     否则会出现「切回二甲双胍，历史里还带着普通感冒那串追问」的错位。
     上下文同样取可见链上各条消息的激活版本。 */

  let msgUid = 0;

  function currentSession() {
    return (S.sessions || []).find(x => x.id === S.currentId) || null;
  }

  function ensureSession() {
    if (currentSession()) return currentSession();
    const s = { id: 's' + Date.now(), title: '', messages: [] };
    S.sessions = S.sessions || [];
    S.sessions.unshift(s);
    S.currentId = s.id;
    return s;
  }

  function msgOf(m) {
    if (m.versions && m.versions[m.active]) return m.versions[m.active];
    return { q: m.q || '', res: m.res || {}, brief: m.brief || '' };
  }
  function syncMsg(m) {
    const v = msgOf(m);
    m.q = v.q; m.res = v.res; m.brief = v.brief;
  }

  /* 补 uid / from：旧会话（这个字段上线前建的）按数组顺序串成一条直线 */
  function normalize(s) {
    const msgs = (s && s.messages) || [];
    msgs.forEach((m, i) => {
      if (!m.uid) m.uid = 'm' + (++msgUid);
      if (!('from' in m)) {
        m.from = i > 0 ? { uid: msgs[i - 1].uid, v: msgs[i - 1].active || 0 } : null;
      }
    });
  }

  /* 当前分支上的可见消息（沿 from 链从根走到叶子），返回 [{m, idx}]。
     idx 是它在 s.messages 里的真实下标，菜单/编辑仍按它定位。 */
  function chainOf(s) {
    if (!s || !s.messages || !s.messages.length) return [];
    normalize(s);
    const msgs = s.messages;
    const key = (uid, v) => uid + '#' + (v || 0);

    // 每个「父消息 + 版本」最多一个孩子，先到先得（同版本多次追问时取最早那条）
    const childOf = new Map();
    msgs.forEach(m => {
      const f = m.from;
      if (!f || !f.uid) return;
      const k = key(f.uid, f.v);
      if (!childOf.has(k)) childOf.set(k, m);
    });

    let cur = msgs.find(m => !m.from || !m.from.uid) || msgs[0];
    const out = [];
    const seen = new Set();
    while (cur && !seen.has(cur.uid) && out.length < 500) {
      seen.add(cur.uid);
      out.push({ m: cur, idx: msgs.indexOf(cur) });
      cur = childOf.get(key(cur.uid, cur.active || 0)) || null;
    }
    return out;
  }

  function newMsg(q, res, brief, parent, parentVer) {
    const m = {
      uid: 'm' + (++msgUid), versions: [{ q, res, brief }], active: 0, q, res, brief,
      from: parent ? { uid: parent.uid, v: parentVer || 0 } : null,
    };
    return m;
  }

  function renderSessions() {
    const list = $('#histList');
    const empty = $('#histEmpty');
    if (!list) return;
    const items = S.sessions || [];
    if (empty) empty.hidden = items.length > 0;
    if (!items.length) { list.innerHTML = ''; return; }
    list.innerHTML = items.map(s => `
      <div class="hist-item${s.id === S.currentId ? ' on' : ''}" data-sid="${esc(s.id)}" role="button" tabindex="0">
        <span class="hist-q">${esc(s.title || '新对话')}</span>
        <span class="hist-n">${chainOf(s).length}</span>
        <span class="hist-ops">
          <button class="hist-op" data-rename="${esc(s.id)}" title="重命名会话" aria-label="重命名">✎</button>
          <button class="hist-op danger" data-delsess="${esc(s.id)}" title="删除会话" aria-label="删除">✕</button>
        </span>
      </div>`).join('');
  }

  // 右侧消息锚点导航：清空 / 追加一个锚点
  function clearMsgNav() {
    const nav = $('#msgNav');
    if (nav) nav.innerHTML = '';
    S.msgSeq = 0;
  }

  function addMsgDot(seq, title) {
    const nav = $('#msgNav');
    if (!nav) return;
    const dot = document.createElement('button');
    dot.className = 'msg-dot';
    dot.type = 'button';
    dot.dataset.target = 'msg-' + seq;
    dot.dataset.title = (title || '').slice(0, 30);
    dot.title = title || '';
    nav.appendChild(dot);
  }

  // 用户气泡上的操作按钮（修改 / 删除）——挂在提问上，不挂在答案上
  function userOpsHtml(idx) {
    if (idx == null) return '';
    return `<div class="mu-ops">
      <button class="op-btn" data-act="edit" data-idx="${idx}" title="修改这个问题并重新生成">修改</button>
      <button class="op-btn danger" data-act="del" data-idx="${idx}" title="删除这条对话">删除</button>
    </div>`;
  }

  // 把一条消息渲染进容器（用户气泡 + 答案 + 锚点），返回答案 DOM
  function renderMessageInto(box, m, idx) {
    const v = msgOf(m);
    const u = document.createElement('div');
    u.className = 'msg-user';
    S.msgSeq = (S.msgSeq || 0) + 1;
    u.id = 'msg-' + S.msgSeq;
    u.setAttribute('data-msg', S.msgSeq);
    u.setAttribute('data-idx', idx);
    u.innerHTML = `<div class="mu-text">${esc(v.q)}</div>${userOpsHtml(idx)}`;
    box.appendChild(u);

    const wrap = document.createElement('div');
    wrap.innerHTML = Views.renderAnswer(v.res, idx, m);
    const ansEl = wrap.firstElementChild;
    box.appendChild(ansEl);
    if (Views.paintTimeline) Views.paintTimeline(ansEl, v.res);
    addMsgDot(S.msgSeq, v.q);
    return ansEl;
  }

  // 当前版本之外还有别的分支时，明确告诉用户「它们没丢，切版本能看到」，
  // 否则切回旧版后下面几条突然消失，会被当成 bug。
  function branchNote(box, s, chain) {
    const hidden = ((s && s.messages) || []).length - chain.length;
    if (hidden <= 0) return;
    const note = document.createElement('div');
    note.className = 'banner info branch-hint';
    note.innerHTML = `<span>当前版本这条线之外还有 <b>${hidden}</b> 条追问，`
      + `它们属于其他版本；点回答右上角的「‹ 版本 ›」切换即可看到。</span>`;
    box.appendChild(note);
  }

  // 重画当前会话（删、改、切版本后调用）
  function rerenderCurrent() {
    const s = currentSession();
    $('#stream').classList.remove('is-welcome');
    $('#stream').innerHTML = '<div class="stream-inner"></div>';
    const box = $('.stream-inner');
    clearMsgNav();
    const chain = chainOf(s);
    if (!chain.length) {
      Views.renderWelcome();
    } else {
      chain.forEach(x => renderMessageInto(box, x.m, x.idx));
      S.ask = msgOf(chain[chain.length - 1].m).res;
      branchNote(box, s, chain);
    }
    renderSessions();
    H.scrollStream();
  }

  function newChat() {
    if (S.asking) return;
    S.ask = null;
    S.currentId = null;
    $('#stream').innerHTML = '';
    Views.renderWelcome();
    renderSessions();
    clearMsgNav();
    // 恢复显示「试试」推荐问题标签
    const chips = $('#chipsRow');
    if (chips) chips.hidden = false;
  }

  function switchSession(id) {
    const s = (S.sessions || []).find(x => x.id === id);
    if (!s) return;
    S.currentId = id;
    const chain = chainOf(s);
    S.ask = chain.length ? msgOf(chain[chain.length - 1].m).res : null;
    // 重放该会话当前分支上的消息
    $('#stream').classList.remove('is-welcome');
    $('#stream').innerHTML = '<div class="stream-inner"></div>';
    const box = $('.stream-inner');
    clearMsgNav();
    if (!chain.length) {
      Views.renderWelcome();
    } else {
      chain.forEach(x => renderMessageInto(box, x.m, x.idx));
      branchNote(box, s, chain);
    }
    renderSessions();
    H.scrollStream();
  }

  /* ── 删除 / 重命名 / 修改 ── */

  async function deleteMessage(idx) {
    const s = currentSession();
    if (!s || !s.messages[idx]) return;
    normalize(s);
    const target = s.messages[idx];
    const v = msgOf(target);

    // 连带删掉它下面的分支：删了父问题，子追问的上下文就不存在了，留着只会错位
    const kill = new Set([target.uid]);
    let grew = true;
    while (grew) {
      grew = false;
      s.messages.forEach(m => {
        if (m.from && m.from.uid && kill.has(m.from.uid) && !kill.has(m.uid)) {
          kill.add(m.uid);
          grew = true;
        }
      });
    }
    const n = kill.size;

    const ok = await H.openModal({
      title: '删除这条对话？',
      text: n > 1
        ? `「${(v.q || '').slice(0, 40)}」以及它下面 ${n - 1} 条追问将一起移除，不可恢复。`
        : `「${(v.q || '').slice(0, 40)}」将从本会话移除，其上下文也一并删除，不可恢复。`,
      okText: '删除', danger: true,
    });
    if (!ok) return;

    s.messages = s.messages.filter(m => !kill.has(m.uid));
    const ch = chainOf(s);
    if (ch.length && !s.title) s.title = msgOf(ch[0].m).q.slice(0, 24);
    rerenderCurrent();
    H.toast(n > 1 ? `已删除 ${n} 条对话` : '已删除这条对话');
  }

  async function deleteSession(id) {
    const s = (S.sessions || []).find(x => x.id === id);
    if (!s) return;
    const ok = await H.openModal({
      title: '删除整个会话？',
      text: `「${s.title || '新对话'}」共 ${s.messages.length} 条对话将全部删除，不可恢复。`,
      okText: '删除', danger: true,
    });
    if (!ok) return;
    S.sessions = S.sessions.filter(x => x.id !== id);
    if (S.currentId === id) {
      S.currentId = S.sessions.length ? S.sessions[0].id : null;
      if (S.currentId) switchSession(S.currentId);
      else newChat();
    } else {
      renderSessions();
    }
    H.toast('已删除会话');
  }

  async function renameSession(id) {
    const s = (S.sessions || []).find(x => x.id === id);
    if (!s) return;
    const name = await H.openModal({
      title: '重命名会话',
      input: true, value: s.title || '', okText: '保存',
    });
    if (name == null) return;
    s.title = name.slice(0, 40);
    renderSessions();
    H.toast('已重命名');
  }

  // 修改一条提问：按新问题重新生成，旧版本存档，可随时切回
  async function editMessage(idx) {
    const s = currentSession();
    if (!s || !s.messages[idx]) return;
    if (S.asking) { H.toast('请等当前回答结束再修改'); return; }
    const m = s.messages[idx];
    const cur = msgOf(m);
    const newQ = await H.openModal({
      title: '修改这条提问',
      text: '按新问题重新生成答案，原答案保存为旧版本，可随时切回；'
          + '原版本下的后续追问会跟着旧版一起收起。',
      input: true, value: cur.q, okText: '重新生成',
    });
    if (newQ == null) return;
    if (newQ === cur.q.trim()) { H.toast('问题没有变化'); return; }

    // 上下文：当前分支上、这条之前最近 4 轮（各条取激活版本）
    const chain = chainOf(s);
    const pos = chain.findIndex(x => x.idx === idx);
    const hist = chain.slice(0, pos < 0 ? 0 : pos).slice(-4).map(x => {
      const v = msgOf(x.m);
      return { q: v.q, brief: v.brief || '' };
    });
    const deepThink = !!(document.getElementById('btnDeep') && document.getElementById('btnDeep').classList.contains('on'));
    const kbOnly = !document.getElementById('btnKb') || document.getElementById('btnKb').classList.contains('on');

    S.asking = true;
    $('#send').disabled = true;

    // 立刻把提问气泡换成新问题。否则整个生成过程中上面挂的还是旧问题，
    // 看起来像「答非所问」，要等最后 rerenderCurrent 才纠正——这就是那个 bug。
    // 只改 DOM 不改数据：失败时 rerenderCurrent() 会从数据还原旧文案。
    const userEl = document.querySelector(`#stream .msg-user[data-idx="${idx}"]`);
    if (userEl) {
      const t = userEl.querySelector('.mu-text');
      if (t) t.textContent = newQ;
      userEl.classList.add('editing');
    }

    // 与正常对话一致：就地流式重生成，显示计时 + 打字机，避免「点了没反应干等」
    const label = deepThink ? '正在深度思考' : (kbOnly ? '正在检索知识库' : '正在重新生成');
    let ansEl = document.querySelector(`#stream .ans[data-idx="${idx}"]`);
    if (!ansEl && userEl) {
      // 兜底：答案容器万一找不到（例如旧 DOM 结构），就地补一个，别让旧答案留在屏幕上
      ansEl = document.createElement('div');
      ansEl.className = 'ans';
      ansEl.setAttribute('data-idx', idx);
      userEl.after(ansEl);
    }
    const t0 = Date.now();
    let statusEl = null, bodyEl = null, started = false, answerText = '', meta = null, doneObj = null;
    let timer = 0;

    if (ansEl) {
      ansEl.innerHTML = `<div class="stream-status"><span class="ss-txt">${esc(label)}… 0s</span></div>`;
      statusEl = ansEl.querySelector('.ss-txt');
      H.scrollStream();
      timer = setInterval(() => {
        if (statusEl) statusEl.textContent = `${label}… ${Math.floor((Date.now() - t0) / 1000)}s`;
      }, 400);
    }

    try {
      await postStream('/api/ask/stream',
        { question: newQ, deep_think: deepThink, kb_only: kbOnly, history: hist },
        (kind, obj) => {
          if (kind === 'meta') {
            meta = obj;
          } else if (kind === 'delta') {
            answerText += obj.text || '';
            if (!started && ansEl) {
              started = true;
              const st = ansEl.querySelector('.stream-status');
              if (st) st.classList.add('has-body');
              bodyEl = document.createElement('div');
              bodyEl.className = 'ans-body';
              ansEl.appendChild(bodyEl);
              const cur2 = document.createElement('div');
              cur2.className = 'cursor';
              ansEl.appendChild(cur2);
            }
            if (bodyEl) { bodyEl.innerHTML = renderBody(answerText); H.scrollStream(); }
          } else if (kind === 'done') {
            doneObj = obj;
          }
        });
      clearInterval(timer);

      if (!answerText || answerText.trim().length < 4) {
        throw new Error('模型没有给出有效回答');
      }
      const res = Object.assign({}, meta, doneObj, {
        question: newQ, answer: answerText, evidence: [],
      });
      if (!m.versions) m.versions = [{ q: cur.q, res: cur.res, brief: cur.brief }];
      m.versions.push({ q: newQ, res, brief: (doneObj && doneObj.brief) || '' });
      m.active = m.versions.length - 1;
      syncMsg(m);
      if (idx === 0) s.title = newQ.slice(0, 24);
      // 新版本是一条新分支，原分支上的后续追问不属于它，切过去会自动收起
      const hidden = Math.max(0, chain.length - chainOf(s).length);
      rerenderCurrent();
      H.toast(hidden
        ? `已生成新版本；原分支下 ${hidden} 条追问已收起，切回旧版可恢复`
        : '已生成新版本，可切换查看旧版');
    } catch (err) {
      clearInterval(timer);
      H.toast('重新生成失败：' + err.message);
      rerenderCurrent();   // 恢复到原状，别留一个半截的容器
    } finally {
      S.asking = false;
      $('#send').disabled = false;
    }
  }

  // 消息版本切换（旧版找回）。切过去之后，只属于别的版本的后续追问会自动收起，
  // 同时它们也不再参与上下文——这正是「切回版本 1 就不该带着版本 2 的追问」。
  function switchVersion(idx, step) {
    const s = currentSession();
    if (!s || !s.messages[idx]) return;
    const m = s.messages[idx];
    const next = m.active + step;
    if (!m.versions || next < 0 || next >= m.versions.length) return;
    const before = chainOf(s).length;
    m.active = next;
    syncMsg(m);
    if (idx === 0) s.title = m.q.slice(0, 24);
    const after = chainOf(s).length;
    rerenderCurrent();
    if (after < before) H.toast(`已切到版本 ${next + 1}；另外 ${before - after} 条属于其他版本的分支，已收起`);
    else if (after > before) H.toast(`已切到版本 ${next + 1}；带出该分支下的 ${after - before} 条追问`);
  }

  Views.renderSessions = renderSessions;
  Views.newChat = newChat;
  Views.switchSession = switchSession;
  Views.rerenderCurrent = rerenderCurrent;
  Views.msgOf = msgOf;
  Views.newMsg = newMsg;
  Views.deleteMessage = deleteMessage;
  Views.deleteSession = deleteSession;
  Views.renameSession = renameSession;
  Views.editMessage = editMessage;
  Views.switchVersion = switchVersion;

  /* ══════════════════════ 提问 ══════════════════════ */

  async function ask(text) {
    const q = (text || '').trim();
    if (!q || S.asking) return;
    // 还在录音就先把麦克风关掉；silence 不回写输入框，不会把刚清空的内容又填回去
    if (window.Voice) window.Voice.silence();
    S.asking = true;
    $('#send').disabled = true;
    const input = $('#q');
    input.value = '';
    input.style.height = 'auto';
    // 用户开始提问后，隐藏「试试」推荐问题标签
    const chips = $('#chipsRow');
    if (chips) chips.hidden = true;

    Views.pushUser(q);
    runStream(q, true);
  }

  // 发起一次流式问答。isFirst=true 表示新提问（answerText 从空开始）；
  // 「继续」时 isFirst=false，复用已有容器状态，不重复 pushUser。
  async function runStream(q, isFirst) {
    const deepThink = !!(document.getElementById('btnDeep') && document.getElementById('btnDeep').classList.contains('on'));
    const kbOnly = !document.getElementById('btnKb') || document.getElementById('btnKb').classList.contains('on');
    const label = deepThink ? '正在深度思考' : (kbOnly ? '正在检索知识库' : '正在思考');

    // 计时：贯穿 loading + 流式全过程
    const t0 = Date.now();
    let statusEl = null;     // 状态栏文字（计时）
    let pauseBtn = null;     // 暂停/继续按钮
    let pend = null;         // loading 占位（仅在 meta 前存在）
    if (isFirst) {
      pend = Views.pushLoading(label + '…');
    }

    let timer = setInterval(() => {
      const secs = Math.floor((Date.now() - t0) / 1000);
      if (statusEl) statusEl.textContent = `${label}… ${secs}s`;
      else if (pend) {
        const n = pend.querySelector && pend.querySelector('.load-lab');
        if (n) n.textContent = `${label}… ${secs}s`;
      }
    }, 400);

    // 流式状态
    let meta = null;
    let answerText = '';
    let streamEl = null;    // 临时答案容器
    let streamBody = null;  // 容器里的 .ans-body
    let started = false;    // 是否已输出正文（决定是否显示横线/正文区/光标）
    let paused = false;
    const ctrl = new AbortController();

    function setPaused(p) {
      paused = p;
      if (pauseBtn) {
        pauseBtn.textContent = p ? '继续' : '暂停';
        pauseBtn.title = p ? '继续生成' : '暂停生成';
      }
      if (streamEl) {
        streamEl.classList.toggle('paused', p);
        // 暂停后光标与圆点停止闪烁
        const cursor = streamEl.querySelector('.cursor');
        if (cursor) cursor.style.display = p ? 'none' : '';
      }
    }

    function doPause() {
      if (paused) {
        // 已是暂停态 → 继续：移除旧容器，重新生成（Ollama 流无法续传）
        setPaused(false);
        if (streamEl && streamEl.parentNode) streamEl.remove();
        streamEl = null; streamBody = null; statusEl = null; pauseBtn = null;
        started = false;
        answerText = '';
        meta = null;
        S.asking = true;
        $('#send').disabled = true;
        runStream(q, false);
        return;
      }
      setPaused(true);
      ctrl.abort();
      clearInterval(timer);
      if (statusEl) statusEl.textContent = '已暂停';
      S.asking = false;
      $('#send').disabled = false;
    }

    // 上下文记忆：带上当前分支最近 4 轮的 [问题, 该轮精简答案]（取各条消息的激活版本）。
    // 用 chainOf 而不是 messages：被版本切换收起的那些追问不该混进来。
    const ctxChain = chainOf(currentSession());
    const ctxHistory = ctxChain.slice(-4)
      .map(x => { const v = msgOf(x.m); return { q: v.q, brief: v.brief || '' }; });

    try {
      await postStream('/api/ask/stream',
        { question: q, deep_think: deepThink, kb_only: kbOnly, history: ctxHistory },
        (kind, obj) => {
          if (kind === 'meta') {
            meta = obj;
            if (pend) { pend.remove(); pend = null; }
            const box = streamBox();
            streamEl = document.createElement('div');
            streamEl.className = 'ans';
            streamEl.setAttribute('data-ans', '');
            // 思考阶段：只有状态栏，不显示正文区/横线/光标
            streamEl.innerHTML = `
              <div class="stream-status">
                <span class="ss-txt">${esc(label)}… ${Math.floor((Date.now() - t0) / 1000)}s</span>
                <button class="ss-pause" type="button" title="暂停生成">暂停</button>
              </div>`;
            box.appendChild(streamEl);
            statusEl = streamEl.querySelector('.ss-txt');
            pauseBtn = streamEl.querySelector('.ss-pause');
            pauseBtn.addEventListener('click', doPause);
            H.scrollStream();
          } else if (kind === 'delta') {
            if (paused) return;
            answerText += obj.text || '';
            if (!started) {
              // 第一次输出正文：给状态栏加横线，创建正文区 + 光标
              started = true;
              const st = streamEl.querySelector('.stream-status');
              if (st) st.classList.add('has-body');
              streamBody = document.createElement('div');
              streamBody.className = 'ans-body';
              const cursor = document.createElement('div');
              cursor.className = 'cursor';
              streamEl.appendChild(streamBody);
              streamEl.appendChild(cursor);
            }
            if (streamBody) {
              streamBody.innerHTML = renderBody(answerText);
              H.scrollStream();
            }
          } else if (kind === 'done') {
            if (paused) return;
            clearInterval(timer);
            const res = Object.assign({}, meta, obj, {
              question: q, answer: answerText, evidence: [],
            });
            const sess = ensureSession();
            // 挂到「当前分支的末尾」：它问的是哪个版本的延续，就记在哪个版本下。
            const ch = chainOf(sess);
            const parent = ch.length ? ch[ch.length - 1].m : null;
            // brief 是该轮的精简上下文（深度思考取「小结」，快速取全文），下一轮带上
            const m = newMsg(q, res, obj.brief || '', parent, parent ? parent.active : 0);
            sess.messages.push(m);
            const idx = sess.messages.length - 1;
            if (!sess.title) sess.title = q.slice(0, 24);

            // 给刚发出的这条提问气泡补上「修改 / 删除」按钮（流式结束时才知道下标）
            if (S.lastUserEl && S.lastUserEl.isConnected && !S.lastUserEl.querySelector('.mu-ops')) {
              S.lastUserEl.setAttribute('data-idx', idx);
              const ops = document.createElement('div');
              ops.innerHTML = userOpsHtml(idx);
              S.lastUserEl.appendChild(ops.firstElementChild);
            }

            const box = streamBox();
            const wrap = document.createElement('div');
            wrap.innerHTML = Views.renderAnswer(res, idx, m);
            const ansEl = wrap.firstElementChild;
            if (streamEl && streamEl.parentNode) streamEl.replaceWith(ansEl);
            else box.appendChild(ansEl);
            if (Views.paintTimeline) Views.paintTimeline(ansEl, res);
            S.ask = res;
            renderSessions();
            addMsgDot(S.msgSeq, q);
            H.scrollStream();
          }
        }, ctrl.signal);
    } catch (err) {
      clearInterval(timer);
      if (paused) {
        // 主动暂停：保留已生成部分，光标已隐藏
        if (streamEl && streamEl.querySelector('.cursor')) {
          streamEl.querySelector('.cursor').remove();
        }
      } else {
        if (streamEl && streamEl.parentNode) streamEl.remove();
        else if (pend) pend.remove();
        const box = streamBox();
        const d = document.createElement('div');
        d.className = 'banner danger';
        d.innerHTML = `<b>请求失败</b><span>${esc(err.name === 'AbortError' ? '已停止' : err.message)}</span>`;
        box.appendChild(d);
      }
    } finally {
      clearInterval(timer);
      S.asking = false;
      $('#send').disabled = false;
    }
  }

  /* ══════════════════════ 知识库 ══════════════════════ */

  const KIND_LABEL = { disease: '疾病章节', drug: '药品说明书', guide: '临床指南', profile: '图谱档案', db: '数据库原文' };
  const HUB_KINDS = [['all', '全部'], ['disease', '疾病章节'], ['drug', '药品说明书'], ['guide', '临床指南'], ['profile', '图谱档案']];
  const DB_KINDS = [['all', '全部'], ['disease', '疾病章节'], ['drug', '药品说明书']];
  let kbBound = false;

  // 统一取文档分类中文名：优先 doc_kind，其次 kind，最后回退到字段标签
  function kindLabel(d) {
    return KIND_LABEL[d.doc_kind] || KIND_LABEL[d.kind] || d.field_label || '';
  }

  function paintKbChrome() {
    const db = (S.boot && S.boot.db) || {};
    const kinds = HUB_KINDS;
    if (!kinds.some(k => k[0] === S.kb.kind)) S.kb.kind = 'all';
    $('#kbFilters').innerHTML = kinds.map(([k, l]) =>
      `<button class="fchip${k === S.kb.kind ? ' on' : ''}" data-kind="${k}">${l}</button>`).join('');
    $('#kbq').placeholder = '在 5,600 余段正文中检索…';
  }

  function dbCard(d) {
    const lang = d.lang === 'en' ? '<span class="lang-b" title="源数据为英文说明书，未翻译">EN</span>' : '';
    const kind = d.db_kind === 'drug' ? '说明书' : '疾病章节';
    return `
      <div class="res from-db${S.kb.activeId === d.id ? ' on' : ''}" data-res="${esc(d.id)}">
        <div class="res-top">
          <span class="kind-b ${esc(d.db_kind)}">${kind}</span>${lang}
          <b title="${esc(d.title)}">${esc(d.title)}</b>
        </div>
        <div class="res-src">${esc(d.from)} · ${esc(d.field_label)} · ${fmt(d.chars)} 字</div>
        <div class="res-snip">${hl(d.snippet, d.hits)}</div>
      </div>`;
  }

  KB.render = function () {
    if (!kbBound) {
      kbBound = true;
      const inp = $('#kbq');
      let t = 0;
      inp.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { S.kb.q = inp.value.trim(); KB.search(); }, 220); });
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { S.kb.q = inp.value.trim(); KB.search(); }
      });
      $('#kbFilters').addEventListener('click', e => {
        const b = e.target.closest('[data-kind]');
        if (!b) return;
        S.kb.kind = b.dataset.kind;
        $$('#kbFilters [data-kind]').forEach(x => x.classList.toggle('on', x === b));
        KB.search();
      });
      $('#kbResults').addEventListener('click', e => {
        const ent = e.target.closest('[data-ent]');
        if (ent) { H.openEntity(ent.dataset.ent); return; }
        const r = e.target.closest('[data-res]');
        if (r) KB.openDoc(r.dataset.res);
      });
      $('#kbDetail').addEventListener('click', e => {
        const ent = e.target.closest('[data-ent]');
        if (ent) { H.openEntity(ent.dataset.ent); return; }
        const act = e.target.closest('[data-act]');
        if (!act) return;
        const d = S.kb.doc;
        if (!d) return;
        if (act.dataset.act === 'ask') {
          go('ask');
          $('#q').value = d.title.replace(/ · .*$/, '') + '，' + d.field_label + '有哪些要点？';
          $('#q').focus();
        }
      });
      const kinds = [['all', '全部'], ['disease', '疾病章节'], ['drug', '药品说明书'], ['guide', '临床指南'], ['profile', '图谱档案']];
      $('#kbFilters').innerHTML = kinds.map(([k, l]) =>
        `<button class="fchip${k === 'all' ? ' on' : ''}" data-kind="${k}">${l}</button>`).join('');
    }

    paintKbChrome();

    if (!S.boot) return;
    // 无搜索词时也走 KB.search —— 里面会判断 q 为空则进入浏览模式，
    // 右侧自动打开第一条，页面不再空白。
    KB.search();
  };

  /* 浏览模式：没搜词时按分类列代表章节，右侧也不再只有一句「选一条资料」。
     同时把第一条自动打开，让右侧立刻有内容可看。 */
  function renderBrowse(r) {
    const kindLabel = { disease: '疾病章节', drug: '药品说明书', guide: '临床指南' };
    const cur = kindLabel[S.kb.kind] || '全部资料';
    $('#kbMeta').innerHTML = `
      <span>${esc(cur)} · 精选 <b>${r.docs.length}</b> 条</span>
      <span>共 ${fmt(r.browse_total || 0)} 个实体有正文章节</span>`;
    if (!r.docs.length) {
      $('#kbResults').innerHTML = `<div class="empty-state" style="padding:36px 16px">
        <div class="es-icon">?</div><h3>这一分类下暂时没有章节</h3>
        <p>换个分类，或直接输入疾病、药品名检索。</p></div>`;
      return;
    }
    const card = d => `
      <div class="res${S.kb.activeId === d.id ? ' on' : ''}" data-res="${esc(d.id)}">
        <div class="res-top">
          <span class="kind-b ${esc(d.doc_kind)}">${esc(kindLabel[d.doc_kind] || d.doc_kind)}</span>
          <b title="${esc(d.title)}">${esc(d.title)}</b>
        </div>
        <div class="res-src">${esc(d.source)} · ${esc(d.field_label)} · ${fmt(d.chars)} 字</div>
        <div class="res-snip">${esc(d.snippet)}</div>
      </div>`;
    $('#kbResults').innerHTML = `<div class="sec-h" style="padding:6px 10px 4px">按关注度排序
      <span class="cnt">${r.docs.length}</span></div>${r.docs.map(card).join('')}`;
    // 自动打开第一条，右侧立刻有内容；但深链已指定 doc 时（activeId 已设）不要覆盖
    if (!S.kb.activeId) {
      KB.openDoc(r.docs[0].id);
    }
  }

  function renderDbList(r) {
    $('#kbMeta').innerHTML = `
      <span>SQL 直查 · <b>${r.total}</b> 条</span>
      <span>${r.took_ms} ms</span>
      <span>${esc(r.source || 'medical_kg.db')}</span>
      ${r.mentions && r.mentions.length
        ? `<span>锚定 ${r.mentions.slice(0, 2).map(m => `<b>${esc(m.name)}</b>`).join(' ')}</span>` : ''}`;

    if (!r.docs.length) {
      const who = (r.mentions && r.mentions[0]) || null;
      $('#kbResults').innerHTML = `<div class="empty-state" style="padding:36px 16px">
        <div class="es-icon">?</div><h3>数据库里没有这一章</h3>
        <p>${who ? `「${esc(who.name)}」是图谱实体，但源库里没有它的正文章节。<br>
            它的信息在关系里——可以看图谱档案，或用问答里的图谱事实模式。`
          : '换个说法，或直接用实体名检索。'}</p>
        ${who ? `<div class="doc-acts" style="justify-content:center;margin-top:12px">
          <button class="btn tiny" data-ent="${esc(who.id)}">查看实体</button></div>` : ''}
      </div>`;
      return;
    }

    $('#kbResults').innerHTML = `<div class="sec-h" style="padding:6px 10px 4px">源库原文
      <span class="cnt">${r.docs.length}</span></div>${r.docs.map(dbCard).join('')}`;
  }

  KB.search = async function () {
    const q = S.kb.q;
    if (!q) {
      // 没搜词但点了分类 → 浏览模式：列出该分类的代表章节，别让页面空白。
      try {
        const r = await get('/api/search', { q: '', kind: S.kb.kind, limit: 40 });
        renderBrowse(r);
      } catch (err) {
        $('#kbResults').innerHTML = `<div class="banner danger"><b>读取失败</b><span>${esc(err.message)}</span></div>`;
      }
      return;
    }
    $('#kbResults').innerHTML = `<div class="loading"><span class="spin"></span>检索中…</div>`;
    try {
      const r = await get('/api/search', { q, kind: S.kb.kind, limit: 40 });
      S.kb.result = r;
      // vec 是语义召回（按意思找），单独成组；其余非 text 仍归「图谱锚定」
      const anchored = r.docs.filter(d => d.path !== 'text' && d.path !== 'vec');
      const semantic = r.docs.filter(d => d.path === 'vec');
      const textOnly = r.docs.filter(d => d.path === 'text');

      $('#kbMeta').innerHTML = `
        <span>意图 <b>${esc(r.intent ? r.intent.label : '—')}</b></span>
        <span>命中 <b>${r.total}</b> 条</span>
        <span>${r.took_ms} ms</span>
        ${r.mentions && r.mentions.length ? r.mentions.slice(0, 3).map(m => `<b>${esc(m.name)}</b>`).join(' ') : ''}`;

      if (!r.docs.length) {
        $('#kbResults').innerHTML = `<div class="empty-state" style="padding:36px 16px">
          <div class="es-icon">?</div><h3>没有找到相关资料</h3>
          <p>换个说法，或直接用实体名检索。</p></div>`;
        return;
      }

      const card = d => `
        <div class="res${S.kb.activeId === d.id ? ' on' : ''}" data-res="${esc(d.id)}">
          <div class="res-top">
            <span class="kind-b ${esc(d.doc_kind || d.kind)}">${esc(kindLabel(d))}</span>
            <b title="${esc(d.title)}">${esc(d.title)}</b>
          </div>
          <div class="res-src">${esc(d.source)}${d.year ? ' · ' + d.year : ''} · ${esc(d.field_label)}</div>
          <div class="res-snip">${hl(d.snippet, d.hits)}</div>
        </div>`;

      $('#kbResults').innerHTML =
        (anchored.length ? `<div class="sec-h" style="padding:6px 10px 4px">图谱锚定的章节<span class="cnt">${anchored.length}</span></div>` + anchored.map(card).join('') : '') +
        (semantic.length ? `<div class="sec-h" style="padding:12px 10px 4px">语义召回（按意思匹配）<span class="cnt">${semantic.length}</span></div>` + semantic.map(card).join('') : '') +
        (textOnly.length ? `<div class="sec-h" style="padding:12px 10px 4px">全文字面相关<span class="cnt">${textOnly.length}</span></div>` + textOnly.map(card).join('') : '');
    } catch (err) {
      $('#kbResults').innerHTML = `<div class="banner danger"><b>检索失败</b><span>${esc(err.message)}</span></div>`;
    }
  };

  /* 无译注的英文药品：正文区不放平铺英文，而是提示"本节尚未译注"，
     英文原文同样折叠在下方"查看英文原文"里，与其他已译注的条目一致。 */
  function enOnlyBody(d) {
    return `
      <div class="zh-doc zh-doc-empty">
        <div class="zh-p"><span class="zh-n">!</span><span>本节尚未译注，暂无中文正文；英文原文请展开下方折叠条核对。</span></div>
      </div>
      <details class="zh-src">
        <summary>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
          查看英文原文（${esc(d.field_label || '')}，${(d.text || '').length.toLocaleString()} 字符）
          <span class="zh-src-arrow">展开 ▾</span>
        </summary>
        <div class="doc-body">${esc(d.text)}</div>
      </details>`;
  }

  /* 中文正文：人工译好的要点列表 + 可折叠的英文原文。
     译文存在 drug_content_zh 表里，页面不调用任何模型。 */
  function zhBody(d) {
    const pts = d.zh_points || [];
    return `
      ${d.zh_note ? `<div class="zh-note-warn"><b>注意</b><span>${esc(d.zh_note)}</span></div>` : ''}
      <div class="zh-doc">
        ${pts.map((p, i) => `<div class="zh-p"><span class="zh-n">${i + 1}</span><span>${esc(p)}</span></div>`).join('')}
      </div>
      <details class="zh-src">
        <summary>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
          查看英文原文（${esc(d.field_label || '')}，${(d.text || '').length.toLocaleString()} 字符）
          <span class="zh-src-arrow">展开 ▾</span>
        </summary>
        <div class="doc-body">${esc(d.text)}</div>
      </details>`;
  }

  /* 药品章节的中文导读：信息全部来自药品登记表与知识图谱（都是中文事实），
     不是对英文说明书的翻译，所以只做「是什么/治什么/忌什么」这一层。 */
  function zhCard(d) {
    const z = d.zh;
    if (!z) return '';
    const meta = [
      ['英文名', z.en_name], ['通用名', z.generic_name],
      ['商品名', z.brand_name], ['生产企业', z.manufacturer],
    ].filter(([, v]) => v);
    const chips = (list, cls) => (list || []).slice(0, 8).map(n =>
      `<span class="zh-chip ${cls || ''}">${esc(n)}</span>`).join('');
    return `
      <div class="zh-card">
        <div class="zh-top"><b>中文导读</b>
          <span>取自药品登记与知识图谱，不是说明书译文</span></div>
        ${z.line ? `<p class="zh-line">${esc(z.line)}</p>` : ''}
        ${meta.length ? `<div class="zh-meta">${meta.map(([k, v]) =>
          `<div><em>${k}</em><i>${esc(v)}</i></div>`).join('')}</div>` : ''}
        ${z.treats && z.treats.length ? `<div class="zh-grp"><em>用于治疗</em>
          <div class="zh-chips">${chips(z.treats)}${z.treats.length >= 12
            ? '<span class="zh-more">等 12 种以上</span>' : ''}</div></div>` : ''}
        ${z.banned && z.banned.length ? `<div class="zh-grp"><em>禁用于</em>
          <div class="zh-chips">${chips(z.banned, 'bad')}</div></div>` : ''}
        ${z.combo && z.combo.length ? `<div class="zh-grp"><em>常见联用</em>
          <div class="zh-chips">${chips(z.combo)}</div></div>` : ''}
        <div class="zh-note">${(d.zh_points && d.zh_points.length)
          ? '下方正文已为本项目译注的中文，英文原文收在下方折叠条可展开核对；剂量与阈值照原文书写。'
          : '本节正文为 openFDA 英文原文，尚未译注——医学表述译错比看不懂更危险，故不做自动翻译；英文原文收在下方折叠条可展开查看。'}</div>
      </div>`;
  }

  KB.openDoc = async function (id, keep) {
    S.kb.activeId = id;
    $$('#kbResults .res').forEach(r => r.classList.toggle('on', r.dataset.res === id));
    const isDb = String(id).startsWith('db:');
    $('#kbDetail').innerHTML = `<div class="loading" style="padding:30px"><span class="spin"></span>读取全文…</div>`;
    try {
      const d = await get(isDb ? '/api/kb/section' : '/api/doc', { id });
      S.kb.doc = d;
      const hasZh = !!(d.zh_points && d.zh_points.length);
      const lang = hasZh
        ? '<span class="lang-b zh">中文译文</span>'
        : (d.from_db && /^[A-Za-z0-9\s\W]+$/.test((d.text || '').slice(0, 200))
          ? '<span class="lang-b">英文原文</span>' : '');
      const from = d.from_db
        ? `<div class="doc-from">直接取自 <code>${esc(d.table || 'medical_kg.db')}</code> · 未经改写</div>`
        : '';
      $('#kbDetail').innerHTML = `
        <div class="doc-pane">
          <div class="doc-head">
            ${from}
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
              <span class="kind-b ${esc(d.kind)}">${esc(KIND_LABEL[d.kind] || d.field_label)}</span>${lang}
              ${KIND_LABEL[d.kind] === d.field_label ? '' : `<span class="pill ghost">${esc(d.field_label)}</span>`}
              <span class="pill ghost">${fmt(d.chars)} 字</span>
            </div>
            <h2>${esc(d.title)}</h2>
            ${d.gloss ? `<div class="doc-gloss">本节：${esc(d.gloss)}</div>` : ''}
            ${d.tags && d.tags.length ? `<div class="doc-tags">${d.tags.map(t =>
              `<span class="pill ghost" style="cursor:pointer" data-ent="${esc(t.id)}">
                <span style="width:6px;height:6px;border-radius:2px;background:${typeColor(t.type || '')};display:inline-block"></span>
                ${esc(t.name)}</span>`).join('')}</div>` : ''}
          </div>
          ${d.suspect ? `<div class="doc-warn"><b>内容与药品可能不符</b><span>${esc(d.suspect)}</span></div>` : ''}
          ${zhCard(d)}
          ${hasZh ? zhBody(d) : (d.from_db && /^[A-Za-z0-9\s\W]+$/.test((d.text || '').slice(0, 200))
            ? enOnlyBody(d)
            : `<div class="doc-body">${esc(d.text)}</div>`)}
          ${(d.numbers && d.numbers.length) ? `<div class="num-row">
            <span class="num-lab">原文关键数值</span>
            ${d.numbers.map(v => `<span class="num-v">${esc(v)}</span>`).join('')}
          </div>` : ''}
          <div class="doc-foot">
            来源：<b>${esc(d.source)}</b>${d.year ? ' · ' + d.year : ''}<br>
            ${d.url
              ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">查看原始出处</a>`
              : (hasZh
                ? '中文为依据英文说明书逐条译注，专业术语与剂量数值照原文书写；英文原文收在下方折叠条，可展开逐句核对。'
                : (d.from_db
                  ? '本节为源库英文原文，尚未译注；英文原文收在下方折叠条，可展开查看。'
                  : '本段来自结构化知识库，已在构建时记录来源。'))}
          </div>
          <div class="doc-acts">
            <button class="btn primary" data-act="ask">用这段资料提问</button>
            ${d.tags && d.tags.length ? `<button class="btn" data-ent="${esc(d.tags[0].id)}">查看关联实体</button>` : ''}
          </div>
        </div>`;
      if (keep) S.kb.activeId = id;
    } catch (err) {
      $('#kbDetail').innerHTML = `<div class="banner danger" style="margin:30px"><b>读取失败</b><span>${esc(err.message)}</span></div>`;
    }
  };

  /* ══════════════════════ 概览 ══════════════════════ */

  let ovCache = null;
  let ovSchema = null;

  const TYPE_COLOR = {
    Disease: '#e6823c', Syndrome: '#e6823c', Drug: '#3f7fc2', DrugClass: '#3f7fc2',
    System: '#8a8a8a', BodyRegion: '#8a8a8a', Pathogen: '#9a6fb0', Strain: '#9a6fb0',
    Symptom: '#e5c75b', Complication: '#e5c75b',
  };

  const APIS = [
    ['GET', '/api/kg/graph', '图谱原生 node/link（ECharts 格式）'],
    ['GET', '/api/kg/entity', '实体属性 + 关系 + 关联章节'],
    ['GET', '/api/kb/search', '知识库全文检索（当场 SQL）'],
    ['GET', '/api/kb/section', '知识库单章全文'],
    ['POST', '/api/ask', '问答：图谱锚定 + 语料证据 + 校验'],
  ];

  async function runCheck() {
    const out = $('#checkOut');
    if (!out) return;
    out.innerHTML = '正在依次调用 5 个接口…';
    const tests = [
      ['图谱子图', () => get('/api/kg/graph', { limit: 60 })],
      ['实体详情', () => get('/api/kg/entity', { id: 'DIS_DENGUE' })],
      ['知识库检索', () => get('/api/kb/search', { q: '高血压' })],
      ['章节全文', () => get('/api/kb/section', { id: 'db:disease:DIS_OTORRHEA:treatment' })],
      ['问答', () => post('/api/ask', { question: '登革热是怎么传播的？' })],
    ];
    const lines = [];
    for (const [name, fn] of tests) {
      const t0 = performance.now();
      try {
        const r = await fn();
        const ms = Math.round(performance.now() - t0);
        let detail = '';
        if (r && r.nodes) detail = `${r.nodes.length} 节点 / ${r.links.length} 关系`;
        else if (r && r.relations) detail = `${r.name} · ${r.relations.length} 组关系`;
        else if (r && r.docs) detail = `${r.total} 条命中`;
        else if (r && r.text) detail = `${fmt(r.chars || r.text.length)} 字`;
        else if (r && r.answer) detail = `置信度 ${((r.guard || {}).confidence || 0).toFixed(2)}`;
        lines.push(`<div><span class="ok">✓</span> ${name} · ${detail} `
          + `<span style="color:var(--faint)">${ms} ms</span></div>`);
      } catch (e) {
        lines.push(`<div><span class="bad">✗</span> ${name} · ${esc(e.message)}</div>`);
      }
    }
    out.innerHTML = lines.join('');
  }

  Views.renderOverview = async function () {
    const box = $('#ov');
    if (ovCache) { paint(ovCache, ovSchema); return; }
    box.innerHTML = `<div class="loading" style="padding:40px"><span class="spin"></span>统计中…</div>`;
    try {
      const [st, sc] = await Promise.all([
        get('/api/stats'),
        get('/api/kg/schema').catch(() => null),
      ]);
      ovCache = st;
      ovSchema = sc;
      paint(st, sc);
    } catch (err) {
      box.innerHTML = `<div class="banner danger"><b>读取失败</b><span>${esc(err.message)}</span></div>`;
    }

    function paint(st, sc) {
      const t = st.totals;
      const dbt = (sc && sc.totals) || (S.boot && S.boot.db) || {};
      const vb = (S.boot && S.boot.vec) || {};
      const dtypes = (sc && sc.distribution && sc.distribution.types) || st.types;
      const tables = (sc && sc.tables) || [];
      const mainTables = ['entity', 'triple', 'predicate_meta', 'disease_content',
        'drug_content', 'drug_label', 'stats_monthly', 'merge_log'];

      box.innerHTML = `
        <h2>知识中枢概览</h2>
        <p class="sub">前端 → HTTP 接口 → medical_kg.db。<b style="color:var(--ink2)">图谱</b>从 entity / triple
           原样读出 node / link，<b style="color:var(--ink2)">知识库</b>对 disease_content / drug_content
           当场执行 SQL。数据库只以只读方式打开，字段和命名一律不改。</p>

        <div class="tiles">
          <div class="tile"><i>图谱实体（源库）</i><b>${(dbt.entities || 0).toLocaleString('zh-CN')}</b></div>
          <div class="tile"><i>关系三元组</i><b>${(dbt.triples || 0).toLocaleString('zh-CN')}</b></div>
          <div class="tile"><i>知识库章节</i><b>${(dbt.sections || 0).toLocaleString('zh-CN')}<small>段</small></b></div>
          <div class="tile"><i>正文字数</i><b>${fmt(Math.round((dbt.chars || 0) / 10000))}<small>万字</small></b></div>
        </div>
        <p class="sub" style="margin-top:-4px">问答使用的中枢语料另行构建：${fmt(t.entities)} 实体 ·
          ${fmt(t.edges)} 关系 · ${fmt(t.docs)} 段 / ${fmt(Math.round(t.doc_chars / 10000))} 万字
          （在源库正文之外补入临床指南与图谱档案）。两套数字同源，图谱视图展示的是源库的
          ${fmt(dbt.structural || 0)} 个结构实体。</p>
        ${vb.available ? `<p class="sub" style="margin-top:-4px">语义索引 · ${(vb.indexed || 0).toLocaleString('zh-CN')} 段 /
            ${vb.dim || 0} 维 · <b style="color:var(--ink2)">${esc(vb.model || '')}</b>
            ${vb.cooling ? '（模型暂不可用，本轮已退回字面检索）' : '　按意思找资料，与图谱锚定并行、按 2:1 补充证据。'}</p>` : ''}

        <div class="panel2" style="margin-bottom:12px">
          <h3>四个层次怎么咬合</h3>
          <div class="flow">
            <div class="fc"><b>前端 web/</b><span>四个视图全部走 fetch，没有预置答案。点一次节点/条目，就发一次请求。</span></div>
            <div class="fa">›</div>
            <div class="fc"><b>后端 server.py</b><span>标准库 HTTP，一个进程同时服务静态页、图谱、知识库与问答。</span></div>
            <div class="fa">›</div>
            <div class="fc"><b>图谱 core/kgdb.py</b><span>entity / triple 只读投影成 node / link，格式与原后端一致。</span></div>
            <div class="fa">›</div>
            <div class="fc"><b>知识库 core/kbdb.py</b><span>当场 SQL 查 disease_content / drug_content，只读、不改写。</span></div>
          </div>
        </div>

        <div class="ov-grid">
          <div class="panel2">
            <h3>实体类型分布（源库）</h3>
            ${bars(dtypes.map(x => ({ label: x.label, count: x.count, c: TYPE_COLOR[x.id] || '#7f9aa9' })), x => x.c, true)}
            <h3 style="margin-top:18px">正文按章节（中枢语料）</h3>
            ${bars(st.fields, () => '#7aa7d4')}
          </div>

          <div class="panel2">
            <h3>数据如何变成答案</h3>
            <div class="pipe">
              <div class="step"><span class="sn">1</span><div class="sb">
                <b>实体锚定</b><p>问题里的词先和图谱里的 ${fmt(t.entities)} 个实体对齐，最长优先、互不重叠。</p>
                <code>graph.link()</code></div></div>
              <div class="step"><span class="sn">2</span><div class="sb">
                <b>意图路由</b><p>判断问的是用法、禁忌、不良反应还是诊断，决定去知识库的哪个章节找。</p>
                <code>intent.classify()</code></div></div>
              <div class="step"><span class="sn">3</span><div class="sb">
                <b>混合检索</b><p>实体名下章节直接取出，再叠加全文字符二元组 BM25，两路分数融合。</p>
                <code>search.retrieve()</code></div></div>
              <div class="step"><span class="sn">4</span><div class="sb">
                <b>证据摘录</b><p>默认不自由生成，只摘被引原文的句子，每句带 [E编号]。</p>
                <code>pipeline.compose()</code></div></div>
              <div class="step"><span class="sn">5</span><div class="sb">
                <b>抗幻觉校验</b><p>引用是否真实、断言是否有支点、是否与图谱禁忌冲突，四项合成置信度。</p>
                <code>guard.verify_answer()</code></div></div>
            </div>
          </div>
        </div>

        <div class="ov-grid" style="margin-top:12px">
          <div class="panel2">
            <h3>medical_kg.db · 表结构</h3>
            <table class="schema">
              <thead><tr><th>表</th><th>用途</th><th>行数</th><th>字段</th></tr></thead>
              <tbody>
                ${mainTables.map(n => {
                  const tb = tables.find(x => x.name === n);
                  if (!tb) return '';
                  return `<tr><td><code>${esc(tb.name)}</code></td>
                    <td class="role">${esc(tb.role)}</td>
                    <td><span class="rw">${(tb.rows || 0).toLocaleString('zh-CN')}</span></td>
                    <td>${tb.columns.length}</td></tr>`;
                }).join('')}
              </tbody>
            </table>
            <p style="margin:8px 0 0;font-size:10.5px;color:var(--faint);line-height:1.7">
              ${dbt.db_mb ? `库文件 ${dbt.db_mb} MB · ` : ''}${fmt(dbt.datapoints || 0)} 个监测数据点单独存于 stats_monthly，
              不进图谱视图但可逐条回溯。</p>
          </div>

          <div class="panel2">
            <h3>前端调用的接口</h3>
            <div class="api-list">
              ${APIS.map(([m, p, d]) => `<div class="api-row">
                <span class="m">${m}</span><code>${esc(p)}</code><span class="d">${esc(d)}</span></div>`).join('')}
            </div>
            <button class="check-run" id="apiCheck">运行一次接口自检</button>
            <div class="check-out" id="checkOut"></div>
          </div>
        </div>

        <div class="ov-grid" style="margin-top:12px">
          <div class="panel2">
            <h3>按解剖系统</h3>
            ${bars(st.systems.slice(0, 10), () => '#4f9bc9')}
          </div>
          <div class="panel2">
            <h3>正文来源</h3>
            ${bars(st.sources.slice(0, 8), () => '#8f7fc4')}
            <h3 style="margin-top:16px">正文构成</h3>
            ${bars(st.kinds, () => '#d99a4e')}
          </div>
        </div>

        <div class="panel2" style="margin-top:12px">
          <h3>覆盖情况</h3>
          <p style="margin:0;font-size:12px;line-height:1.9;color:var(--muted)">
            ${fmt(t.diseases)} 个疾病实体中，<b style="color:var(--ink2)">${fmt(t.covered)}</b> 个实体有正文可引用。
            其余实体（多为传染病监测体系里的病种）没有独立章节，问答时使用由图谱属性与关系生成的
            <b style="color:var(--ink2)">「图谱档案」</b>作为证据——不臆造正文，也不假装没看见。
          </p>
        </div>`;

      const btn = $('#apiCheck');
      if (btn) btn.onclick = runCheck;
    }
  };

  /* ══════════════════════ 事件绑定 ══════════════════════ */

  document.addEventListener('DOMContentLoaded', () => {
    // 输入框
    const ta = $('#q');
    ta.addEventListener('input', () => {
      ta.style.height = 'auto';
      ta.style.height = Math.min(120, ta.scrollHeight) + 'px';
    });
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(ta.value); }
    });
    $('#send').addEventListener('click', () => ask(ta.value));

    // 模式切换：深度思考 / 仅知识库
    const btnDeep = $('#btnDeep');
    const btnKb = $('#btnKb');
    if (btnDeep) btnDeep.addEventListener('click', () => {
      btnDeep.classList.toggle('on');
      btnDeep.setAttribute('aria-pressed', btnDeep.classList.contains('on'));
    });
    if (btnKb) btnKb.addEventListener('click', () => {
      btnKb.classList.toggle('on');
      btnKb.setAttribute('aria-pressed', btnKb.classList.contains('on'));
    });

    // 会话侧边栏：新建对话 + 点击切换 + 重命名 / 删除
    const histNew = $('#histNew');
    if (histNew) histNew.addEventListener('click', () => newChat());
    const histList = $('#histList');
    if (histList) histList.addEventListener('click', e => {
      const ren = e.target.closest('[data-rename]');
      if (ren) { e.stopPropagation(); renameSession(ren.dataset.rename); return; }
      const del = e.target.closest('[data-delsess]');
      if (del) { e.stopPropagation(); deleteSession(del.dataset.delsess); return; }
      const it = e.target.closest('[data-sid]');
      if (!it) return;
      switchSession(it.dataset.sid);
    });

    $('#stream').addEventListener('click', e => {
      const s = e.target.closest('[data-sample]');
      if (s) { ta.value = s.dataset.sample; ask(s.dataset.sample); return; }

      // 打分：👍 / 👎
      const v = e.target.closest('[data-vote]');
      if (v) {
        const ans = v.closest('[data-ans]');
        if (ans) {
          const q = ans.dataset.q || '';
          const answerText = (ans.querySelector('.ans-body') || {}).textContent || '';
          const outOfKb = ans.dataset.outOfKb === '1';
          submitVote(q, answerText, v.dataset.vote, outOfKb, v);
        }
        return;
      }

      const ent = e.target.closest('[data-ent]');
      if (ent) { H.openEntity(ent.dataset.ent); return; }
      const act = e.target.closest('[data-act]');
      if (act) {
        const a = act.dataset.act;
        if (a === 'graph') {
          // 关键：取「被点的那条回答」锚定的实体，而不是 S.ask（它永远指向最新一条）。
          // 之前用 S.ask，点老消息的按钮会跳到完全不相关的实体上。
          const ansEl = act.closest('.ans');
          const i = ansEl ? Number(ansEl.dataset.idx) : -1;
          const sess = currentSession();
          const msg = sess && sess.messages[i];
          const res = (msg && msgOf(msg).res) || S.ask || {};
          const mentions = res.mentions || [];
          // 疾病才有「部位页」；药物/症状/病原没有部位，走以它为中心的关系图
          const pick = mentions.find(x => x.type === 'Disease') || mentions[0];
          if (!pick) { H.toast('这条回答没有锚定到图谱实体'); return; }
          H.go('graph');
          window.GraphView.reveal(pick.id, pick.name);
        } else if (a === 'edit') {
          editMessage(Number(act.dataset.idx));
        } else if (a === 'del') {
          deleteMessage(Number(act.dataset.idx));
        } else if (a === 'ver') {
          switchVersion(Number(act.dataset.idx), Number(act.dataset.step));
        }
        return;
      }
    });

    document.addEventListener('click', e => {
      const ex = e.target.closest('#examples button');
      if (ex) { ta.value = ex.dataset.q; ask(ex.dataset.q); }
    });
  });

  async function submitVote(q, answerText, vote, outOfKb, btn) {
    try {
      await post('/api/feedback', { question: q, answer: answerText, vote, out_of_kb: outOfKb });
      H.toast(vote === 'good' ? '已标记为有用，感谢反馈' : '已记录反馈');
      const wrap = btn && btn.parentElement;
      if (wrap) wrap.querySelectorAll('.vote-btn').forEach(b => b.classList.remove('on'));
      if (btn) btn.classList.add('on');
    } catch (err) {
      H.toast('反馈提交失败：' + err.message);
    }
  }

  Views.ask = ask;
})();
