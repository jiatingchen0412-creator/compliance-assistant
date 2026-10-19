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
   *
   * 必须带超时和 AbortController：本地模型偶尔会卡在"正在检索"上不动，
   * 没有兜底的话请求永远不返回，发送键一直 disabled，用户只能刷新页面。
   * 空闲超时按"多久没收到新字节"算（正常生成时一直在吐 delta），
   * 总超时是最后一道保险。
   */
  async chatStream(question, sessionId, handlers) {
    const IDLE_MS = 90000;          // 90 秒没有收到任何数据就认为连接死了
    const TOTAL_MS = 15 * 60 * 1000; // 单次提问的硬上限
    const controller = new AbortController();
    let idleTimer = null;
    let totalTimer = null;
    let timedOut = false;
    const armIdle = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => { timedOut = true; controller.abort(); }, IDLE_MS);
    };
    armIdle();
    totalTimer = setTimeout(() => { timedOut = true; controller.abort(); }, TOTAL_MS);

    try {
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: sessionId }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        // 以前这里一律报"无法连接后端服务"，会把后端 400/500 的具体原因吞掉，
        // 用户按提示去重启程序，其实是参数或服务端出错。
        throw new Error(await this._err(res));
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        armIdle();
        buffer += decoder.decode(value, { stream: true });
        // SSE 允许 \r\n 换行；有中间件会把 \n 改写成 \r\n，只认 \n\n 会整条流解析不出来
        buffer = buffer.replace(/\r\n/g, '\n');

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
    } catch (e) {
      if (timedOut && (e.name === 'AbortError' || controller.signal.aborted)) {
        throw new Error('等待回答超时，请重试。');
      }
      throw e;
    } finally {
      clearTimeout(idleTimer);
      clearTimeout(totalTimer);
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
/**
 * HTML 转义。
 *
 * 为什么不用「textContent 再读 innerHTML」了：那个做法只处理 & < >，
 * 引号会原样返回。而 esc() 也会被用在属性位置（href="${esc(url)}"），
 * 只要数据里有一个 " 就能逃出属性、插进 onmouseover 之类的事件处理器——
 * 知识库的 source_url 是随导入 JSON 一起进来的，重开会话还会再渲染一次，
 * 属于存储型 XSS。所以引号必须一起转义。
 */
function esc(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * 只放行 http / https 的链接地址，其余（javascript:、data:、vbscript:、
 * 带控制字符的畸形值）一律返回空串，由调用方降级成纯文本。
 * 属性转义是第一道闸，这里是第二道：即使将来又有人把 esc() 用错地方，
 * 也不会因为一个协议前缀就生成可点击的危险链接。
 */
function safeUrl(url) {
  const raw = String(url == null ? '' : url).trim();
  if (!raw) return '';
  // URL 解析器会忽略 \t \n \0 之类控制字符（"java\tscript:" 的经典绕过），先整体拒掉
  if (/[\u0000-\u001f\u007f]/.test(raw)) return '';
  let parsed;
  try {
    parsed = new URL(raw, window.location.origin);
  } catch (e) {
    return '';
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
  return parsed.href;
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
  const main = document.querySelector('main');

  /** 抽屉面板：优先用 aria-controls 指的那个元素，两个页面都能对上 */
  function panelEl() {
    const id = toggle.getAttribute('aria-controls');
    return (id && document.getElementById(id)) || document.querySelector('.sidebar, .kb-tree');
  }

  /** 面板里第一个键盘可达的控件 */
  function firstFocusable(root) {
    if (!root) return null;
    const nodes = root.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),'
      + ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
    for (const el of nodes) {
      if (el.getClientRects().length) return el;
    }
    return null;
  }

  function setOpen(open, returnFocus = true) {
    const wasOpen = isOpen();
    document.body.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');

    // 抽屉盖住的是正文：打开时正文必须 inert，否则 Tab 仍能走进被遮住的
    // 输入框和按钮里（实测首页有 35 个、知识库页 13 个可聚焦元素），
    // 屏幕阅读器也会照常读到后面那一整页内容。
    if (main) main.inert = open;

    if (open) {
      // 焦点跟着抽屉走，键盘用户按 Tab 才是在抽屉里循环，而不是跳到看不见的地方
      const panel = panelEl();
      const first = firstFocusable(panel);
      if (first) {
        first.focus();
      } else if (panel) {
        panel.setAttribute('tabindex', '-1');
        panel.focus();
      }
    } else if (wasOpen && returnFocus) {
      toggle.focus();   // 焦点还回按钮，键盘用户不会"迷路"
    }
  }

  function isOpen() {
    return document.body.classList.contains('nav-open');
  }

  toggle.addEventListener('click', () => setOpen(!isOpen()));

  if (scrim) scrim.addEventListener('click', () => setOpen(false));

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen()) setOpen(false);
  });

  // 窗口拉宽回桌面尺寸时清掉抽屉状态，
  // 否则 body 上残留的 nav-open 会让遮罩盖住整个页面。
  // 这种情况下侧栏又变回常驻的，焦点不该被抢回汉堡按钮。
  window.addEventListener('resize', () => {
    if (window.innerWidth > 860 && isOpen()) setOpen(false, false);
  });
})();

