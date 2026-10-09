# 在这个仓库里工作

给 AI 编码助手（Claude Code / Cursor / Copilot 等）的项目说明。人看也可以。

---

## 这是什么

一个**完全离线**运行的安全合规自查工具：用户用大白话描述自己的系统，程序在本地知识库
（等保 2.0 / NIST CSF 2.0 / OWASP Top 10，283 条）里检索对应条款，由本地大模型
（Ollama + Qwen2.5-7B）生成回答，并对高风险项加 ⚠️ 标注。全部数据留在本机，不联网。

技术栈刻意保守：**Python + FastAPI + SQLite + 原生 HTML/CSS/JS**。
没有前端框架、没有构建步骤、没有 CDN——用户双击 `启动助手.bat` 就能跑。

---

## 先读什么

按这个顺序读，不要跳。跳过去就会重复踩已经踩过的坑。

| 顺序 | 文件 | 读它是为了知道 |
|---|---|---|
| 1 | `docs/00-项目简报.md` | 全局：做了什么、为什么这么做、踩过哪些坑 |
| 2 | `docs/01-需求规格说明.md` | 需求边界，什么在范围内什么不在 |
| 3 | `docs/05-问答与防幻觉规范.md` | **最容易改坏的地方**：引用白名单、⚠️ 判定、库外法规拦截 |
| 4 | `docs/07-测试与验收规范.md` | 验收标准与指标红线 |
| 5 | `app/quality.py` 的 `RED_LINES` / `EXPECTED_COUNTS` | 红线的代码位置，改红线要两边一起改 |
| 6 | 需要动哪个模块再读 `docs/02` `03` `04` `08` | 按需 |

`README.md` 是给用户看的（安装、使用、故障排查），改动它时注意别把面向开发者的内容塞进去。

---

## 工作循环

本项目验证有效的循环是五步，**不要跳步**：

1. **规格先行** — 动手前先确认需求文档里怎么写的。需求不清就先问，不要猜。
2. **一次只做一步** — 一步 = 一个能独立验证的改动。做完、验完、记完，再开下一步。
3. **先跑测试再宣称完成** — 「应该是好的」不算完成。见下面的命令速查。
4. **红线必须实测** — 改了检索、提示词、防幻觉相关代码，必须跑 `run_eval.py --no-llm`。
5. **留痕** — `tools/devlog.py done "…"` / `decision "…"`，把**为什么**记下来，
   不只是做了什么。半年后没人记得为什么不用 BM25。

### 每步做完的自检清单

- [ ] 全部 9 个套件通过（`tests/check_syntax.py` 一定最先跑）
- [ ] 改了检索/提示词/防幻觉 → `run_eval.py --no-llm` 仍达标
- [ ] 改了前端 → `tests/test_frontend.py` 通过（真浏览器，不是看 HTTP 200）
- [ ] 改了配置项 → `.env.example` 与 `tools/sync_config.py` 同步
- [ ] 改了指标/行为 → 对应 `docs/*.md` 同步更新
- [ ] `devlog/` 记了决策与理由

---

## 三条铁律

### 一、零运行时依赖、离线可用

- 不引前端框架、不引构建工具、不引 CDN（字体、图标、JS 库都不行）。
  图标用内联 SVG 雪碧图（`web/index.html` / `web/kb.html` 顶部的 `<symbol>`）。
- 后端依赖只加在 `requirements.txt` 里，且必须是能离线工作的。
- 新增任何第三方依赖前先问：**断网时这个功能还能用吗？**

### 二、会静默失败的东西必须有自动化检查

这条是血的教训：`web/js/kb.js` 里写过 `dengbao_2.0: '等保2.0'`——
非法 JS 变量名，整个知识库页在浏览器里失效，而 HTTP 状态码是 200，
所以「接口正常」的检查完全没发现。**语法正确 ≠ 页面能用。**

- JS 引用的元素 ID、图标名 → `tests/check_syntax.py` 校验（写错浏览器只会静默画空白）
- 页面能不能用 → `tests/test_frontend.py` 用真 Chrome 兜底
- 中文写进 `.bat` → `check_syntax.py` 的 `check_bat_ascii()` 拦截
  （cmd 按代码页逐字节读批处理，中文会让它完全无法执行，双击毫无反应）

### 三、测试不许依赖外部服务是否在线

Ollama 没开、模型没下、显存不够——这些都不是测试失败的理由。
需要的调用一律打桩（见 `tests/test_edge_cases.py`）。
`tests/test_frontend.py` 也不启动完整的 `app.main`（会加载 fastembed、探测 Ollama），
而是拿 FastAPI 实例用 `uvicorn` 起临时端口，`lifespan="off"`。

---

## 指标红线

改红线之前先改 `docs/07-测试与验收规范.md`。红线在 `app/quality.py`：

