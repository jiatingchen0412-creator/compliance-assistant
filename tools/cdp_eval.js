#!/usr/bin/env node
/**
 * 用 Chrome DevTools Protocol 驱动一个真实浏览器：打开页面、在页面里跑一段脚本、
 * 把返回值写成 JSON 文件。
 *
 * 为什么需要它：
 *   前面查"前端有没有坏"，靠的是 `node --check`（只查语法）和 `--screenshot` 截图人眼看。
 *   这两种办法都漏过真问题——`dengbao_2.0: '等保2.0'` 这种非法 JS 变量名语法检查查不出来，
 *   而那一次整个知识库页面在浏览器里是直接失效的，我们却以为"HTTP 200 就是好的"。
 *   这个脚本让"页面真的跑起来了吗"变成可断言的。
 *
 * 为什么自己撸 CDP 而不用 puppeteer/playwright：
 *   本项目要求零运行时依赖、离线可用。Node 24 自带全局 WebSocket 和 fetch，
 *   够写一个几十行的 CDP 客户端；引一整套浏览器自动化框架不值当。
 *
 * 用法（一般由 tests/test_frontend.py 调用，不手敲）：
 *   node tools/cdp_eval.js --url http://127.0.0.1:8765/ \
 *        --script <要在页面里执行的 js 文件> \
 *        --axe tests/vendor/axe.min.js \
 *        --mobile --width 390 --height 844 \
 *        --settle 2500 \
 *        --out result.json
 *
 * 结果永远写进 --out 指定的文件，不走 stdout——这样调用方不需要开管道，
 * 在受限的执行环境里也不会因为管道而失败。
 */
'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// 注入到每个新文档最前面的错误收集器。
// 必须在页面自己的脚本之前跑，否则加载期的报错就漏了。
const ERROR_COLLECTOR = `
window.__FE_ERRORS = [];
window.addEventListener('error', function (e) {
  window.__FE_ERRORS.push('error: ' + (e.message || e.type || 'unknown'));
}, true);
window.addEventListener('unhandledrejection', function (e) {
  var r = e.reason;
  window.__FE_ERRORS.push('rejection: ' + ((r && r.message) || String(r)));
});
(function () {
  var orig = console.error;
  console.error = function () {
    try {
      window.__FE_ERRORS.push('console.error: ' + Array.prototype.map.call(arguments, String).join(' '));
    } catch (_) {}
    return orig.apply(console, arguments);
  };
})();
`;

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) continue;
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith('--')) {
      out[key] = next;
      i++;
    } else {
      out[key] = true;
    }
  }
  return out;
}

function findChrome(explicit) {
  const candidates = [
    explicit,
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    process.env.LOCALAPPDATA
      ? path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe')
      : null,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  for (const c of candidates) {
    try {
      if (fs.existsSync(c)) return c;
    } catch (_) {}
  }
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 极简 CDP 客户端：一个 WebSocket、一张 id→resolve 的表、一组事件监听。 */
class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener('open', () => resolve());
      this.ws.addEventListener('error', (e) => reject(new Error('WebSocket 连接失败：' + (e.message || e.type))));
    });
    this.ws.addEventListener('message', (ev) => {
      let msg;
      try {
        msg = JSON.parse(typeof ev.data === 'string' ? ev.data : ev.data.toString());
      } catch (_) {
        return;
      }
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else resolve(msg.result);
        return;
      }
      if (msg.method) {
        const list = this.handlers.get(msg.method) || [];
        for (const fn of list) {
          try {
            fn(msg.params);
          } catch (_) {}
        }
      }
    });
  }

  send(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error('CDP 调用超时：' + method));
        }
      }, 60000);
    });
  }

  once(method) {
    return new Promise((resolve) => {
      const list = this.handlers.get(method) || [];
      const fn = (params) => {
        const idx = list.indexOf(fn);
        if (idx >= 0) list.splice(idx, 1);
        resolve(params);
      };
      list.push(fn);
      this.handlers.set(method, list);
    });
  }

  close() {
    try {
      this.ws.close();
    } catch (_) {}
  }
}

async function waitForFile(file, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const text = fs.readFileSync(file, 'utf8').trim();
      if (text) return text.split(/\r?\n/)[0].trim();
    } catch (_) {}
    await sleep(120);
  }
  throw new Error('等待 ' + file + ' 超时（' + timeoutMs + 'ms），Chrome 可能没起来');
}

