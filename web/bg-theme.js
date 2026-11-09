/* ==========================================================
   bg-theme.js —— 共享背景主题
   · 预设：纯色 / 渐变 / 光束 / 纹理 / 深色
   · 自定义：纯色、双色渐变（可调角度）、本地图片或图片链接（可调遮罩）
   · 选择会写入 localStorage，四个页面与 iframe 共享
   用法：
     BGTheme.mount(el)   把背景选择区渲染进指定容器（如设置面板）
     BGTheme.open()      打开右侧抽屉（页面上的 .bgt-trigger 会自动绑定）
   ========================================================== */
(function () {
  'use strict';

  var KEY = 'app-bg';
  var KEY_CSS = 'app-bg-css';
  var KEY_DARK = 'app-bg-dark';

  var PRESETS = [
    /* ---- 纯色 ---- */
    { id: 'plain',  name: '纯净白', kind: 'solid',    css: '#f7f9fb' },
    { id: 'cloud',  name: '云灰',   kind: 'solid',    css: '#f1f3f7' },
    { id: 'sand',   name: '米砂',   kind: 'solid',    css: '#faf5ec' },
    { id: 'sky2',   name: '雾蓝',   kind: 'solid',    css: '#eef4fa' },
    { id: 'mint2',  name: '薄荷',   kind: 'solid',    css: '#edf8f3' },
    { id: 'rose2',  name: '藕粉',   kind: 'solid',    css: '#fbf0f3' },
    /* ---- 渐变 ---- */
    { id: 'mist',   name: '晨雾蓝', kind: 'gradient', css: 'linear-gradient(145deg,#f0f9ff 0%,#dcebfb 100%)' },
    { id: 'sunset', name: '暖阳',   kind: 'gradient', css: 'linear-gradient(150deg,#fff8ee 0%,#ffe9d4 55%,#ffe0e6 100%)' },
    { id: 'mint',   name: '薄荷青', kind: 'gradient', css: 'linear-gradient(160deg,#f1fdf7 0%,#d9f3e8 55%,#e6f4ff 100%)' },
    { id: 'sakura', name: '樱粉',   kind: 'gradient', css: 'linear-gradient(160deg,#fff5f8 0%,#ffe4ee 55%,#f1eafd 100%)' },
    { id: 'lilac',  name: '紫雾',   kind: 'gradient', css: 'linear-gradient(160deg,#f7f4ff 0%,#e9e2ff 55%,#e5efff 100%)' },
    { id: 'salt',   name: '海盐',   kind: 'gradient', css: 'linear-gradient(160deg,#effbfc 0%,#d6eef2 55%,#e4f1fb 100%)' },
    /* ---- 光束 / 纹理 ---- */
    { id: 'aurora', name: '极光',   kind: 'mesh',
      css: 'radial-gradient(900px 540px at 10% 4%, #d9eeff 0%, rgba(217,238,255,0) 62%),' +
           'radial-gradient(760px 520px at 92% 8%, #eee2ff 0%, rgba(238,226,255,0) 60%),' +
           'radial-gradient(880px 620px at 58% 104%, #dcf7ec 0%, rgba(220,247,236,0) 62%),' +
           'linear-gradient(150deg,#f8fbff 0%,#f1f6ff 100%)' },
    { id: 'beam',   name: '斜光',   kind: 'mesh',
      css: 'linear-gradient(118deg, rgba(255,255,255,0) 38%, rgba(255,255,255,.9) 50%, rgba(255,255,255,0) 62%),' +
           'radial-gradient(760px 480px at 18% 12%, #e3f3ff 0%, rgba(227,243,255,0) 60%),' +
           'linear-gradient(150deg,#f7fbff 0%,#eaf2fb 100%)' },
    { id: 'dots',   name: '点阵',   kind: 'pattern',
      css: 'radial-gradient(circle at 1px 1px, rgba(20,60,100,.085) 1px, transparent 0) 0 0/20px 20px,' +
           'linear-gradient(160deg,#f7fbfd 0%,#eaf2f9 100%)' },
    { id: 'grid',   name: '网格',   kind: 'pattern',
      css: 'linear-gradient(rgba(20,60,100,.05) 1px, transparent 1px) 0 0/26px 26px,' +
           'linear-gradient(90deg, rgba(20,60,100,.05) 1px, transparent 1px) 0 0/26px 26px,' +
           'linear-gradient(160deg,#f9fbfd 0%,#eef3f8 100%)' },
    /* ---- 深色 ---- */
    { id: 'night',  name: '暗夜',   kind: 'dark',     css: 'linear-gradient(145deg,#233140 0%,#111a24 100%)' },
    { id: 'ink',    name: '墨蓝',   kind: 'dark',     css: 'linear-gradient(150deg,#14242f 0%,#0b1620 58%,#131c2e 100%)' },
    { id: 'forest', name: '深林',   kind: 'dark',     css: 'linear-gradient(150deg,#152619 0%,#0c1a12 100%)' },
    /* ---- 医疗背景图 ---- 素材放在 web/bg/，改图直接换同名文件即可 */
    { id: 'img1', name: '十字光斑', kind: 'image', group: 'image',
      css: 'linear-gradient(rgba(255,255,255,.18),rgba(255,255,255,.18)), url("bg/img1.jpg")' },
    { id: 'img2', name: '分子结构', kind: 'image', group: 'image',
      css: 'linear-gradient(rgba(255,255,255,.26),rgba(255,255,255,.26)), url("bg/img2.jpg")' },
    { id: 'img3', name: '医疗科技', kind: 'image', group: 'image',
      css: 'linear-gradient(rgba(255,255,255,.52),rgba(255,255,255,.52)), url("bg/img3.jpg")' },
    { id: 'img4', name: '六边形',   kind: 'image', group: 'image',
      css: 'linear-gradient(rgba(255,255,255,.34),rgba(255,255,255,.34)), url("bg/img4.png")' }
  ];

  var DEFAULT_ID = 'img2';   // 默认用本项目自带的医疗背景图（见 web/bg/）
  var DEFAULT_CUSTOM = { mode: 'solid', c1: '#eef4fa', c2: '#dbe9f8', angle: 145, img: '', veil: 50 };

  var state = { id: DEFAULT_ID, custom: clone(DEFAULT_CUSTOM) };
  var roots = [];          // 已挂载的容器
  var drawerEl = null;

  /* ---------------- 工具 ---------------- */
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }

  function getPreset(id) {
    for (var i = 0; i < PRESETS.length; i++) if (PRESETS[i].id === id) return PRESETS[i];
    return null;
  }

  /* 颜色明暗判断 */
  function isDarkColor(c) {
    if (!c || typeof c !== 'string') return false;
    var hex = c.trim().replace('#', '');
    if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    if (!/^[0-9a-f]{6}$/i.test(hex)) return false;
    var r = parseInt(hex.substr(0, 2), 16);
    var g = parseInt(hex.substr(2, 2), 16);
    var b = parseInt(hex.substr(4, 2), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) < 120;
  }

  /* 解析出最终的 background 值 */
  function resolve() {
    var p = getPreset(state.id);
    if (p) return { css: p.css, dark: p.kind === 'dark', image: p.kind === 'image' };

    var c = state.custom || {};
    if (c.mode === 'image' && c.img) {
      var v = (c.veil == null ? 45 : +c.veil) / 100;
      return {
        css: 'linear-gradient(rgba(255,255,255,' + v + '),rgba(255,255,255,' + v + ')), url("' + c.img + '")',
        dark: false,
        image: true
      };
    }
    if (c.mode === 'grad') {
      return {
        css: 'linear-gradient(' + (c.angle || 0) + 'deg,' + c.c1 + ' 0%,' + c.c2 + ' 100%)',
        dark: isDarkColor(c.c1) && isDarkColor(c.c2),
        image: false
      };
    }
    return { css: c.c1 || '#eef4fa', dark: isDarkColor(c.c1), image: false };
  }

  /* ---------------- 应用 ---------------- */
  function apply(persist) {
    var r = resolve();
    var css = r.css;
    var dark = r.dark;

    var html = document.documentElement;
    var body = document.body;

    /* html 先上色，避免首屏白闪 */
    html.style.background = css;
    html.style.backgroundSize = r.image ? 'cover' : 'auto';
    html.style.backgroundPosition = 'center';
    html.style.backgroundRepeat = 'no-repeat';
    html.style.backgroundAttachment = 'fixed';

    if (body) {
      body.style.background = css;
      body.style.backgroundSize = r.image ? 'cover' : 'auto';
      body.style.backgroundPosition = 'center';
      body.style.backgroundRepeat = 'no-repeat';
      body.style.backgroundAttachment = 'fixed';
      body.classList.toggle('dark-bg', dark);
    }
    html.classList.toggle('dark-bg', dark);

    /* 触发一次同步重排：固定背景层在部分浏览器里会被缓存，切主题后需要失效重绘 */
    void (body ? body.offsetHeight : 0);

    if (persist !== false) {
      try {
        localStorage.setItem(KEY, JSON.stringify(state));
        localStorage.setItem(KEY_CSS, css);
        localStorage.setItem(KEY_DARK, dark ? '1' : '0');
      } catch (e) {
        toast('背景太大，未能保存（本次仍生效）');
      }
    }

    syncAll();
    broadcast(css, dark);
    document.dispatchEvent(new CustomEvent('bgchange', { detail: { id: state.id, dark: dark, css: css } }));
  }

  /* 通知同源 iframe */
  function broadcast(css, dark) {
    $$('iframe').forEach(function (f) {
      try { f.contentWindow && f.contentWindow.postMessage({ __bgTheme: 1, css: css, dark: dark }, '*'); } catch (e) { }
    });
  }

  /* ---------------- 状态同步到 UI ---------------- */
  function syncAll() {
    roots.forEach(sync);
    if (drawerEl) sync($('.bgt-dbody', drawerEl));
  }

  function sync(root) {
    if (!root) return;
    $$('.bgt-sw', root).forEach(function (el) {
      el.classList.toggle('on', el.getAttribute('data-id') === state.id);
    });
    var c = state.custom;
    $$('.bgt-tabs button', root).forEach(function (b) {
      b.classList.toggle('on', b.getAttribute('data-m') === c.mode);
    });
    $$('.bgt-pane', root).forEach(function (p) {
      p.classList.toggle('on', p.getAttribute('data-pane') === c.mode);
    });

    /* 回填控件值（不打断正在输入的框） */
    var pane = $('.bgt-pane[data-pane="' + c.mode + '"]', root) || root;
    $$('input[data-k]', pane).forEach(function (inp) {
      if (inp === document.activeElement) return;
      var k = inp.getAttribute('data-k');
      if (k === 'angle') inp.value = c.angle;
      else if (k === 'veil') inp.value = c.veil;
      else if (k === 'img') { if (!/^data:/.test(c.img || '')) inp.value = c.img || ''; }
      else if (c[k] != null) inp.value = c[k];
    });
    $$('em[data-val]', pane).forEach(function (em) {
      var k = em.getAttribute('data-val');
      em.textContent = k === 'angle' ? (c.angle + '°') : (c.veil + '%');
    });

    $$('em[data-hex]', root).forEach(function (em) {
      var k = em.getAttribute('data-hex');
      em.textContent = (c[k] || '').toUpperCase();
    });

    /* 预览条 */
    var pv = $('.bgt-preview', root);
    if (pv) {
      var r = resolve();
      pv.style.background = r.css;
      pv.style.backgroundSize = r.image ? 'cover' : 'auto';
    }

    /* 滑杆已走过的颜色 */
    $$('input[type=range][data-k]', pane).forEach(function (inp) {
      var max = +inp.max || 100;
      var v = +inp.value;
      var pct = Math.round(v / max * 100);
      inp.style.background = 'linear-gradient(90deg,var(--bgt-accent) ' + pct + '%,#c9d3df ' + pct + '%)';
    });
  }

  /* ---------------- 生成 DOM ---------------- */
  function gridHTML(group, exclude) {
    return PRESETS.filter(function (p) {
      if (group && p.group !== group) return false;
      if (exclude && p.group === exclude) return false;
      return true;
    }).map(function (p) {
      return '<button type="button" class="bgt-sw" data-id="' + p.id + '" title="' + p.name + '">' +
        '<span class="bgt-thumb" style="background:' + p.css + ';background-size:cover"></span>' +
        '<span class="bgt-nm">' + p.name + '</span></button>';
    }).join('');
  }

  function sectionHTML() {
    return '' +
      '<div class="bgt-lab">显示模式</div>' +
      '<div class="bgt-modes" id="bgtModes">正在读取…</div>' +

      '<div class="bgt-lab">医疗背景图</div>' +
      '<div class="bgt-grid">' + gridHTML('image') + '</div>' +

      '<div class="bgt-lab">纯色 / 渐变</div>' +
      '<div class="bgt-grid">' + gridHTML(null, 'image') + '</div>' +

      '<div class="bgt-lab">自定义</div>' +
      '<div class="bgt-custom">' +
        '<div class="bgt-tabs">' +
          '<button type="button" data-m="solid">纯色</button>' +
          '<button type="button" data-m="grad">渐变</button>' +
          '<button type="button" data-m="image">图片</button>' +
        '</div>' +

        '<div class="bgt-pane" data-pane="solid">' +
          '<div class="bgt-row"><span>底色</span><input type="color" data-k="c1"><em data-hex="c1"></em></div>' +
        '</div>' +

        '<div class="bgt-pane" data-pane="grad">' +
          '<div class="bgt-row"><span>起始</span><input type="color" data-k="c1"></div>' +
          '<div class="bgt-row"><span>结束</span><input type="color" data-k="c2"></div>' +
          '<div class="bgt-row"><span>角度</span><input type="range" min="0" max="360" step="5" data-k="angle"><em data-val="angle"></em></div>' +
        '</div>' +

        '<div class="bgt-pane" data-pane="image">' +
          '<div class="bgt-row"><span>图片</span>' +
            '<label class="bgt-file">选择本地图片<input type="file" accept="image/*" data-k="file" hidden></label>' +
          '</div>' +
          '<div class="bgt-row"><span>链接</span><input type="text" class="bgt-url" data-k="img" placeholder="或粘贴图片地址"></div>' +
          '<div class="bgt-row"><span>遮罩</span><input type="range" min="0" max="85" step="5" data-k="veil"><em data-val="veil"></em></div>' +
          '<div class="bgt-note">遮罩越高，背景越淡、文字越清楚</div>' +
        '</div>' +

        '<div class="bgt-preview"></div>' +
      '</div>' +
      '<button type="button" class="bgt-reset" data-role="reset">恢复默认背景</button>';
  }

  function bind(root) {
    root.addEventListener('click', function (e) {
      var sw = e.target.closest ? e.target.closest('.bgt-sw') : null;
      if (sw) { state.id = sw.getAttribute('data-id'); apply(); return; }

      var tab = e.target.closest ? e.target.closest('.bgt-tabs button') : null;
      if (tab) {
        state.custom.mode = tab.getAttribute('data-m');
        state.id = 'custom';
        apply();
        return;
      }
      if (e.target.closest && e.target.closest('[data-role="reset"]')) {
        state.id = DEFAULT_ID;
        state.custom = clone(DEFAULT_CUSTOM);
        apply();
        return;
      }
    });

    root.addEventListener('input', function (e) {
      var t = e.target;
      var k = t.getAttribute && t.getAttribute('data-k');
      if (!k || k === 'file') return;
      var pane = t.closest('.bgt-pane');
      var mode = pane ? pane.getAttribute('data-pane') : 'solid';

      if (mode === 'image') {
        if (k === 'img') state.custom.img = t.value.trim();
        else if (k === 'veil') state.custom.veil = +t.value;
        state.custom.mode = 'image';
      } else if (mode === 'grad') {
        if (k === 'angle') state.custom.angle = +t.value;
        else state.custom[k] = t.value;
        state.custom.mode = 'grad';
      } else {
        state.custom.c1 = t.value;
        state.custom.mode = 'solid';
      }
      state.id = 'custom';
      apply();
    });

    root.addEventListener('change', function (e) {
      var t = e.target;
      if (t.getAttribute && t.getAttribute('data-k') === 'file' && t.files && t.files[0]) {
        readImage(t.files[0]);
        t.value = '';
      }
    });
  }

  /* 图片读入 + 压缩，避免撑爆 localStorage */
  function readImage(file) {
    if (!/^image\//.test(file.type)) { toast('请选择图片文件'); return; }
    var url = URL.createObjectURL(file);
    var img = new Image();
    img.onload = function () {
      var max = 1920;
      var w = img.naturalWidth, h = img.naturalHeight;
      var scale = Math.min(1, max / Math.max(w, h));
      w = Math.round(w * scale); h = Math.round(h * scale);
      var cv = document.createElement('canvas');
      cv.width = w; cv.height = h;
      cv.getContext('2d').drawImage(img, 0, 0, w, h);
      var data = cv.toDataURL('image/jpeg', 0.82);
      URL.revokeObjectURL(url);
      state.custom.img = data;
      state.custom.mode = 'image';
      state.id = 'custom';
      apply();
    };
    img.onerror = function () { URL.revokeObjectURL(url); toast('图片读取失败'); };
    img.src = url;
  }

  /* ---------------- 抽屉 ---------------- */
  function ensureDrawer() {
    if (drawerEl) return drawerEl;

    var ov = document.createElement('div');
    ov.className = 'bgt-overlay';

    var d = document.createElement('aside');
    d.className = 'bgt-drawer';
    d.innerHTML =
      '<div class="bgt-dhead">' +
        '<div><b>背景样式</b><span>选一套预设，或自己配色 / 传图</span></div>' +
        '<button type="button" class="bgt-x" title="关闭">×</button>' +
      '</div>' +
      '<div class="bgt-dbody">' + sectionHTML() + '</div>';

    document.body.appendChild(ov);
    document.body.appendChild(d);
    drawerEl = d;
    d.__overlay = ov;

    bind($('.bgt-dbody', d));

    function close() { d.classList.remove('on'); ov.classList.remove('on'); }
    ov.addEventListener('click', close);
    $('.bgt-x', d).addEventListener('click', close);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });

    return d;
  }

  /* 显示模式：青年版已就绪，长辈版只留接口占位。
     真要做长辈版不是调字号那么简单——整个信息密度、点击热区、图谱都要重排，
     所以这里不假装能用，直接标「开发中」。 */
  function loadModes() {
    var box = document.getElementById('bgtModes');
    if (!box) return;
    fetch('/api/ui/profile').then(function (r) { return r.json(); }).then(function (d) {
      var cur = d.mode || 'youth';
      box.innerHTML = (d.modes || []).map(function (m) {
        var on = m.id === cur;
        var dis = !m.ready;
        return '<button type="button" class="bgt-mode' + (on ? ' on' : '') + (dis ? ' dis' : '') +
          '" ' + (dis ? 'disabled' : '') + ' data-mode="' + m.id + '">' +
          '<b>' + m.name + '</b><span>' + (m.note || '') + '</span></button>';
      }).join('');
      Array.prototype.forEach.call(box.querySelectorAll('.bgt-mode:not(.dis)'), function (b) {
        b.onclick = function () { applyMode(b.getAttribute('data-mode')); };
      });
    }).catch(function () {
      box.innerHTML = '<div class="bgt-mode on"><b>青年版</b><span>默认字号</span></div>';
    });
  }

  function applyMode(id) {
    fetch('/api/ui/profile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: id })
    }).then(function (r) { return r.json(); }).then(function () {
      document.documentElement.setAttribute('data-font-mode', id);
      loadModes();
    });
  }

  function open() {
    var d = ensureDrawer();
    sync($('.bgt-dbody', d));
    loadModes();
    d.classList.add('on');
    d.__overlay.classList.add('on');
  }
  function close() {
    if (!drawerEl) return;
    drawerEl.classList.remove('on');
    drawerEl.__overlay.classList.remove('on');
  }

  /* 把「背景选择区」渲染到自定义容器里 */
  function mount(el) {
    if (!el) return;
    el.innerHTML = sectionHTML();
    bind(el);
    roots.push(el);
    sync(el);
  }

  /* ---------------- 读取已保存设置 ---------------- */
  function load() {
    /* 支持 ?bg=img2 直接从链接指定背景，便于分享和截图核对 */
    try {
      var qid = new URLSearchParams(location.search).get('bg');
      if (qid && getPreset(qid)) { state.id = qid; return; }
    } catch (e) { }

    var raw = null;
    try { raw = localStorage.getItem(KEY); } catch (e) { }

    if (raw) {
      /* 兼容旧版：'bg-default' 这类纯字符串 */
      if (raw.charAt(0) === '{') {
        try {
          var o = JSON.parse(raw);
          if (o && o.id) state.id = o.id;
          if (o && o.custom) state.custom = Object.assign(clone(DEFAULT_CUSTOM), o.custom);
        } catch (e) { }
      } else {
        var legacy = { 'bg-default': 'mist', 'bg-solid': 'plain', 'bg-warm': 'sunset', 'bg-dark': 'night' };
        state.id = legacy[raw] || DEFAULT_ID;
      }
    }
  }

  /* ---------------- 提示条 ---------------- */
  var toastTimer = null;
  function toast(msg) {
    var t = $('.bgt-toast');
    if (!t) {
      t = document.createElement('div');
      t.className = 'bgt-toast';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('on'); }, 2400);
  }

  /* ---------------- 启动 ---------------- */
  load();
  apply(false);   // 立刻给 html 上色

  function boot() {
    apply(false);
    /* 顶栏触发按钮 */
    $$('.bgt-trigger').forEach(function (b) {
      b.addEventListener('click', open);
    });
    /* iframe 载入后再同步一次 */
    $$('iframe').forEach(function (f) {
      f.addEventListener('load', function () {
        var r = resolve();
        try { f.contentWindow && f.contentWindow.postMessage({ __bgTheme: 1, css: r.css, dark: r.dark }, '*'); } catch (e) { }
      });
    });
    /* 其它标签页改了背景，本页跟随 */
    window.addEventListener('storage', function (e) {
      if (e.key === KEY) { load(); apply(false); }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  /* 对外接口 */
  window.BGTheme = {
    mount: mount,
    open: open,
    close: close,
    presets: PRESETS,
    current: function () { return state.id; },
    set: function (id) { state.id = id; apply(); },
    refresh: function () { apply(false); }
  };
})();
