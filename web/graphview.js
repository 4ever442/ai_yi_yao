/* 图谱视图（改造版）：部位 → 疾病 → 症状，一眼看清，点即详情
 *
 * 与旧版的区别（旧版是力导向 + 只有大节点显示名字 + 点两下才看到详情）：
 *   1. 入口是部位：头顶到脚八个部位，选中就出这个部位的图谱，不用先面对一张毛线球；
 *   2. 布局是固定的两圈：中间部位，内圈疾病，外圈常见症状。位置稳定，标签不打架；
 *   3. 所有节点都带名字，不需要悬停或点击去猜；
 *   4. 单击节点，右侧常驻面板立刻显示属性、关系、正文，不弹遮罩、不用二次点击。
 *
 * 数据：GET /api/part-graph?name=头部 一次拿全（疾病 24 / 症状 20 / 边 60 左右）。
 */
(() => {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const H = () => window.H;
  const esc = s => H().esc(s);

  const COLOR = {
    BodyRegion: '#8a8a8a',
    Disease: '#e6823c',
    Symptom: '#e5c75b',
    Drug: '#3f7fc2',
    Pathogen: '#9a6fb0',
    Complication: '#d97757',
  };

  const PROP_CN = {
    system_name: '所属系统', part: '身体部位', discipline: '学科', cfr: '病死率',
    incubation: '潜伏期', legal_class: '法定分类', department: '就诊科室', icd10: 'ICD-10',
    en_name: '英文名', drug_class: '药物类别', route: '给药途径', form: '剂型',
    brand_name: '商品名', manufacturer: '生产商', vaccine_preventable: '疫苗可预防',
    zoonotic: '动物源性', status: '上市状态', platform: '技术路线',
  };

  /* 标签/连线跟着背景明暗走：深色背景上用浅字深描边，否则白底上的深字会糊 */
  function isDark() { return document.body.classList.contains('dark-bg'); }
  function theme() {
    return isDark()
      ? { dis: '#eaf1f9', sym: '#e6dcae', halo: '#0d1621', line: '#42556b', partLine: '#5b6f86' }
      : { dis: '#243040', sym: '#5a5f46', halo: '#ffffff', line: '#e8dcc0', partLine: '#c9d6e6' };
  }

  const S = {
    part: '',
    data: null,
    chart: null,
    sel: '',
    parts: [],
    ready: false,
    cache: {},          // 部位 → 数据，来回切换不重复请求
    ego: '',            // 非空表示当前是「以某实体为中心」的关系图
    egoName: '',
    egoData: null,
    stack: [],          // 下钻路径：头部 › 化脓性迷路炎 › 头孢曲松
  };

  /* ─────────────────────────── 图表 ─────────────────────────── */
  function chart() {
    const el = $('#g2Chart');
    if (!el || !window.echarts) return null;
    if (!S.chart) {
      S.chart = echarts.init(el);
      S.chart.on('click', p => {
        if (p.dataType === 'node' && p.data.id) select(p.data.id, p.data.full || p.data.name);
      });
      // 双击：把这个实体当成中心，展开它自己的一跳关系网。
      // 部位图只能看到「这个部位里有哪些病」，症状、药物被压在圈外；
      // 双击「头痛」就能看到所有与头痛相连的疾病与药物，这是查关系的入口。
      S.chart.on('dblclick', p => {
        if (p.dataType !== 'node' || !p.data.id) return;
        if (p.data.id === S.ego) return;
        enterEgo(p.data.id, p.data.full || p.data.name);
      });
    }
    return S.chart;
  }

  function boxSize() {
    const el = $('#g2Chart');
    return { w: (el && el.clientWidth) || 720, h: (el && el.clientHeight) || 620 };
  }

  /* 固定布局：中心 → 疾病内圈 → 症状外圈。
     症状的角度取它所连疾病角度的「圆均值」，所以「头痛」会落在会引起头痛的那几个病中间，
     而不是被随机丢到某个角落。 */
  function layout(g) {
    const { w, h } = boxSize();
    const cx = w / 2, cy = h / 2;
    const R1 = Math.min(w, h) * 0.285;
    const R2 = Math.min(w, h) * 0.468;
    const pos = {}, ang = {};

    const ds = g.diseases || [];
    ds.forEach((d, i) => {
      const a = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(1, ds.length);
      // 相邻疾病错开半径：24 个病排在同一个圆上时，一圈弧长只有 50 像素左右，
      // 「急性坏死性溃疡性牙周炎」这种长名字必然压到隔壁。隔一个往里收一点就够。
      const r = (i % 2 === 0) ? R1 : R1 * 1.19;
      ang[d.id] = a;
      pos[d.id] = [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
    });

    const sAng = {};
    (g.links || []).forEach(l => {
      if (l.kind !== 'symptom') return;
      if (ang[l.source] == null) return;
      (sAng[l.target] || (sAng[l.target] = [])).push(ang[l.source]);
    });

    // 症状先按「所连疾病的圆均值」定一个理想角度，再做一次角度推挤：
    // 直接按理想角度画，「咳嗽/呕吐」这类经常同时出现的会挤到同一格上，标签叠在一起。
    const want = (g.symptoms || []).map((s, i) => {
      const list = sAng[s.id];
      let a;
      if (list && list.length) {
        let mx = 0, my = 0;
        list.forEach(x => { mx += Math.cos(x); my += Math.sin(x); });
        a = Math.atan2(my, mx);
      } else {
        a = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(1, (g.symptoms || []).length);
      }
      return { id: s.id, a };
    });

    const n = want.length;
    if (n) {
      const gap = (2 * Math.PI / n) * 0.72;
      want.sort((x, y) => x.a - y.a);
      for (let i = 1; i < n; i++) {
        if (want[i].a < want[i - 1].a + gap) want[i].a = want[i - 1].a + gap;
      }
      // 推挤后绕不回来（太挤）就退回均匀分布，宁可位置不完美也不要标签叠字
      if (want[n - 1].a - want[0].a > 2 * Math.PI - gap) {
        want.forEach((w, i) => { w.a = -Math.PI / 2 + (i * 2 * Math.PI) / n; });
      }
    }

    want.forEach((w, i) => {
      ang[w.id] = w.a;
      pos[w.id] = [cx + Math.cos(w.a) * R2, cy + Math.sin(w.a) * R2];
    });

    pos[g.regionId] = [cx, cy];
    return { pos, ang };
  }

  function labelPos(a, isCenter) {
    if (isCenter) return 'bottom';
    const c = Math.cos(a), s = Math.sin(a);
    if (c > 0.35) return 'right';
    if (c < -0.35) return 'left';
    return s < 0 ? 'top' : 'bottom';
  }

  /* 名字太长会把整幅图顶出画面（「急性坏死性溃疡性牙周炎（ANUG）」就有 16 个字）。
     图上截断，完整名字留在悬停提示和右侧详情里。 */
  function shortName(name, max) {
    const n = String(name || '');
    const limit = max || 11;
    return n.length > limit ? n.slice(0, limit) + '…' : n;
  }

  function draw(g) {
    const c = chart();
    if (!c) return;
    $('#g2Loading').hidden = true;

    const { pos, ang } = layout(g);
    const picked = S.sel;
    const T = theme();

    const nodes = [];
    nodes.push({
      id: g.regionId,
      name: g.part,
      type: 'BodyRegion',
      x: pos[g.regionId][0], y: pos[g.regionId][1],
      symbolSize: 58,
      itemStyle: {
        color: '#eef3f9', borderColor: COLOR.BodyRegion, borderWidth: 2,
        shadowBlur: 14, shadowColor: 'rgba(20,38,66,.12)',
      },
      label: {
        show: true, position: 'inside', fontSize: 13, fontWeight: 700, color: '#141d2b',
      },
      value: g.total,
    });

    (g.diseases || []).forEach(d => {
      const p = pos[d.id];
      nodes.push({
        id: d.id, name: shortName(d.name), full: d.name, type: 'Disease',
        x: p[0], y: p[1],
        symbolSize: Math.min(26, 12 + d.degree / 7),
        itemStyle: {
          color: COLOR.Disease, borderColor: '#fff',
          borderWidth: picked === d.id ? 3 : 2,
          shadowBlur: picked === d.id ? 12 : 0,
          shadowColor: 'rgba(230,130,60,.55)',
        },
        label: {
          show: true, position: labelPos(ang[d.id], false), fontSize: 12.8,
          color: T.dis, fontWeight: picked === d.id ? 700 : 500,
          textBorderColor: T.halo, textBorderWidth: 3,
        },
      });
    });

    (g.symptoms || []).forEach(s => {
      const p = pos[s.id];
      nodes.push({
        id: s.id, name: shortName(s.name, 9), full: s.name, type: 'Symptom',
        x: p[0], y: p[1],
        symbolSize: Math.min(20, 9 + s.count * 0.7),
        itemStyle: {
          color: COLOR.Symptom, borderColor: '#fff',
          borderWidth: picked === s.id ? 3 : 1.5,
        },
        label: {
          show: true, position: labelPos(ang[s.id], false), fontSize: 12.2,
          color: T.sym, fontWeight: picked === s.id ? 700 : 400,
          textBorderColor: T.halo, textBorderWidth: 3,
        },
      });
    });

    const links = (g.links || []).map(l => {
      const isPart = l.kind === 'part';
      const hot = picked && (l.source === picked || l.target === picked);
      return {
        source: l.source, target: l.target,
        value: l.label,
        lineStyle: {
          color: isPart ? T.partLine : T.line,
          width: hot ? 2 : (isPart ? 1.2 : 0.9),
          opacity: isPart ? 0.95 : (hot ? 0.95 : 0.55),
          curveness: isPart ? 0 : 0.12,
        },
      };
    });

    applyOption(nodes, links);
  }

  /* 两幅图（部位图 / 关系图）共用同一套渲染选项，只是数据不同 */
  function applyOption(nodes, links) {
    const c = chart();
    if (!c) return;
    c.setOption({
      backgroundColor: 'transparent',
      animationDuration: 380,
      tooltip: {
        backgroundColor: 'rgba(255,255,255,.98)', borderColor: '#e2e8f1', borderWidth: 1,
        textStyle: { color: '#141d2b', fontSize: 12 },
        extraCssText: 'box-shadow:0 8px 24px rgba(16,24,40,.12);border-radius:8px;padding:7px 10px;',
        formatter: p => {
          if (p.dataType === 'edge') return `<span style="color:#77839a">${esc(p.data.value || '')}</span>`;
          const cnt = p.data.value ? `<br><span style="color:#98a1ae">${p.data.value} 种疾病</span>` : '';
          return `<b>${esc(p.data.full || p.data.name)}</b>${cnt}` +
            `<br><span style="color:#98a1ae">单击看详情 · 双击展开关系</span>`;
        },
      },
      series: [{
        type: 'graph',
        layout: 'none',
        roam: true,
        draggable: true,
        cursor: 'pointer',
        scaleLimit: { min: 0.5, max: 2.4 },
        labelLayout: { hideOverlap: false },
        emphasis: {
          focus: 'adjacency',
          label: { fontWeight: 700 },
          lineStyle: { width: 2, opacity: 1 },
        },
        data: nodes,
        links,
      }],
    }, { notMerge: true });
  }

  /* ─────────────────── 以某个实体为中心的关系图 ───────────────────
     部位图回答的是「这个部位有哪些病」，关系图回答的是「这个症状/药物
     都跟谁有关」。后者靠双击进入：单击永远只是看详情，不改视图。 */
  function egoNeighbours(d, cap) {
    const items = [], seen = new Set([d.id]);
    (d.relations || []).forEach(g => {
      (g.items || []).forEach(it => {
        const id = it.other || it.id || '';
        const nm = it.other_name || it.name || '';
        if (!id || !nm || seen.has(id)) return;
        seen.add(id);
        items.push({ id, name: nm, rel: g.label || '', type: it.other_type || it.type || '' });
      });
    });
    return items.slice(0, cap || 26);
  }

  function egoLayout(items) {
    const { w, h } = boxSize();
    const cx = w / 2, cy = h / 2;
    const R = Math.min(w, h) * 0.335;
    const pos = {}, ang = {};
    items.forEach((it, i) => {
      const a = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(1, items.length);
      // 相邻节点错开半径：等分一圈时弧长只有四十来像素，长名字必然互相压住
      const r = (i % 2) ? R * 0.78 : R;
      ang[it.id] = a;
      pos[it.id] = [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
    });
    return { pos, ang, cx, cy };
  }

  function drawEgo(d) {
    const c = chart();
    if (!c) return;
    $('#g2Loading').hidden = true;
    const items = egoNeighbours(d);
    const { pos, ang, cx, cy } = egoLayout(items);
    const T = theme();
    const picked = S.sel;
    const center = COLOR[d.type] || '#5aa8a0';

    const nodes = [{
      id: d.id, name: shortName(d.name, 10), full: d.name, type: d.type,
      x: cx, y: cy, symbolSize: 58,
      itemStyle: {
        color: '#eef3f9', borderColor: center, borderWidth: 2,
        shadowBlur: 14, shadowColor: 'rgba(20,38,66,.12)',
      },
      label: { show: true, position: 'inside', fontSize: 12.5, fontWeight: 700, color: '#141d2b' },
      value: items.length ? `${items.length} 条关系` : '',
    }];

    items.forEach(it => {
      const p = pos[it.id];
      nodes.push({
        id: it.id, name: shortName(it.name, 10), full: it.name, type: it.type,
        x: p[0], y: p[1], symbolSize: 19,
        itemStyle: {
          color: COLOR[it.type] || '#7b8798', borderColor: '#fff',
          borderWidth: picked === it.id ? 3 : 2,
        },
        label: {
          show: true, position: labelPos(ang[it.id], false), fontSize: 11.5,
          color: T.dis, fontWeight: picked === it.id ? 700 : 500,
          textBorderColor: T.halo, textBorderWidth: 3,
        },
      });
    });

    const links = items.map(it => ({
      source: d.id, target: it.id, value: it.rel,
      lineStyle: { color: T.line, width: 1.15, opacity: .8, curveness: .06 },
    }));

    applyOption(nodes, links);
  }

  async function enterEgo(id, name, reuse) {
    // 回退时路径已经在栈里，不要重复压；新下钻则压到末尾
    if (!reuse) {
      const at = S.stack.findIndex(x => x.id === id);
      if (at >= 0) S.stack = S.stack.slice(0, at + 1);
      else S.stack.push({ id, name: name || id });
    }
    S.ego = id;
    S.egoName = name || '';
    S.sel = id;
    $('#g2Loading').hidden = false;
    try {
      const d = await H().get('/api/entity', { id });
      if (d.error) throw new Error(d.error);
      S.egoData = d;
      S.egoName = d.name || name;
      drawEgo(d);
      renderEgoSide(d);
      S.stack[S.stack.length - 1] = { id: d.id, name: d.name };
      $('#g2Count').textContent = `${(egoNeighbours(d).length)} 条关系`;
      renderCrumb();
      select(id, S.egoName);
    } catch (e) {
      $('#g2Loading').hidden = true;
      S.ego = '';
      H().toast('展开失败：' + e.message);
    }
  }

  /* 关系图模式下左栏换成「按关系分组的邻居」，和图上的一圈节点对应 */
  function renderEgoSide(d) {
    const side = $('#g2SideInner');
    const groups = (d.relations || []).filter(g => (g.items || []).length);
    side.innerHTML = `
      <div class="g2-side-h">${esc(d.name)}<span>${groups.length} 类关系 · 图中展示一跳邻居</span></div>
      ${groups.map(g => `
        <div class="g2-sys">
          <button class="g2-sys-h" type="button">
            <i class="g2-arrow">›</i>${esc(g.label)}<span>${g.total || g.items.length}</span>
          </button>
          <div class="g2-sys-list" hidden>${(g.items || []).map(it => {
            const id = it.other || it.id || '';
            const nm = it.other_name || it.name || '';
            return `<button type="button" class="g2-di" data-id="${esc(id)}" data-name="${esc(nm)}"
                  title="${esc(nm)}"><i class="g2-dot"></i><span class="g2-dn">${esc(nm)}</span><i class="g2-dg">›</i></button>`;
          }).join('')}</div>
        </div>`).join('')}
      <div class="g2-side-note">单击名字看详情，双击名字或图上的点，继续以它为中心展开。</div>`;

    $$('.g2-sys-h', side).forEach(btn => {
      btn.onclick = () => {
        const list = btn.parentElement.querySelector('.g2-sys-list');
        const open = list.hidden;
        list.hidden = !open;
        btn.classList.toggle('on', open);
      };
    });
    $$('.g2-di', side).forEach(el => {
      el.onclick = () => select(el.dataset.id, el.dataset.name);
      el.ondblclick = () => enterEgo(el.dataset.id, el.dataset.name);
    });
  }

  /* 唯一的重绘入口：当前在关系图就画关系图，否则画部位图。
     之前散落着十来处 draw(S.data)，只要有一处晚一步执行（mount 末尾那个
     30ms 的 setTimeout 就是），刚画好的关系图就被部位图覆盖。 */
  function redraw() {
    if (S.ego && S.egoData) drawEgo(S.egoData);
    else if (S.data) draw(S.data);
  }

  /* ─────────────── 左右面板：可拖宽、可收起 ───────────────
     248 个病名和长正文挤在固定宽度里都难读，干脆让用户自己决定给谁更多空间。
     宽度与显隐存 localStorage，下次进来还是这个样子。 */
  const PANEL_KEY = 'medhub.graph.panels';
  const P = { side: true, info: true, sideW: 246, infoW: 316 };

  function loadPanels() {
    try {
      const o = JSON.parse(localStorage.getItem(PANEL_KEY) || '{}');
      if (typeof o.side === 'boolean') P.side = o.side;
      if (typeof o.info === 'boolean') P.info = o.info;
      if (o.sideW > 120) P.sideW = o.sideW;
      if (o.infoW > 120) P.infoW = o.infoW;
    } catch (e) { /* 忽略损坏的本地设置 */ }
  }

  function savePanels() {
    try { localStorage.setItem(PANEL_KEY, JSON.stringify(P)); } catch (e) { /* 无痕模式 */ }
  }

  /* 立即应用（无动画）：用于初始加载和拖拽结束后的落定。
     关键：节点坐标是绝对像素值，resize 只改「窗口」大小、不重排节点，
     所以拖拽/收起面板时图不会跳——只有切换部位或重置时才 redraw 重排。 */
  function applyPanels(dragging) {
    const body = $('#g2Body');
    if (!body) return;
    body.classList.toggle('no-side', !P.side);
    body.classList.toggle('no-info', !P.info);
    const side = $('#g2Side'), info = $('#g2Info');
    if (side) side.style.width = P.side ? P.sideW + 'px' : '';
    if (info) info.style.width = P.info ? P.infoW + 'px' : '';
    const bs = $('#g2TglSide'), bi = $('#g2TglInfo');
    if (bs) bs.classList.toggle('on', P.side);
    if (bi) bi.classList.toggle('on', P.info);
    if (S.chart) {
      // 只 resize，不 redraw：resize 后 ECharts 按原坐标重绘，图不跳
      S.chart.resize();
    }
  }

  /* 收起/展开动画：CSS transition 驱动宽度（真实浏览器可靠）。
     内容包在 -inner 层，min-width 保持原宽，过渡中文字不会被逐字挤断。 */
  const PANEL_DUR = 300;

  function togglePanel(which) {
    const body = $('#g2Body');
    if (!body) return;
    const key = which === 'side' ? 'side' : 'info';
    const cls = 'anim-' + which;
    const noCls = 'no-' + which;
    P[key] = !P[key];

    const el = which === 'side' ? $('#g2Side') : $('#g2Info');
    const bs = which === 'side' ? $('#g2TglSide') : $('#g2TglInfo');
    const target = P[key] ? (which === 'side' ? P.sideW : P.infoW) : 0;

    if (P[key]) {
      // 展开：先解除 display:none，从 0 宽过渡到目标宽
      body.classList.remove(noCls);
      body.classList.remove(cls);
      if (bs) bs.classList.add('on');
      // 先置 0 宽（无过渡），下一帧再设目标宽触发 transition
      el.style.transition = 'none';
      el.style.width = '0px';
      void el.offsetWidth;   // 强制回流
      el.style.transition = '';
      el.style.width = target + 'px';
      // 动画全程只 resize，不 redraw，避免节点从旧坐标飞到新坐标的跳动
      let n = 0;
      const tick = setInterval(() => {
        if (S.chart) S.chart.resize();
        if (++n > PANEL_DUR / 16) { clearInterval(tick); if (S.chart) S.chart.resize(); }
      }, 16);
    } else {
      // 收起：加 anim 类（内容淡出+位移），宽度过渡到 0，结束后 display:none
      body.classList.add(cls);
      body.classList.remove(noCls);
      if (bs) bs.classList.remove('on');
      el.style.transition = '';
      el.style.width = '0px';
      let n = 0;
      const tick = setInterval(() => {
        if (S.chart) S.chart.resize();
        if (++n > PANEL_DUR / 16) {
          clearInterval(tick);
          body.classList.remove(cls);
          body.classList.add(noCls);
          if (S.chart) S.chart.resize();
        }
      }, 16);
    }
    savePanels();
  }


  function initPanels() {
    loadPanels();
    applyPanels();
    const bs = $('#g2TglSide'), bi = $('#g2TglInfo');
    if (bs) bs.onclick = () => togglePanel('side');
    if (bi) bi.onclick = () => togglePanel('info');

    $$('.g2-split').forEach(sp => {
      sp.addEventListener('mousedown', ev => {
        ev.preventDefault();
        const isLeft = sp.dataset.side === 'left';
        const el = isLeft ? $('#g2Side') : $('#g2Info');
        if (!el || el.offsetWidth === 0) return;
        const sx = ev.clientX, sw = el.offsetWidth;
        document.body.classList.add('resizing');
        sp.classList.add('dragging');
        let raf = 0;
        const move = e2 => {
          const d = isLeft ? e2.clientX - sx : sx - e2.clientX;
          const w = Math.max(150, Math.min(620, sw + d));
          if (isLeft) P.sideW = w; else P.infoW = w;
          // rAF 节流：一帧最多改一次宽度，避免 mousemove 高频触发重排
          if (!raf) raf = requestAnimationFrame(() => {
            el.style.width = w + 'px';
            if (S.chart) S.chart.resize();
            raf = 0;
          });
        };
        const up = () => {
          document.removeEventListener('mousemove', move);
          document.removeEventListener('mouseup', up);
          document.body.classList.remove('resizing');
          sp.classList.remove('dragging');
          if (raf) { cancelAnimationFrame(raf); raf = 0; }
          applyPanels();
          savePanels();
        };
        document.addEventListener('mousemove', move);
        document.addEventListener('mouseup', up);
      });
    });
  }

  /* 面包屑：部位 › 实体 › 实体…，每一段都能点回去。
     只有一个「返回」按钮时，下钻两层后就没法逐级退，这是上一版的缺陷。 */
  function renderCrumb() {
    const el = $('#g2Crumb');
    if (!el) return;
    if (!S.ego || !S.stack.length) {
      el.hidden = true;
      el.innerHTML = '';
      return;
    }
    const lis = [`<button class="g2-cr" data-depth="-1">${esc(S.part || '部位图')}</button>`];
    S.stack.forEach((x, i) => {
      const last = i === S.stack.length - 1;
      lis.push(last
        ? `<b class="g2-cr-cur">${esc(x.name)}</b>`
        : `<button class="g2-cr" data-depth="${i}">${esc(x.name)}</button>`);
    });
    el.innerHTML = lis.join('<span class="g2-sep">›</span>');
    el.hidden = false;
    $$('.g2-cr', el).forEach(b => {
      b.onclick = () => {
        const d = Number(b.dataset.depth);
        if (d < 0) { backToPart(); return; }
        S.stack = S.stack.slice(0, d + 1);
        enterEgo(S.stack[d].id, S.stack[d].name, true);
      };
    });
  }

  function backToPart() {
    S.ego = '';
    S.egoData = null;
    S.stack = [];
    renderCrumb();
    if (S.part) openPart(S.part);
  }

  /* 「重置页面」：回到刚打开时的样子——默认部位、无选中、右侧清空、缩放复位。
     以前这个按钮只清右侧面板，叫「重置」会误导。 */
  async function resetPage() {
    S.sel = '';
    S.ego = '';
    S.egoData = null;
    S.stack = [];
    renderCrumb();
    clearInfo();
    const q = $('#gq');
    if (q) q.value = '';
    const sug = $('#gsug');
    if (sug) sug.hidden = true;
    S.cache = {};
    $('#g2Count').textContent = '';
    await openPart('头部');
  }

  /* ─────────────────────────── 左栏：疾病清单 ─────────────────────────── */
  // 与人体导航页同一套系统色：两个页面看到同一批系统时，颜色也要一致
  const SYS_COLOR = {
    '神经系统': '#6f5bd0', '感觉器官': '#2f9bc6', '内分泌系统': '#18a08b', '呼吸系统': '#3f9e57',
    '循环系统': '#d64f52', '消化系统': '#dd8a33', '泌尿系统': '#3f7ec9', '生殖系统': '#bf6bb4',
    '皮肤': '#c1a13f', '运动系统': '#78859a',
  };
  const sysColor = n => SYS_COLOR[String(n || '').replace(/\s/g, '')] || '#8b98aa';

  // 只做索引，不做堆砌：默认全部折叠成「系统 + 数量」三行，
  // 想看哪个系统再展开。把 248 个病名一次摊出来，等于把图上的东西又抄一遍。
  function renderSide(g) {
    const side = $('#g2SideInner');
    const shownIds = new Set((g.diseases || []).map(d => d.id));
    side.innerHTML = `
      <div class="g2-side-h">${esc(g.part)}<span>${g.total} 种疾病 · 图中展示 ${g.diseases.length} 种</span></div>
      <div class="g2-side-tip">
        <div><i class="on">●</i>已画在这幅图里，单击高亮</div>
        <div><i>○</i>排在第 ${g.diseases.length} 名之外，单击跳到它所在的部位</div>
        <div class="tip2x">任一病名<em>双击</em>，进入以它为中心的关系图</div>
      </div>
      ${g.systems.map(s => `
        <div class="g2-sys">
          <button class="g2-sys-h" type="button">
            <i class="g2-arrow">›</i><i class="g2-sdot" style="--c:${sysColor(s.system)}"></i>${esc(s.system)}<span>${s.count}</span>
          </button>
          <div class="g2-sys-list" hidden>${s.diseases.map(d => {
            const on = shownIds.has(d.id);
            return `<button type="button" class="g2-di${on ? '' : ' off'}" data-id="${esc(d.id)}"
                  data-name="${esc(d.name)}" title="${esc(d.name)}${on ? '（已在这幅图里）' : '（不在当前图，点击跳到它所在的部位）'}"
                  ><i class="g2-dot"></i><span class="g2-dn">${esc(d.name)}</span><i class="g2-dg">›</i></button>`;
          }).join('')}</div>
        </div>`).join('')}
      <div class="g2-side-note">点系统名展开清单。名字太长会截断，鼠标停一下看全名。</div>`;

    $$('.g2-sys-h', side).forEach(btn => {
      btn.onclick = () => {
        const list = btn.parentElement.querySelector('.g2-sys-list');
        const open = list.hidden;
        list.hidden = !open;
        btn.classList.toggle('on', open);
      };
    });

    $$('.g2-di', side).forEach(el => {
      el.onclick = () => {
        const id = el.dataset.id;
        if (shownIds.has(id)) select(id, el.dataset.name);
        else focusEntity(id, el.dataset.name);
      };
      // 双击 = 直接进关系图，和图上双击节点的行为一致
      el.ondblclick = () => {
        enterEgo(el.dataset.id, el.dataset.name);
      };
    });
  }

  /* ─────────────────────────── 右栏：详情面板 ─────────────────────────── */
  function clearInfo() {
    $('#g2InfoInner').innerHTML = `
      <div class="g2-info-empty">
        <div class="g2-ie-ic">点</div>
        <b>点图上的圆点</b>
        <p>右侧立刻显示它的属性、关系和正文，不需要再点第二次。</p>
        <p class="g2-ie-tip">图例：<i style="background:${COLOR.Disease}"></i>疾病
          <i style="background:${COLOR.Symptom}"></i>症状
          <i style="background:${COLOR.BodyRegion}"></i>部位</p>
      </div>`;
  }

  function infoLoading(name) {
    $('#g2InfoInner').innerHTML = `
      <div class="g2-info-h"><h2>${esc(name)}</h2></div>
      <div class="g2-info-load"><span class="spin"></span>正在读取…</div>`;
  }

  function propRows(props) {
    const keep = Object.entries(props || {}).filter(([k, v]) =>
      PROP_CN[k] && v !== '' && v != null && typeof v !== 'object').slice(0, 6);
    if (!keep.length) return '';
    return `<div class="g2-sec">属性</div><div class="g2-props">${keep.map(([k, v]) =>
      `<div class="g2-prop"><span>${esc(PROP_CN[k])}</span><b>${esc(String(v).slice(0, 40))}</b></div>`
    ).join('')}</div>`;
  }

  function renderInfo(d) {
    const box = $('#g2InfoInner');
    if (!d || d.error) {
      box.innerHTML = `<div class="g2-info-empty"><b>没读到这个实体</b><p>${esc((d && d.error) || '')}</p></div>`;
      return;
    }
    const color = COLOR[d.type] || d.type_color || '#5aa8a0';
    const rels = (d.relations || []).slice(0, 4);
    const doc = (d.docs || [])[0];

    box.innerHTML = `
      <div class="g2-info-h">
        <span class="g2-pill" style="background:${color}1a;color:${color}">${esc(d.type_label || d.type)}</span>
        ${d.system ? `<span class="g2-pill ghost">${esc(d.system)}</span>` : ''}
        <h2>${esc(d.name)}</h2>
        ${d.aliases && d.aliases.length
          ? `<div class="g2-sub">又称：${esc(d.aliases.slice(0, 5).join('、'))}</div>` : ''}
        ${d.summary ? `<div class="g2-sum">${esc(d.summary)}</div>` : ''}
      </div>
      ${propRows(d.props)}
      ${rels.length ? rels.map(g => `
        <div class="g2-sec">${esc(g.label)}<span class="g2-cnt">${g.total}</span></div>
        <div class="g2-rel">${g.items.slice(0, 10).map(it => {
          const nm = it.other_name || it.name || it.other || '';
          const id = it.other || it.id || '';
          return id ? `<span class="g2-chip" data-id="${esc(id)}" data-name="${esc(nm)}">${esc(nm)}</span>`
                    : `<span class="g2-chip off">${esc(nm)}</span>`;
        }).join('')}</div>`).join('') : ''}
      ${doc ? `
        <div class="g2-sec">正文 · ${esc(doc.field_label || doc.title || '')}
          ${doc.chars ? `<span class="g2-cnt">${doc.chars} 字</span>` : ''}</div>
        <div class="g2-doc" data-doc-body>${esc((doc.excerpt || '').slice(0, 220)) || '<i>该实体没有正文段落，仅图谱关系</i>'}</div>
        ${(doc.chars || 0) > 180 || (doc.excerpt || '').length >= 150
          ? `<button class="g2-more" type="button" data-more="${esc(doc.id)}">显示全文</button>` : ''}`
        : ''}
      <div class="g2-info-acts">
        <button class="btn" data-ask>用「${esc(d.name)}」提问</button>
        ${doc && doc.id ? `<button class="btn ghost" data-doc="${esc(doc.id)}">查看全文</button>` : ''}
      </div>`;

    $$('.g2-chip[data-id]', box).forEach(el => {
      el.onclick = () => focusEntity(el.dataset.id, el.dataset.name);
    });
    const bAsk = $('[data-ask]', box);
    if (bAsk) bAsk.onclick = () => {
      const q = $('#q');
      if (q) { q.value = d.name + ' 是什么？'; q.focus(); }
      H().go('ask');
      H().toast('已把实体填进提问框');
    };
    // 就地展开：不去知识库跳一圈，用户只想把这段话读完
    const bMore = $('[data-more]', box);
    if (bMore) bMore.onclick = async () => {
      const id = bMore.dataset.more;
      bMore.disabled = true;
      bMore.textContent = '读取中…';
      try {
        let full = null;
        try { full = await H().get('/api/doc', { id }); } catch (e) { full = null; }
        if ((!full || !full.text) && /^label:/.test(id)) {
          // 药品说明书章节 id 形如 label:DRG_X:dosage，转成知识库的 section id
          const m = /^label:(.+):([a-z_]+)$/.exec(id);
          if (m) full = await H().get('/api/kb/section', { id: `db:drug:${m[1]}:${m[2]}` });
        }
        const body = $('[data-doc-body]', box);
        if (body && full && full.text) {
          body.textContent = full.text;
          body.classList.add('full');
          bMore.remove();
        } else {
          bMore.disabled = false;
          bMore.textContent = '显示全文';
          H().toast('这一段读不到更长的内容');
        }
      } catch (e) {
        bMore.disabled = false;
        bMore.textContent = '显示全文';
        H().toast('读取失败：' + e.message);
      }
    };

    const bDoc = $('[data-doc]', box);
    if (bDoc) bDoc.onclick = () => {
      H().go('kb');
      if (window.KB && window.KB.openDoc) window.KB.openDoc(bDoc.dataset.doc);
    };
  }

  async function select(id, name) {
    S.sel = id;
    redraw();
    infoLoading(name || id);
    try {
      const d = await H().get('/api/entity', { id });
      if (S.sel !== id) return;
      renderInfo(d);
    } catch (e) {
      $('#g2InfoInner').innerHTML = `<div class="g2-info-empty"><b>读取失败</b><p>${esc(e.message)}</p></div>`;
    }
  }

  /* 打开某个实体：先切到它所在的部位，再选中 */
  function focusEntity(id, name) {
    return reveal(id, name);
  }

  /* 部位图只画连接度最高的 24 个病。问答里点到的病可能不在其中，
     那就把它补进图里（连同一条「位于」边和左栏条目），否则切过去是一片空白。 */
  function ensureDisease(ent, part) {
    const g = S.data;
    if (!g || g.part !== part) return;
    if ((g.diseases || []).some(d => d.id === ent.id)) return;

    const row = { id: ent.id, name: ent.name, degree: ent.degree || 0,
                  system: ent.system || '其他', injected: true };
    g.diseases.push(row);
    g.links.push({ source: g.regionId || ('PART:' + part), target: ent.id,
                   label: '位于', kind: 'part' });

    const sys = (g.systems || []).find(x => x.system === row.system);
    if (sys) {
      if (!sys.diseases.some(d => d.id === ent.id)) sys.diseases.unshift(row);
      sys.count = sys.diseases.length;
    } else {
      g.systems = g.systems || [];
      g.systems.unshift({ system: row.system, count: 1, diseases: [row] });
    }
    renderSide(g);
    const cnt = $('#g2Count');
    if (cnt) cnt.textContent = `${g.diseases.length} 种疾病 · ${g.symptoms.length} 个表现`;
  }

  /* 外部定位入口（问答「在图谱中查看」、深链、详情里的实体 chip 都走这里）：
     目标必须出现在图上，且详情面板要打开——这是「跳过去有东西看」的最低要求。
       · 疾病 → 切到它所在的部位，不在展示名单里就补进去
       · 药物 / 症状 / 病原没有部位页 → 改用「以它为中心」的关系图 */
  async function reveal(key, name) {
    if (!key) return;
    if (!S.ready || (!S.part && !S.ego)) await mount();

    let ent = null;
    try { ent = await H().get('/api/entity', { id: key }); } catch (e) { ent = null; }
    if (!ent || ent.error) {
      // 传进来的可能是名字而不是 ID
      try {
        const r = await H().get('/api/kg/search', { q: name || key, limit: 1 });
        if (r.items && r.items.length) return reveal(r.items[0].id, r.items[0].name);
      } catch (e) { /* 落到提示 */ }
      $('#g2Loading').hidden = true;
      H().toast('没找到实体：' + (name || key));
      return;
    }

    // 详情面板被收起过就先展开，否则用户看到的正是「跳过去没有详细信息」
    if (!P.info) togglePanel('info');

    const part = ent.region || (ent.props && ent.props.part) || '';
    if (ent.type === 'Disease' && part) {
      if (S.ego || S.part !== part) await openPart(part);
      S.sel = ent.id;
      ensureDisease(ent, part);
      redraw();
      renderInfo(ent);
      addCtxSafe(ent);
      // 补进来的节点在圈上可能被挤到图外，重置一下视野
      if (ent.degree && S.chart) setTimeout(() => { if (S.chart) S.chart.resize(); }, 40);
    } else {
      if (S.ego === ent.id && S.egoData) {
        S.sel = ent.id;
        redraw();
      } else {
        await enterEgo(ent.id, ent.name);   // drawEgo + select() 自带详情
      }
    }
  }

  function addCtxSafe(d) {
    try { H().addCtx({ id: d.id, name: d.name, type: d.type, type_label: d.type_label }); } catch (e) { /* 上下文已收起，失败无所谓 */ }
  }

  /* ─────────────────────────── 部位切换 ─────────────────────────── */
  function paintParts() {
    const bar = $('#g2Parts');
    bar.innerHTML = S.parts.map(p =>
      `<button class="g2-part${p.name === S.part ? ' on' : ''}" data-p="${esc(p.name)}">
         ${esc(p.name)}<i>${p.count}</i></button>`).join('');
    $$('.g2-part', bar).forEach(el => { el.onclick = () => openPart(el.dataset.p); });
  }

  async function openPart(name) {
    S.part = name;
    S.sel = '';
    // 从关系图切回部位图：状态要跟着清掉，否则「重置」会画错一张图
    S.ego = '';
    S.egoData = null;
    S.stack = [];
    renderCrumb();
    paintParts();
    $('#g2Loading').hidden = false;

    let g = S.cache[name];
    if (!g) {
      try {
        g = await H().get('/api/part-graph', { name });
      } catch (e) {
        $('#g2Loading').hidden = true;
        H().toast('加载失败：' + e.message);
        return;
      }
      if (g.error) { $('#g2Loading').hidden = true; H().toast(g.error); return; }
      S.cache[name] = g;
    }
    S.data = g;
    renderSide(g);
    $('#g2Count').textContent = `${g.diseases.length} 种疾病 · ${g.symptoms.length} 个表现`;
    draw(g);
    clearInfo();
  }

  /* ─────────────────────────── 搜索 ─────────────────────────── */


  /* ─────────────────────────── 入口 ─────────────────────────── */
  /* mount 会被两条路径同时触发：切视图时调用一次、搜索跳转时 enterEgo 又要等它。
     不合并的话，部位图会在关系图之后才画完，把关系图覆盖掉。 */
  let mountPromise = null;
  function mount() {
    if (!mountPromise) {
      mountPromise = doMount().catch(e => { throw e; })
        .finally(() => { setTimeout(() => { mountPromise = null; }, 0); });
    }
    return mountPromise;
  }

  async function doMount() {
    const c = chart();
    if (c) setTimeout(() => c.resize(), 30);

    if (!S.ready) {
      S.ready = true;
      $('#g2Catalog').onclick = () => { location.href = 'catalog.html'; };
      const bReset = $('#g2Fit');
      if (bReset) bReset.onclick = resetPage;
      initPanels();

      window.addEventListener('resize', () => {
        if (!S.chart) return;
        S.chart.resize();
      });

      // 换了背景就重算一次配色，深色模式下标签要换成浅色
      document.addEventListener('bgchange', redraw);

      try {
        const d = await H().get('/api/parts');
        S.parts = d.items || [];
      } catch (e) {
        H().toast('部位目录加载失败');
        return;
      }
      paintParts();
    }

    if (!S.part) {
      const p = new URLSearchParams((location.hash.split('?')[1]) || '');
      await openPart(p.get('part') || '头部');
    } else {
      paintParts();
      redraw();
      setTimeout(() => { if (S.chart) { S.chart.resize(); redraw(); } }, 30);
    }
  }

  window.GraphView = {
    mount,
    /* 深链 / 外部调用：直接进以某实体为中心的关系图 */
    async enterEgo(key) {
      if (!S.ready) await mount();
      if (!key) return;
      let id = key, name = '';
      if (!/^[A-Z]{2,4}_/.test(key)) {
        try {
          const r = await H().get('/api/kg/search', { q: key, limit: 1 });
          if (r.items && r.items.length) { id = r.items[0].id; name = r.items[0].name; }
        } catch (e) { /* 当作 ID 用 */ }
      }
      enterEgo(id, name);
    },
    resize() { if (S.chart) { S.chart.resize(); redraw(); } },
    /* 深链和「在图谱中查看」传过来的可能是实体 ID（DIS_偏头痛），也可能是名字。
       统一走 reveal：疾病进它所在的部位页（不在展示名单就补进去），
       其它类型以它为中心展开关系图——总之目标一定在图上、详情一定打开。 */
    async focus(key, _strict) {
      return reveal(key);
    },
    reveal,
  };
})();