async function removeDirWithRetry(dir) {
  for (let i = 0; i < 8; i++) {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
      return true;
    } catch (_) {
      // Windows 上刚杀掉的 Chrome 还会占着 profile 目录里的文件，等一会儿再删
      await sleep(250);
    }
  }
  return false;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const outFile = args.out;
  if (!outFile) {
    process.stderr.write('缺少 --out\n');
    return 2;
  }

  const result = { ok: false, url: args.url || null, error: null, value: null };
  const write = () => fs.writeFileSync(outFile, JSON.stringify(result, null, 2), 'utf8');

  const chromePath = findChrome(args.chrome);
  if (!chromePath) {
    result.error = '找不到 Chrome，可用 --chrome <路径> 或设置环境变量 CHROME_PATH';
    write();
    return 2;
  }

  const width = parseInt(args.width || '1440', 10);
  const height = parseInt(args.height || '1000', 10);
  const settle = parseInt(args.settle || '2500', 10);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-cdp-'));

  let child = null;
  let cdp = null;
  try {
    child = spawn(
      chromePath,
      [
        '--headless=new',
        '--disable-gpu',
        '--no-sandbox',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-extensions',
        '--disable-background-networking',
        '--disable-application-cache',
        '--disable-sync',
        '--hide-scrollbars',
        '--remote-debugging-port=0',
        `--user-data-dir=${profile}`,
        `--window-size=${width},${height}`,
        'about:blank',
      ],
      // stdio:'ignore' —— 受限执行环境里拿管道读子进程输出会失败，这里也不需要
      { stdio: 'ignore', windowsHide: true }
    );

    const port = await waitForFile(path.join(profile, 'DevToolsActivePort'), 30000);
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = targets.find((t) => t.type === 'page');
    if (!page || !page.webSocketDebuggerUrl) throw new Error('没有拿到可用的页面调试地址');

    cdp = new CDP(page.webSocketDebuggerUrl);
    await cdp.ready;

    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: ERROR_COLLECTOR });

    // Emulation 设置的视口不受操作系统窗口最小宽度限制。
    // 直接 `--window-size=390` 在 Windows 上会被撑到 500 多像素，媒体查询根本不按 390 生效。
    if (args.mobile) {
      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width, height, deviceScaleFactor: 1, mobile: true,
      });
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
    }

    const loaded = cdp.once('Page.loadEventFired');
    await cdp.send('Page.navigate', { url: args.url });
    await loaded;

    // 页面自己的 fetch（会话列表、统计、目录树）要等一会儿才落地
    await sleep(settle);

    if (args.axe && fs.existsSync(args.axe)) {
      const axeSrc = fs.readFileSync(args.axe, 'utf8');
      const axeRes = await cdp.send('Runtime.evaluate', {
        expression: axeSrc,
        returnByValue: false,
        awaitPromise: false,
      });
      if (axeRes.exceptionDetails) {
        throw new Error('注入 axe 失败：' + (axeRes.exceptionDetails.text || ''));
      }
    }

    let expression = 'true';
    if (args.script && fs.existsSync(args.script)) {
      expression = fs.readFileSync(args.script, 'utf8');
    }

    const evalRes = await cdp.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
      userGesture: true,
    });

    if (evalRes.exceptionDetails) {
      const d = evalRes.exceptionDetails;
      result.error = '页面脚本抛异常：' + (d.exception && d.exception.description ? d.exception.description : d.text);
    } else {
      result.value = evalRes.result ? evalRes.result.value : null;
      result.ok = true;
    }
  } catch (err) {
    result.error = String((err && err.stack) || err);
  } finally {
    if (cdp) cdp.close();
    if (child) {
      try {
        child.kill();
      } catch (_) {}
    }
    await sleep(300);
    await removeDirWithRetry(profile);
    write();
  }

  return result.ok ? 0 : 1;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    try {
      const args = parseArgs(process.argv.slice(2));
      if (args.out) {
        fs.writeFileSync(args.out, JSON.stringify({ ok: false, error: String(err && err.stack || err) }, null, 2), 'utf8');
      }
    } catch (_) {}
    process.exit(1);
  });
