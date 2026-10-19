/* 屏幕阅读器「代理走查」探针：把页面的无障碍结构读成一份确定性 JSON。
 *
 * 为什么要有这个文件：
 *   `tests/vendor/axe.min.js` 只能判「规则违规」——缺名字、对比度、ARIA 用法错。
 *   它判不了更常见的那一类问题：「读屏时听到的东西对不对」。比如：
 *     · 阅读顺序和视觉顺序是不是一回事（CSS 换序、position 定位会让两者分家）；
 *     · 可聚焦控件的名字是不是只剩一个「按钮」；
 *     · 标题层级在 AX 树里还成不成立；
 *     · live region 会不会在流式输出时把同一段话念几十遍。
 *   这些只能靠「把 AX 结构取出来自己比对」，所以有这份脚本。
 *
 * 它是什么、不是什么（重要，别在文档里写成别的）：
 *   · 它是**代理检查**：不调用 Chrome 的 Accessibility.getFullAXTree（页面上下文里
 *     拿不到 CDP 域），而是按 ARIA / HTML-AAM 的规则在 DOM 上重算一份「等价结构」：
 *     角色取显式 role 或标签的隐含角色，名字按 aria-labelledby → aria-label → alt →
 *     <label> → 可见文本的顺序算，被忽略的节点按 display/visibility/aria-hidden 判。
 *   · 它**不是**真人屏幕阅读器走查。NVDA / VoiceOver 的实际朗读停顿、标点处理、
 *     浏览模式与焦点模式的切换，只有人戴着耳机听才知道。手测清单见
 *     `docs/09-无障碍走查.md`。
 *
 * 怎么跑：
 *   node tools/cdp_eval.js --url http://127.0.0.1:PORT/ \
 *        --script tests/a11y_walkthrough.js --settle 2500 --out a11y.json
 *   也可以由 `tests/test_frontend.py` 驱动（它就是那么用的）。
 *
 * 输出：一个对象（`--out` 文件里的 value），字段见文件末尾的 return。
 * 确定性：不读时间、不读随机数、不依赖网络；数组一律按 DOM / 坐标排序。
 *   流式那一段用的是注入的假 SSE（不碰后端），所以离线也能跑。
 */
