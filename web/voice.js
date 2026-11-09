/* 语音输入 · 浏览器原生识别（Web Speech API）
   ────────────────────────────────────────────────────────────────
   为什么用浏览器原生识别，而不是本地模型：
   本机环境没有 whisper / vosk / funasr，也没有 ffmpeg 与 numpy，装一套离线模型
   要几百 MB 且首次识别要几十秒。Web Speech API 零依赖、边说边出字，代价是需要联网：
   Edge 走 Azure、Chrome 走 Google，都属于在线识别。

   所以在不可用时**不要静默消失**，按钮置灰并说明原因；识别失败也给出可操作的中文提示。
   ──────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  /* 测试时可注入假的识别器，走同一套状态机与渲染路径 */
  const Ctor = window.__VOICE_MOCK__ || window.SpeechRecognition || window.webkitSpeechRecognition || null;
  const SECURE = window.isSecureContext !== false;
  const UNSUPPORTED = '当前浏览器不支持语音输入（Firefox 等暂不支持）。换成 Edge 或 Chrome 打开即可，也可以直接打字。';

  const ERR = {
    'not-allowed': '麦克风权限被拒绝。点地址栏左侧的锁形图标，把「麦克风」改成允许，然后刷新重试。',
    'service-not-allowed': '浏览器禁止了语音识别服务。请到系统「隐私与安全 → 麦克风」里允许这个浏览器使用麦克风。',
    'audio-capture': '没有找到麦克风设备。请检查麦克风是否插好、是否被系统禁用。',
    'network': '语音服务连不上。浏览器内置识别需要联网，网络正常仍失败时换成 Edge 打开通常可以解决。',
    'no-speech': '没有听到声音。请靠近麦克风再试，说完停顿一秒即可。',
    'language-not-supported': '当前识别语言不被支持，已按中文尝试。',
  };

  let rec = null;
  let st = 'idle';        // idle | starting | listening | stopping
  let origText = '';      // 开始识别前的原文，取消时用来还原
  let origCaret = 0;
  let base = '';          // 已确认的文字
  let caret = 0;          // 插入位置
  let interim = '';       // 临时结果
  let canceled = false;
  let startedAt = 0;
  let tick = 0;
  let guardT = 0;
  let slowT = 0;

  const HINT = '说完停顿一秒自动结束 · Esc 取消';

  const el = id => document.getElementById(id);
  const taEl = () => el('q');

  function toast(m) {
    if (window.H && window.H.toast) window.H.toast(m);
    else console.warn('[voice]', m);
  }

  function lang() {
    // 语音语言切换下拉框已移除，固定中文识别
    return 'zh-CN';
  }

  function emit(ta) {
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /* 把「已确认文字 + 临时结果」整体写回输入框，光标停在临时结果末尾 */
  function paint(pre) {
    const ta = taEl();
    if (!ta) return;
    ta.value = base.slice(0, caret) + (pre || '') + base.slice(caret);
    const p = caret + (pre || '').length;
    try { ta.setSelectionRange(p, p); } catch (_) { /* ignore */ }
    emit(ta);
  }

  function commit(text) {
    if (!text) return;
    base = base.slice(0, caret) + text + base.slice(caret);
    caret += text.length;
    interim = '';
    paint('');
  }

  function setStatus(t) {
    const n = el('lbStatus');
    if (n) n.textContent = t;
  }

  function setHint(t) {
    const n = el('lbEsc');
    if (n) n.textContent = t;
  }

  /* 卡在「启动中」时把波形停住、配色转成琥珀：还在动的波形会让人以为在录音 */
  function setWait(on) {
    const n = el('listenBar');
    if (n) n.classList.toggle('wait', !!on);
  }

  function ui(on) {
    const btn = el('mic');
    const bar = el('listenBar');
    if (btn) {
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.title = on ? '正在聆听…（点击结束）' : '语音输入（点击开始说话）';
    }
    if (bar) bar.hidden = !on;
    clearInterval(tick);
    tick = 0;
    if (!on) { setWait(false); return; }
    setWait(false);
    startedAt = Date.now();
    tick = setInterval(() => {
      const t = el('lbTime');
      if (t) t.textContent = Math.floor((Date.now() - startedAt) / 1000) + ' 秒';
    }, 250);
  }

  function reset() {
    clearTimeout(guardT);
    clearTimeout(slowT);
    clearInterval(tick);
    tick = 0;
    rec = null;
    st = 'idle';
    interim = '';
    ui(false);
  }

  /* 结束收尾：有临时结果就先落地，再决定是保留还是还原 */
  function finish() {
    clearTimeout(guardT);
    clearTimeout(slowT);
    rec = null;
    st = 'idle';
    ui(false);
    const ta = taEl();
    if (!ta) return;
    if (canceled) {
      ta.value = origText;
      try { ta.setSelectionRange(origCaret, origCaret); } catch (_) { /* ignore */ }
      emit(ta);
      interim = '';
      return;
    }
    if (interim) commit(interim);
    else paint('');
    const view = el('view-ask');
    if (view && !view.hidden) ta.focus();
  }

  function start() {
    if (!Ctor) { toast(UNSUPPORTED); return; }
    if (!SECURE) { toast('语音输入需要在 http://127.0.0.1 或 https 下打开，直接双击本地文件不支持。'); return; }
    if (st !== 'idle') { stop(true); return; }

    const ta = taEl();
    if (!ta) return;

    origText = ta.value;
    origCaret = typeof ta.selectionStart === 'number' ? ta.selectionStart : ta.value.length;
    base = origText;
    caret = origCaret;
    interim = '';
    canceled = false;

    let r;
    try { r = new Ctor(); } catch (_) { toast(UNSUPPORTED); return; }

    r.lang = lang();
    r.continuous = false;
    r.interimResults = true;
    r.maxAlternatives = 1;

    r.onstart = () => {
      if (rec !== r) return;
      clearTimeout(slowT);
      setWait(false);
      st = 'listening';
      setStatus('正在聆听，说完稍等一秒');
      setHint(HINT);
    };

    r.onresult = ev => {
      if (rec !== r) return;
      let fin = '';
      let itr = '';
      for (let i = ev.resultIndex || 0; i < ev.results.length; i++) {
        const res = ev.results[i];
        const txt = (res[0] && res[0].transcript) || '';
        if (res.isFinal) fin += txt;
        else itr += txt;
      }
      if (fin) commit(fin);
      interim = itr;
      paint(interim);
    };

    r.onerror = ev => {
      const code = ev && ev.error;
      /* 出错就把半截结果丢掉，别把听错的词留在框里 */
      canceled = true;
      if (code && ERR[code]) toast(ERR[code]);
      else if (code !== 'aborted') toast('语音识别失败（' + (code || '未知错误') + '）。');
    };

    /* 绑定到具体的识别器实例：silence() 已经换掉 rec 时，迟到的 onend 不能再回写输入框 */
    r.onend = () => { if (rec !== r) return; finish(); };

    rec = r;
    st = 'starting';
    /* 先给界面反馈，不等 onstart —— 有些浏览器申请权限时 onstart 会晚几百毫秒 */
    ui(true);
    setStatus('正在启动麦克风…');
    setHint(HINT);

    /* 焦点留在输入框：字是往这里长的，而且用户说完习惯直接回车发送。
       焦点若留在麦克风按钮上，回车会变成「再点一次麦克风」，永远发不出去。 */
    try { ta.focus(); ta.setSelectionRange(caret, caret); } catch (_) { /* ignore */ }

    /* 授权弹窗没人理会时，识别器既不 onstart 也不 onerror，界面会一直停在
       「正在启动麦克风…」。这里分两级：7 秒换成说明，30 秒兜底收尾。 */
    clearTimeout(slowT);
    slowT = setTimeout(() => {
      if (st !== 'starting') return;
      setWait(true);
      setStatus('没有收到麦克风响应');
      setHint('请在地址栏允许麦克风，或点「完成」退出');
    }, 7000);

    clearTimeout(guardT);
    guardT = setTimeout(() => {
      if (st === 'idle') return;
      canceled = true;
      try { r.abort(); } catch (_) { /* ignore */ }
      finish();
      toast('麦克风没有响应。请检查浏览器地址栏的麦克风权限，或改用 Edge 重试。');
    }, 30000);

    try {
      r.start();
    } catch (_) {
      reset();
      ta.value = origText;
      toast('语音输入启动失败，请稍后再试。');
    }
  }

  function stop(commitIt) {
    if (st === 'idle') return;
    canceled = !commitIt;
    const r = rec;
    if (!r) { finish(); return; }
    st = 'stopping';
    setStatus(commitIt ? '正在整理文字…' : '已取消');
    try {
      if (commitIt) r.stop();
      else r.abort();
    } catch (_) {
      finish();
    }
  }

  function cancel() { stop(false); }

  /* 提交问题 / 切走视图时用：立刻停止，且**不动输入框**。
     走 stop() 会让 onend 把 base 写回输入框，而调用方刚刚清空了它。 */
  function silence() {
    if (st === 'idle') return;
    canceled = true;
    const r = rec;
    rec = null;
    clearTimeout(guardT);
    clearInterval(tick);
    tick = 0;
    st = 'idle';
    interim = '';
    ui(false);
    try { if (r) r.abort(); } catch (_) { /* ignore */ }
  }

  function toggle() {
    if (st === 'idle') start();
    else stop(true);
  }

  function attach() {
    const btn = el('mic');
    const bar = el('listenBar');

    if (!Ctor || !SECURE) {
      if (btn) {
        const why = SECURE ? UNSUPPORTED : '语音输入需在 http://127.0.0.1 或 https 下使用';
        btn.classList.add('off');
        btn.title = why;
        /* 不用 aria-disabled：按钮确实能点，点了要说明原因；
           标成 disabled 会让读屏软件跳过它，等于把提示也藏起来了 */
        btn.setAttribute('aria-label', '语音输入不可用。' + why);
      }
      if (btn) btn.addEventListener('click', () => { toast(btn.title); });
      return;
    }

    if (btn) btn.addEventListener('click', toggle);
    if (bar) {
      const ok = el('lbStop');
      const no = el('lbCancel');
      if (ok) ok.addEventListener('click', () => stop(true));
      if (no) no.addEventListener('click', cancel);
    }

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && st !== 'idle') {
        e.preventDefault();
        cancel();
      }
    });
  }

  window.Voice = {
    attach,
    start,
    stop: () => stop(true),
    cancel,
    silence,
    toggle,
    get supported() { return !!Ctor && SECURE; },
    /* 测试用：暴露内部状态，便于断言状态机 */
    _state: () => ({ st, base, caret, interim, canceled }),
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach);
  else attach();
})();
