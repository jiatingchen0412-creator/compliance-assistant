/*
 * 前端探针：在真实浏览器里打开页面后跑这段脚本，把"可核对的事实"抓成一份 JSON。
 *
 * 由 tests/test_frontend.py 经 tools/cdp_eval.js 注入执行。
 * 这里只取事实、不做判定——阈值和"算不算通过"放在 Python 里，
 * 因为那边的报错信息能写成人话，这里写得再花也只是一堆 JSON。
 *
 * 抓四类东西：
 *   1. 加载期有没有 JS 报错（cdp_eval 注入的 window.__FE_ERRORS）
 *   2. axe-core 的无障碍违规，以及 color-contrast 的"待定项"几何位置
 *   3. 键盘可达性：可滚动区域里到底有没有可聚焦控件
 *   4. 移动端抽屉能不能开合、有没有横向溢出
 */
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const px = (n) => Math.round(n);

  const out = {
    url: location.pathname,
    errors: (window.__FE_ERRORS || []).slice(),
    viewport: { w: window.innerWidth, h: window.innerHeight },
    doc: {
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
    },
    font: {
      status: document.fonts.status,
      interLoaded: document.fonts.check('16px "Inter Variable"', 'Compliance 8.1.4.1'),
      bodyFamily: getComputedStyle(document.body).fontFamily,
    },
    present: {},
    focusable: {},
    headings: [],
    listRoles: [],
    violations: null,
    incompleteContrast: [],
    drawer: null,
  };

  for (const id of ['chatScroll', 'input', 'btnSend', 'sessionList', 'statusChip',
                    'navToggle', 'navScrim', 'treeBox', 'clauseList', 'pager',
                    'statGrid', 'stdSelect', 'fileInput', 'searchBox']) {
    if (document.getElementById(id)) out.present[id] = true;
  }

  // 标题层级：h1 是给屏幕阅读器用的（.sr-only），也算数——它就该在
  for (const h of document.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    out.headings.push({
      level: Number(h.tagName[1]),
      text: h.textContent.trim().slice(0, 30),
      srOnly: h.classList.contains('sr-only'),
    });
  }

  // 可滚动区域里有没有可聚焦的东西（axe 的 scrollable-region-focusable 查的就是这个）
  const focusCount = (sel) => document.querySelectorAll(
    `${sel} button, ${sel} a[href], ${sel} input, ${sel} select, ${sel} [tabindex]:not([tabindex="-1"])`
  ).length;
  out.focusable.sessionList = focusCount('#sessionList');
  out.focusable.treeBox = focusCount('#treeBox');

  const list = document.getElementById('sessionList');
  if (list) {
    out.listRoles = Array.from(list.children).map((c) => c.getAttribute('role'));
  }

  // 移动端抽屉：按钮可见 → 点开 → 侧栏真的进了视口 → 点遮罩 → 收回
  const toggle = document.getElementById('navToggle');
  const scrim = document.getElementById('navScrim');
  const drawerPanel = () => document.getElementById('sidebar') || document.querySelector('.kb-tree');
  if (toggle && getComputedStyle(toggle).display !== 'none') {
    const panel = drawerPanel();
    out.drawer = { buttonVisible: true, panelFound: !!panel };
    toggle.click();
    await sleep(450);
    const r = panel ? panel.getBoundingClientRect() : null;
    out.drawer.openClass = document.body.classList.contains('nav-open');
    out.drawer.left = r ? px(r.left) : null;
    out.drawer.ariaExpanded = toggle.getAttribute('aria-expanded');
    if (scrim) {
      scrim.click();
      await sleep(450);
      out.drawer.closedAfterScrim = !document.body.classList.contains('nav-open');
    }
  } else {
    out.drawer = { buttonVisible: false };
  }

  if (window.axe) {
    const res = await window.axe.run(document, {
      runOnly: {
        type: 'tag',
        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'],
      },
    });
    out.violations = res.violations.map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.map((n) => n.target.join(' ')),
    }));
    // color-contrast 出现在 incomplete 里，多半是因为元素滚出了可视区，
    // axe 拿不到它背后的背景色。那是几何裁剪，不是真的对比度不行。
    // 所以只把事实记下来：「这个待定项在不在视口内」，由测试判断。
    for (const v of res.incomplete) {
      if (v.id !== 'color-contrast') continue;
      for (const n of v.nodes) {
        const sel = n.target[n.target.length - 1];
        let el = null;
        try { el = document.querySelector(sel); } catch (e) { el = null; }
        if (!el) {
          out.incompleteContrast.push({ sel, found: false });
          continue;
        }
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        out.incompleteContrast.push({
          sel,
          w: px(r.width),
          h: px(r.height),
          cy: px(cy),
          inViewport: r.width > 0 && r.height > 0
            && cx >= 0 && cx <= window.innerWidth
            && cy >= 0 && cy <= window.innerHeight,
        });
      }
    }
  }

  return out;
})()
