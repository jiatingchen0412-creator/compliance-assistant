/* 统一的接口调用封装 */

const API = {
  async get(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(await this._err(res));
    return res.json();
  },

  async post(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error(await this._err(res));
    return res.json();
  },

  async put(path, body) {
    const res = await fetch(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error(await this._err(res));
    return res.json();
  },

  async del(path) {
    const res = await fetch(path, { method: 'DELETE' });
    if (!res.ok) throw new Error(await this._err(res));
    return res.json();
  },

  async upload(path, formData) {
    const res = await fetch(path, { method: 'POST', body: formData });
    if (!res.ok) throw new Error(await this._err(res));
    return res.json();
  },

  async _err(res) {
    try {
      const data = await res.json();
      return data.detail || data.message || `请求失败（${res.status}）`;
    } catch (e) {
      return `请求失败（${res.status}）`;
    }
  },

  /**
   * 流式提问：后端用 SSE 返回。
   * 注意用 fetch + ReadableStream 手动解析，因为 EventSource 不支持 POST。
   */
  async chatStream(question, sessionId, handlers) {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, session_id: sessionId }),
    });
    if (!res.ok || !res.body) {
      throw new Error('无法连接后端服务，请确认程序仍在运行。');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 以空行分隔事件
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = this._parseSSE(raw);
        if (!ev) continue;
        const fn = handlers[ev.event];
        if (fn) fn(ev.data);
      }
    }
  },

  _parseSSE(raw) {
    let event = 'message';
    const dataLines = [];
    for (const line of raw.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return null;
    try {
      return { event, data: JSON.parse(dataLines.join('\n')) };
    } catch (e) {
      return null;
    }
  },
};

/* ---------- 小工具 ---------- */
function esc(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

/**
 * 线性图标：统一取页面顶部 SVG 雪碧图里的 symbol。
 * 不再用 emoji 当图标（emoji 在不同系统上字体、颜色、粗细都不一样，
 * 混在一起正是"不专业"的来源）。
 */
function ICON(name, cls) {
  return '<svg class="i' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true">'
    + '<use href="#i-' + name + '"/></svg>';
}

function toast(msg, ms = 2600) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), ms);
}

function downloadText(filename, content) {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/* ---------- 页面外壳交互（对话页和知识库页共用这一份） ---------- */

/**
 * 窄屏（≤860px）时侧栏收成抽屉，由顶栏的汉堡按钮控制。
 * 放在公共文件里而不是各页各写一遍，是为了保证两个页面的开合行为完全一致。
 */
(function initShell() {
  const toggle = document.getElementById('navToggle');
  if (!toggle) return;
  const scrim = document.getElementById('navScrim');

  function setOpen(open) {
    document.body.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function isOpen() {
    return document.body.classList.contains('nav-open');
  }

  toggle.addEventListener('click', () => setOpen(!isOpen()));

  if (scrim) scrim.addEventListener('click', () => setOpen(false));

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen()) {
      setOpen(false);
      toggle.focus();   // 焦点还回按钮，键盘用户不会"迷路"
    }
  });

  // 窗口拉宽回桌面尺寸时清掉抽屉状态，
  // 否则 body 上残留的 nav-open 会让遮罩盖住整个页面。
  window.addEventListener('resize', () => {
    if (window.innerWidth > 860 && isOpen()) setOpen(false);
  });
})();