| 项 | 值 | 说明 |
|---|---|---|
| `recall_min` | 27 | 该命中的 29 题里至少对 27 题 |
| `reject_required` | 5 | 5 道无关题必须全部拒答（**一票否决**） |
| `risk_min` | 8 | 9 道高风险题至少标对 8 题 |
| `outkb_required` | 6 | 点名库外法规的题必须全部识别（**一票否决**） |
| `undet_required` | 4 | 适用性判定题必须全部拦住（**一票否决**） |
| `forbid_max_false` | 0 | ⚠️ 误标一道都不许有 |

`EXPECTED_COUNTS`（hit 29 / reject 5 / out_of_kb 6 / undeterminable 4 / risk_forbid 2）
卡的是**题库数量本身**——只看通过数的话，题库被删掉一半，红线反而显示"通过"。
加题必须同步改这两个常量。

> 一个结构性教训：**不要用被评测的同一个阈值去定义"正确答案"**。
> v1.1 的质量门就犯过这个错——阈值过了但本不该回答的问题，质量门结构上发现不了。
> 四类判定现在各自独立。

---

## 常用命令

```powershell
# 跑全部测试（9 个套件 / 99 个用例）
.venv\Scripts\python.exe tests\check_syntax.py       # 静态检查，最先跑
.venv\Scripts\python.exe tests\run_eval.py --no-llm  # 检索与判定质量（不需要 Ollama，秒级）
.venv\Scripts\python.exe tests\run_eval.py           # 加上端到端那层（需要 Ollama，分钟级）
.venv\Scripts\python.exe tests\test_scope.py
.venv\Scripts\python.exe tests\test_guard.py
.venv\Scripts\python.exe tests\test_normalize.py
.venv\Scripts\python.exe tests\test_edge_cases.py
.venv\Scripts\python.exe tests\test_revert.py        # 用临时库，不动真实数据
.venv\Scripts\python.exe tests\test_streaming.py
.venv\Scripts\python.exe tests\test_export.py
.venv\Scripts\python.exe tests\test_frontend.py      # 需要 Node + Chrome

# 起服务 / 健康检查
.venv\Scripts\python.exe -m app.main
.venv\Scripts\python.exe tools\health_check.py

# 开发日志
.venv\Scripts\python.exe tools\devlog.py done "…"
.venv\Scripts\python.exe tools\devlog.py decision "…"
```

> **PowerShell 坑**：不要用 `$py = ".venv\Scripts\python.exe"` 再 `& $py`，
> 会报 `无法加载模块".venv"`。用 `Join-Path (Get-Location) ".venv\Scripts\python.exe"`。
> 也不要拿 `Out-File -Encoding UTF8` 写 git commit message（会带 BOM）。

---

## 别做的事

- **别为了"更现代"引框架、构建链或 TypeScript。** 出过的事故（非法变量名）
  换框架一样中招，而零构建是用户能双击 `.bat` 跑起来的前提。
- **别杀用户机器上的其他进程。** 遇到显存被占（`ollama` 加载模型 OOM）时，
  报告即可，不要动别人的程序。
- **别把真实用户数据提交进仓库。** `data/app.db`、`.env`、`data/logs`、`data/models`
  都在 `.gitignore` 里。截图前检查侧栏有没有用户的历史会话标题。
- **别在 `.bat` 里写中文。** 中文提示交给 Python 打印（见 `tools/first_run.py`）。
- **别静默降级。** 大模型不可用时要让用户看见（页面上有降级徽标），
  不能悄悄变成一个只会列条款的工具还不说。
- **别跳过 `check_syntax.py`。** 它是唯一能拦住"静默失败"的静态防线。

---

## 已知的坑（都已修，但容易复发）

| 坑 | 症状 | 修法 |
|---|---|---|
| 系统代理 | 本地 Ollama 请求被送去 `127.0.0.1:7897`，静默降级 | `httpx` 客户端加 `trust_env=False`；测试就绪探针用 `http.client` |
| GBK 控制台 | 输出被重定向时打印 ⚠️/✅ 直接抛 `UnicodeEncodeError` 终结进程 | `import app.console`（`app/__init__.py` 已自动导入） |
| `.bat` 中文 | 双击毫无反应，cmd 报 `'xxx' is not recognized` | `.bat` 只写纯 ASCII |
| 就绪探针猛试 | 服务还没 accept 就连，卡死 50 秒以上 | 先 `sleep(3)` 再探，timeout 放宽到 60 秒 |
| `chrome --screenshot` | `--virtual-time-budget` 不吃网络请求，反复截到空状态 | 用 `tools/cdp_eval.js --screenshot`（真实时间等待） |
| Chrome 视口 | 视口比 `--window-size` 小；Windows 上最小宽度约 500px | 用 `--mobile`（CDP `setDeviceMetricsOverride`），别指望 `--window-size=390` |
| 同一份 UI 写两遍 | HTML 静态一份 + JS 又生成一份，同 id 元素并存 | 只保留一份；JS 用完是"摘下来"而不是删掉 |
| 备份早于记录 | 恢复备份抹掉批次记录，撤销标记失效 | `ingest.restore_batch_record()` |
| `w:eastAsia` | Word 导出中文退化成宋体 | `app/export.py` 的 `_font()` 同时设 `w:eastAsia` |