(async () => {
  const out = { page: location.pathname, ok: true, problems: [] };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* ---------------- 基础：可见性 / 角色 / 名字 ---------------- */
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5;
  };

  const textOf = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '');

  // 隐含角色：只覆盖这个项目真正会用到的标签（不是完整 AAM 实现）
  const implicitRole = (el) => {
    const tag = el.tagName;
    if (tag === 'A') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'file') return 'button';
      if (t === 'submit' || t === 'button') return 'button';
      if (t === 'search') return 'searchbox';
      return 'textbox';
    }
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'SELECT') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'H1' || tag === 'H2' || tag === 'H3' || tag === 'H4' || tag === 'H5' || tag === 'H6') {
      return 'heading';
    }
    if (tag === 'MAIN') return 'main';
    if (tag === 'NAV') return 'navigation';
    if (tag === 'ASIDE') return 'complementary';
    if (tag === 'HEADER') return 'banner';
    if (tag === 'FOOTER') return 'contentinfo';
    if (tag === 'UL' || tag === 'OL') return 'list';
    if (tag === 'LI') return 'listitem';
    if (tag === 'TABLE') return 'table';
    if (tag === 'SUMMARY') return 'button';
    if (tag === 'svg') return 'graphics-document';
    return 'generic';
  };

  const roleOf = (el) => (el.getAttribute('role') || '').trim() || implicitRole(el);

  // 可访问名称：按浏览器/AAM 的优先级取，只取到能判定「空 / 非空」为止
  const nameOf = (el) => {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const s = lb.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => n.getAttribute('aria-label') || textOf(n))
        .join(' ')
        .replace(/\s+/g, ' ')
        .trim();
      if (s) return { name: s, from: 'aria-labelledby' };
    }
    const al = (el.getAttribute('aria-label') || '').trim();
    if (al) return { name: al, from: 'aria-label' };
    if (el.tagName === 'IMG') {
      const alt = (el.getAttribute('alt') || '').trim();
      if (alt) return { name: alt, from: 'alt' };
    }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      const label = el.id
        ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
        : null;
      if (label) {
        const s = textOf(label);
        if (s) return { name: s, from: 'label' };
      }
      const wrap = el.closest('label');
      if (wrap) {
        const s = textOf(wrap);
        if (s) return { name: s, from: 'label-wrap' };
      }
      // placeholder **不算**可访问名称（axe 的 label 规则也不认），这里显式标出来
      if (el.getAttribute('placeholder')) return { name: '', from: 'placeholder-only' };
      return { name: '', from: 'none' };
    }
    const t = textOf(el);
    if (t) return { name: t, from: 'contents' };
    const title = (el.getAttribute('title') || '').trim();
    if (title) return { name: title, from: 'title' };
    return { name: '', from: 'none' };
  };

  const sel = (el) => {
    const id = el.id ? `#${el.id}` : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.')
      : '';
    return el.tagName.toLowerCase() + id + cls;
  };

  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { top: Math.round(r.top), left: Math.round(r.left), w: Math.round(r.width), h: Math.round(r.height) };
  };

  /* ---------------- 1. 可聚焦控件的名字完整性 ---------------- */
  // tabIndex >= 0 且可见、未禁用 = 键盘/读屏真的会停在这儿
  const focusables = Array.from(document.querySelectorAll('a[href],button,input,select,textarea,summary,[tabindex]'))
    .filter((el) => el.tabIndex >= 0 && visible(el) && !el.disabled)
    .map((el) => {
      const nm = nameOf(el);
      return { sel: sel(el), role: roleOf(el), name: nm.name, from: nm.from, ...rect(el) };
    });
  out.focusables = focusables;
  out.emptyNames = focusables.filter((f) => !f.name).map((f) => `${f.sel}（${f.role}，name 来自 ${f.from}）`);

  /* ---------------- 2. 阅读顺序：DOM 序 vs 视觉序 ---------------- */
  // 取地标 + 标题 + 可交互元素，按 DOM 顺序列出来，再按 (top,left) 排一遍比对。
  // 只看「同一视觉行内左右颠倒」和「上下颠倒」两类，避免 sticky 顶栏这种正常例外。
  const landmarks = Array.from(document.querySelectorAll('main,nav,aside,header,footer,[role]'))
    .filter((el) => {
      const r = roleOf(el);
      return ['main', 'navigation', 'complementary', 'banner', 'contentinfo', 'search', 'log', 'status', 'alert', 'alertdialog', 'list'].includes(r) && visible(el);
    })
    .map((el) => ({ sel: sel(el), role: roleOf(el), name: nameOf(el).name, ...rect(el) }));
  out.landmarks = landmarks;

  const orderNodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .filter(visible)
    .map((el) => ({
      sel: sel(el), role: 'heading', level: Number(el.tagName.slice(1)),
      name: textOf(el).slice(0, 40), ...rect(el),
    }));
  out.readingOrder = orderNodes;

  // 视觉顺序：先 top（同一行按 left），DOM 顺序已在数组里
  const byVisual = orderNodes.slice().sort((a, b) => (a.top - b.top) || (a.left - b.left) || (a.sel < b.sel ? -1 : 1));
  out.orderMismatch = [];
  orderNodes.forEach((node, i) => {
    if (byVisual[i].sel !== node.sel) {
      out.orderMismatch.push(`${i}: DOM 里是 ${node.sel}（top=${node.top},left=${node.left}），`
        + `按坐标应排 ${byVisual[i].sel}（top=${byVisual[i].top},left=${byVisual[i].left}）`);
    }
  });

  /* ---------------- 3. 标题结构（AX 视角：只算没被隐藏的） ---------------- */
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .filter(visible)
    .map((el) => ({ level: Number(el.tagName.slice(1)), text: textOf(el).slice(0, 40) }));
  out.headings = headings;
  const h1n = headings.filter((h) => h.level === 1).length;
  if (h1n !== 1) out.problems.push(`可见标题里有 ${h1n} 个 h1，应正好 1 个`);
  for (let i = 1; i < headings.length; i += 1) {
    if (headings[i].level > headings[i - 1].level + 1) {
      out.problems.push(`标题从 h${headings[i - 1].level} 跳到 h${headings[i].level}：「${headings[i].text}」`);
    }
  }

  /* ---------------- 4. live region 配置 ---------------- */
  const liveNodes = Array.from(document.querySelectorAll('[aria-live],[role="status"],[role="alert"],[role="log"],[role="alertdialog"]'));
  out.live = liveNodes.map((el) => {
    const role = el.getAttribute('role') || '';
    // 没有显式 aria-live 时，role 自带隐含的 polite/assertive
    const implied = { status: 'polite', log: 'polite', alert: 'assertive', alertdialog: 'assertive' }[role] || '';
    return {
      sel: sel(el),
      id: el.id || '',
      role,
      ariaLive: el.getAttribute('aria-live'),
      effective: el.getAttribute('aria-live') || implied || 'off',
      atomic: el.getAttribute('aria-atomic'),
      busy: el.getAttribute('aria-busy'),
      visible: visible(el),
      textLen: textOf(el).length,
    };
  });

  /* ---------------- 5. 流式回答：整段重绘对读屏意味着什么 ---------------- */
  // 注入假 SSE：12 个 delta，一次 meta。全程不碰后端，离线可跑。
  // 同时用 MutationObserver 数 #chatScroll 里的子节点替换次数和文本增量，
  // 这两个数与「读屏会被打断/重播多少次」直接相关。
  const scroll = document.getElementById('chatScroll');
  const srun = { available: !!scroll, deltas: 12, snapshots: {} };
  out.streaming = srun;
  if (!scroll) {
    out.problems.push('页面上没有 #chatScroll，无法测流式播报');
    return out;
  }

  const snap = (tag) => ({
    tag,
    live: scroll.getAttribute('aria-live'),
    busy: scroll.getAttribute('aria-busy'),
    role: scroll.getAttribute('role'),
    answerLive: (scroll.querySelector('.answer-text') || { getAttribute: () => null }).getAttribute('aria-live'),
  });

  let mutations = 0;
  let textGrowth = 0;
  const obs = new MutationObserver((records) => {
    records.forEach((r) => {
      mutations += 1;
      if (r.type === 'childList') {
        // 每次 innerHTML 整体替换都会把旧节点摘掉、新节点插进来
        textGrowth += textOf(scroll).length;
      }
    });
  });

  // 注意：api.js 里的 API 是顶层 const，只存在于脚本作用域，不是 window 的属性，
  // 所以只能写裸 API / typeof 判断；`window.API` 永远是 undefined。
  const canStream = typeof API !== 'undefined' && API && typeof API.chatStream === 'function';
  const realChatStream = canStream ? API.chatStream : null;
  if (!realChatStream) {
    out.problems.push('页面上没有 API.chatStream，无法测流式播报');
    return out;
  }

  try {
    API.chatStream = async (question, sessionId, handlers) => {
      handlers.status({ text: '正在检索知识库条款…' });
      await sleep(20);
      handlers.session({ session_id: 'walkthrough-session' });
      obs.observe(scroll, { childList: true, subtree: true, characterData: true });
      for (let i = 0; i < srun.deltas; i += 1) {
        handlers.delta({ text: `第 ${i + 1} 段回答正文。` });
        if (i === 0) srun.snapshots.midStream = snap('mid');
        await sleep(8);
      }
      handlers.meta({
        found: true, mode: 'llm', risks: [], citations: [],
        followup_questions: [], notes: [], elapsed: 1.2,
      });
    };
    srun.snapshots.before = snap('before');
    await send('无障碍走查用的假问题');
    await sleep(80);
  } catch (e) {
    srun.error = String(e && e.message ? e.message : e);
  } finally {
    obs.disconnect();
    API.chatStream = realChatStream;
    srun.snapshots.after = snap('after');
  }
  srun.mutations = mutations;
  srun.announcedChars = textGrowth;

  const ann = document.getElementById('srAnnounce');
  srun.announcer = ann ? { exists: true, role: ann.getAttribute('role'), live: ann.getAttribute('aria-live'), atomic: ann.getAttribute('aria-atomic'), text: textOf(ann) } : { exists: false };

  /* ---------------- 6. 汇总 ---------------- */
  if (out.emptyNames.length) {
    out.problems.push(`${out.emptyNames.length} 个可聚焦控件没有可访问名称`);
  }
  if (srun.snapshots.midStream && srun.snapshots.midStream.live !== 'off') {
    out.problems.push(`流式输出期间 #chatScroll 的 aria-live 是 ${srun.snapshots.midStream.live}，`
      + '读屏会把每个 delta 当新内容播报（应该是 off）');
  }
  if (srun.snapshots.midStream && srun.snapshots.midStream.busy !== 'true') {
    out.problems.push('#chatScroll 流式期间没有 aria-busy="true"，读屏不知道内容还在变');
  }
  if (!ann || !textOf(ann)) {
    out.problems.push('回答生成完成后没有任何「完成播报」文本（缺 #srAnnounce 或内容为空）');
  }
  out.ok = out.problems.length === 0;
  return out;
})()
