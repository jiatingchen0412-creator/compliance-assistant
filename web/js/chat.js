/* 聊天页交互逻辑 */

/* 快捷问题。icon 存的是图标名（对应 HTML 顶部雪碧图里的 #i-xxx），
   不再用 emoji——emoji 在 Windows / macOS / 安卓上长得都不一样，
   颜色和粗细也压不住，混在一起就没有统一的视觉语言。 */
const QUICK_QUESTIONS = [
  { icon: 'users', title: '员工共用同一个管理员账号', text: '我们公司十几个人都用同一个管理员账号登录进销存系统，这样有问题吗？' },
  { icon: 'lock', title: '客户手机号没加密存着', text: '客户的手机号和地址都存在数据库里，没有做加密，需要注意什么？' },
  { icon: 'logout', title: '员工离职权限没回收', text: '员工离职后，他的系统账号和权限一直没停用，合规上有什么要求？' },
  { icon: 'clipboard', title: '系统没有任何操作日志', text: '我们的系统没有记录谁在什么时候做了什么操作，这算不合规吗？' },
  { icon: 'database', title: '数据只有一份没有备份', text: '所有业务数据只存在一台服务器上，从来没有做过备份，有什么风险？' },
  { icon: 'idcard', title: '小程序收集了身份证号', text: '我们的小程序要求用户上传身份证照片，需要满足哪些要求？' },
  { icon: 'bug', title: '服务器很久没打补丁', text: '我们的服务器系统很久没更新过补丁了，会有什么合规问题？' },
  { icon: 'building', title: '系统是外包公司做的', text: '我们的系统是找外包公司开发的，交付之后就没再管过，合规上要注意什么？' },
];

const state = {
  sessionId: null,
  sending: false,
  sessions: [],
};

/* ---------------- 启动检查 ---------------- */
async function pollStartup() {
  const ovMsg = document.getElementById('ovMsg');
  const ovTip = document.getElementById('ovTip');
  const ovBar = document.getElementById('ovBar');

  for (let i = 0; i < 400; i++) {
    let s;
    try {
      s = await API.get('/api/startup');
    } catch (e) {
      ovMsg.textContent = '正在等待后端服务启动…';
      await new Promise((r) => setTimeout(r, 1200));
      continue;
    }

    ovMsg.textContent = s.message || '正在启动…';
    if (s.total > 0) {
      ovBar.classList.remove('indeterminate');
      ovBar.style.width = Math.max(8, Math.round((s.progress / s.total) * 100)) + '%';
    }
    if (s.phase === 'error') {
      ovTip.innerHTML = '启动出错：' + esc(s.error) +
        '<br><br>请查看程序窗口里的错误信息，或重新双击 启动助手.bat。';
      return false;
    }
    if (s.ready) {
      document.getElementById('overlay').classList.add('hidden');
      return true;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
  ovTip.textContent = '启动时间较长，可先尝试刷新页面。';
  return false;
}

/* ---------------- 环境状态 ---------------- */
async function refreshHealth() {
  const chip = document.getElementById('statusChip');
  const hintRight = document.getElementById('hintRight');
  try {
    const h = await API.get('/api/health');
    const llm = h.llm || {};
    if (llm.status === 'ok') {
      chip.textContent = `本地模型已就绪 · ${llm.model}`;
      chip.className = 'status-chip ok';
      chip.title = `Ollama ${llm.base_url} · 模型 ${llm.model}`;
    } else if (llm.status === 'model_missing') {
      chip.textContent = '缺少大模型（当前仅能检索条款）';
      chip.className = 'status-chip warn';
      chip.title = llm.hint || '';
    } else {
      chip.textContent = 'Ollama 未运行（当前仅能检索条款）';
      chip.className = 'status-chip warn';
      chip.title = llm.hint || '';
    }
    const vec = h.vector_search ? '语义检索已启用' : '语义检索未启用（仅关键词）';
    hintRight.textContent = `${vec} · 知识库 ${h.config ? h.config.retrieval_top_k : ''} 条/次`;
    if (llm.hint) console.warn('[环境提示]', llm.hint);
    renderQualityWarning(h.quality);
  } catch (e) {
    chip.textContent = '状态未知';
    chip.className = 'status-chip warn';
  }
}

/* 检索质量自检未通过时，在聊天区顶部挂一条黄色警告 */
function renderQualityWarning(quality) {
  const existing = document.getElementById('qualityWarn');
  const scroll = document.getElementById('chatScroll');
  if (!quality || !quality.checked || quality.passed || quality.skipped) {
    if (existing) existing.remove();
    return;
  }
  if (existing) existing.remove();

  const box = document.createElement('div');
  box.className = 'quality-warn';
  box.id = 'qualityWarn';
  const items = (quality.failures || []).map((f) => `<li>${esc(f)}</li>`).join('');
  box.innerHTML = `
    <span class="qw-icon">${ICON('alert')}</span>
    <div class="qw-body">
      <div class="qw-title">检索质量自检未通过</div>
      <ul class="qw-list">${items}</ul>
      <div class="qw-hint">
        说明检索参数（阈值 / 权重 / 分词词典）被改动后指标下降了。
        回答可能变得不准，建议检查 .env 里的 RETRIEVAL_MIN_SCORE 等配置，
        或运行 tools\\check_quality.py 查看详情。
      </div>
    </div>
    <button id="qualityRecheck">重新自检</button>`;
  scroll.insertBefore(box, scroll.firstChild);
  box.querySelector('#qualityRecheck').onclick = async () => {
    try {
      await API.post('/api/quality/recheck');
      toast('已开始重新自检，几秒后自动刷新');
      setTimeout(refreshHealth, 6000);
    } catch (e) {
      toast('重新自检失败：' + e.message);
    }
  };
}

/* ---------------- 会话列表 ---------------- */
async function loadSessions() {
  const box = document.getElementById('sessionList');
  try {
    const data = await API.get('/api/sessions');
    state.sessions = data.sessions || [];
    box.innerHTML = '';
    if (!state.sessions.length) {
      box.innerHTML = '<div class="list-hint" role="listitem">还没有历史对话</div>';
      return;
    }
    for (const s of state.sessions) {
      // role=list 的容器里只能放 role=listitem，否则屏幕阅读器读不出"这是一份列表"，
      // 之前这里是无角色的 div，属于无障碍硬错误。
      const el = document.createElement('div');
      el.className = 'session-item' + (s.id === state.sessionId ? ' active' : '');
      el.setAttribute('role', 'listitem');

      // 标题和删除都做成真正的 <button>：原来是带 onclick 的 span，
      // 键盘用户根本聚焦不到，也就永远切不了对话（这原本是实打实的可用性缺口）。
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'title';
      open.textContent = s.title;
      open.title = s.title;
      if (s.id === state.sessionId) open.setAttribute('aria-current', 'true');
      open.onclick = () => openSession(s.id);

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'del';
      del.textContent = '×';
      del.title = '删除对话';
      del.setAttribute('aria-label', `删除对话「${s.title}」`);
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`确定删除对话「${s.title}」？`)) return;
        await API.del('/api/sessions/' + s.id);
        if (state.sessionId === s.id) newChat();
        loadSessions();
      };

      el.append(open, del);
      box.appendChild(el);
    }
  } catch (e) {
    box.innerHTML = '<div class="list-hint error" role="listitem">读取失败</div>';
  }
}

function newChat() {
  state.sessionId = null;
  document.getElementById('chatScroll').innerHTML = '';
  renderEmptyState();
  loadSessions();
}

async function openSession(id) {
  try {
    const data = await API.get('/api/sessions/' + id);
    state.sessionId = id;
    const scroll = document.getElementById('chatScroll');
    scroll.innerHTML = '';
    for (const m of data.messages) {
      if (m.role === 'user') addUserMessage(m.content);
      else addAssistantMessage(m.content, m);
    }
    scrollTop();
    loadSessions();
  } catch (e) {
    toast('打开对话失败：' + e.message);
  }
}

/* ---------------- 渲染 ---------------- */

// 空状态那块 DOM 只写在 index.html 里一份。切到历史对话时把它从文档里摘下来
// （removeEmptyState），需要时再原样放回去，这样永远不会出现两个 id="emptyState"
// 同时显示（曾经就是这样：HTML 一份、JS 又生成一份，重复标题加两套快捷卡片）。
let emptyStateEl = null;

function renderEmptyState() {
  const scroll = document.getElementById('chatScroll');
  if (!emptyStateEl) emptyStateEl = document.getElementById('emptyState');
  if (emptyStateEl && !emptyStateEl.isConnected) scroll.appendChild(emptyStateEl);
  if (!emptyStateEl) return;

  const grid = emptyStateEl.querySelector('#quickGrid');
  if (!grid) return;
  grid.innerHTML = '';
  for (const q of QUICK_QUESTIONS) {
    const card = document.createElement('div');
    card.className = 'quick-card';
    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.innerHTML = `<span class="qc-icon">${ICON(q.icon)}</span><strong>${esc(q.title)}</strong>
                      <div class="qc-text">${esc(q.text)}</div>`;
    card.onclick = () => send(q.text);
    // 卡片点了能直接提问，那就得能用键盘点：回车和空格都要管
    card.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        send(q.text);
      }
    };
    grid.appendChild(card);
  }
}

function removeEmptyState() {
  const el = document.getElementById('emptyState');
  // 只是从文档里摘下来，引用还留着，renderEmptyState 随时能放回去
  if (el) el.remove();
}

function scrollTop(force) {
  const scroll = document.getElementById('chatScroll');
  const nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 180;
  if (force || nearBottom) scroll.scrollTop = scroll.scrollHeight;
}

function addUserMessage(text) {
  removeEmptyState();
  const scroll = document.getElementById('chatScroll');
  const row = document.createElement('div');
  row.className = 'msg-row user';
  row.innerHTML = `<div class="avatar" aria-hidden="true"></div>
                   <div class="bubble"><div class="answer-text">${esc(text)}</div></div>`;
  scroll.appendChild(row);
  scrollTop(true);
}

function addAssistantMessage(answerText, meta) {
  removeEmptyState();
  const scroll = document.getElementById('chatScroll');
  const row = document.createElement('div');
  row.className = 'msg-row assistant';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  const riskHtml = renderRisks(meta.risks);
  if (riskHtml) bubble.insertAdjacentHTML('beforeend', riskHtml);

  bubble.insertAdjacentHTML('beforeend', modeBadgeHtml(meta));

  const textEl = document.createElement('div');
  textEl.className = 'answer-text';
  textEl.textContent = answerText;
  bubble.appendChild(textEl);

  const citeBox = document.createElement('div');
  bubble.appendChild(citeBox);
  renderCitations(citeBox, meta.citations);

  const fuBox = document.createElement('div');
  bubble.appendChild(fuBox);
  renderFollowups(fuBox, meta.followup_questions);

  if (meta.notes && meta.notes.length) {
    const det = document.createElement('details');
    det.className = 'notes';
    det.innerHTML = `<summary>技术详情（${meta.notes.length} 条）</summary><ul>` +
      meta.notes.map((n) => `<li>${esc(n)}</li>`).join('') + '</ul>';
    bubble.appendChild(det);
  }

  const tools = document.createElement('div');
  tools.className = 'msg-tools';
  const btnCopy = document.createElement('button');
  btnCopy.innerHTML = ICON('copy') + '复制回答';
  btnCopy.onclick = () => {
    navigator.clipboard.writeText(answerText).then(() => toast('已复制到剪贴板'));
  };
  tools.appendChild(btnCopy);
  const btnExport = document.createElement('button');
  btnExport.innerHTML = ICON('download') + '导出 Markdown';
  btnExport.onclick = () => exportSession();
  tools.appendChild(btnExport);
  const btnWord = document.createElement('button');
  btnWord.innerHTML = ICON('filetext') + '导出 Word 报告';
  btnWord.onclick = () => exportSessionDocx();
  tools.appendChild(btnWord);
  if (meta.elapsed) {
    const span = document.createElement('span');
    span.textContent = `耗时 ${meta.elapsed} 秒`;
    tools.appendChild(span);
  }
  bubble.appendChild(tools);

  row.innerHTML = '<div class="avatar" aria-hidden="true">' + ICON('shield') + '</div>';
  row.appendChild(bubble);
  scroll.appendChild(row);
  scrollTop(true);
  return { row, textEl, bubble };
}

function renderRisks(risks) {
  if (!risks || !risks.length) return '';
  const items = risks.map((r) =>
    `<div class="risk-item">
       <span class="risk-label">${ICON('alert')}${esc(r.label)}</span>
       ${r.clauses && r.clauses.length ? `<span class="risk-clauses">（涉及 ${esc(r.clauses.join('、'))}）</span>` : ''}
       <div class="risk-why">${esc(r.why || '')}</div>
     </div>`).join('');
  return `<div class="risk-banner">
            <div class="risk-title">${ICON('alert')}高风险项提示（建议重点关注）</div>
            ${items}
          </div>`;
}

function renderCitations(box, citations) {
  if (!citations || !citations.length) return;
  const title = document.createElement('div');
  title.className = 'citations-title';
  title.textContent = `引用条款（${citations.length} 条，点击展开查看原文）`;
  const wrap = document.createElement('div');
  wrap.className = 'citations';
  wrap.appendChild(title);

  for (const c of citations) {
    const card = document.createElement('div');
    const risky = (c.risk_tags || []).some((t) =>
      ['encryption', 'access_control', 'personal_info'].includes(t));
    card.className = 'cite-card' + (risky ? ' has-risk' : '');
    const tags = (c.risk_tags || []).map((t) => {
      const label = { encryption: '数据加密', access_control: '访问控制', personal_info: '个人信息保护' }[t] || t;
      const icon = ['encryption', 'access_control', 'personal_info'].includes(t) ? ICON('alert') : '';
      return `<span class="tag risk">${icon}${esc(label)}</span>`;
    }).join('');
    const typeTag = c.text_type === 'summary'
      ? '<span class="tag summary-mark">要点摘要（非标准原文）</span>' : '';
    // 来源标识：内置种子还是用户导入
    const srcBadge = c.source_kind === 'user'
      ? '<span class="src-badge user">用户导入</span>'
      : '<span class="src-badge">内置</span>';
    const srcLink = c.source_url
      ? `<a class="src-link" href="${esc(c.source_url)}" target="_blank" rel="noopener">${ICON('external')}查看官方来源</a>`
      : '';

    card.innerHTML = `
      <div class="cite-head">
        <span class="cite-arrow">${ICON('chevron-right')}</span>
        <span class="cite-id">${esc(c.clause_id)}</span>
        <span class="cite-std">${esc(c.standard_name)}</span>
        <span class="cite-title">${esc(c.title)}</span>
      </div>
      <div class="cite-body">
        <div class="cite-meta">${esc((c.chapter_path || []).join(' › '))}</div>
        <div class="cite-text">${esc(c.text || '')}</div>
        <div class="cite-tags">${tags}${typeTag}${srcBadge}${srcLink}</div>
      </div>`;
    card.querySelector('.cite-head').onclick = () => card.classList.toggle('open');
    wrap.appendChild(card);
  }
  box.appendChild(wrap);
}

/* 模式徽标：非流式与流式两条渲染路径共用，避免两边逻辑漂移
   （历史上正是因为两条路径各写一份，修了一边漏了另一边）。
   可以同时出现多个徽标，比如"点名了库外法规" + "知识库里没找到"。 */
function modeBadgeHtml(meta) {
  if (!meta) return '';
  const wrap = (cls, text) =>
    `<div class="badge-row"><span class="mode-badge${cls ? ' ' + cls : ''}">${text}</span></div>`;
  const badges = [];

  if (meta.mode === 'needs_context') {
    // 这类问题不是"没找到条款"，而是"缺了系统信息就没法判定"，
    // 必须和"未找到"区分开，否则用户会以为知识库里没有相关内容。
    badges.push(wrap('', '需要先了解您的系统情况，才能判断是否适用'));
    return badges.join('');
  }

  if (meta.out_of_kb && meta.out_of_kb.length) {
    const names = meta.out_of_kb.map((n) => String(n).replace(/《.*?》/g, '')).join('、');
    badges.push(wrap('out-of-kb', `您提到的 ${names} 未收录，以下是与它相关的合规要求`));
  }
  if (meta.need_clarification) {
    badges.push(wrap('', '信息不足，需要您补充几个情况'));
  } else if (meta.mode === 'retrieval_only') {
    badges.push(wrap('', '大模型不可用，已降级为条款检索'));
  } else if (meta.mode === 'no_result' || meta.found === false) {
    badges.push(wrap('no-result', '知识库中未找到对应条款'));
  }
  return badges.join('');
}

function renderFollowups(box, questions) {
  if (!questions || !questions.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'followups';
  wrap.innerHTML = '<div class="followups-title">为了给出更准确的条款依据，请补充以下信息：</div>';
  for (const q of questions) {
    const btn = document.createElement('button');
    btn.className = 'followup-btn';
    btn.textContent = q;
    btn.onclick = () => {
      document.getElementById('input').value = q;
      document.getElementById('input').focus();
    };
    wrap.appendChild(btn);
  }
  box.appendChild(wrap);
}

/* ---------------- 发送 ---------------- */
async function send(text) {
  if (state.sending) return;
  text = (text || '').trim();
  if (!text) return;

  state.sending = true;
  document.getElementById('btnSend').disabled = true;
  document.getElementById('input').value = '';
  autoGrow(document.getElementById('input'));

  addUserMessage(text);

  // 占位的助手气泡（打字状态）
  removeEmptyState();
  const scroll = document.getElementById('chatScroll');
  const row = document.createElement('div');
  row.className = 'msg-row assistant';
  row.innerHTML = `<div class="avatar" aria-hidden="true">${ICON('shield')}</div>
    <div class="bubble"><div class="status-line">
      <span class="typing"><span></span><span></span><span></span></span>
      <span id="stageText">正在检索知识库条款…</span>
    </div></div>`;
  scroll.appendChild(row);
  scrollTop(true);

  const bubble = row.querySelector('.bubble');
  let textEl = null;
  let acc = '';

  try {
    await API.chatStream(text, state.sessionId, {
      status: (d) => {
        const el = document.getElementById('stageText');
        if (el) el.textContent = d.text || '处理中…';
      },
      session: (d) => {
        state.sessionId = d.session_id;
      },
      delta: (d) => {
        if (!textEl) {
          bubble.innerHTML = '';
          textEl = document.createElement('div');
          textEl.className = 'answer-text';
          bubble.appendChild(textEl);
        }
        acc += d.text;
        textEl.textContent = acc;
        scrollTop();
      },
      // 服务端在生成结束后的校验里改动了正文（位置序号翻译、补免责声明、
      // 清理矛盾说法），这里整段替换，保证用户看到的是校验后的内容
      replace: (d) => {
        if (!textEl) {
          bubble.innerHTML = '';
          textEl = document.createElement('div');
          textEl.className = 'answer-text';
          bubble.appendChild(textEl);
        }
        acc = d.text;
        textEl.textContent = acc;
        scrollTop();
      },
      meta: (d) => {
        if (!textEl) {
          bubble.innerHTML = '';
          textEl = document.createElement('div');
          textEl.className = 'answer-text';
          textEl.textContent = acc;
          bubble.appendChild(textEl);
        }
        // 风险条插到最上面
        const riskHtml = renderRisks(d.risks);
        if (riskHtml) bubble.insertAdjacentHTML('afterbegin', riskHtml);

        // 需要补充信息时优先显示追问提示，不能显示成"没找到"。
        // 先去掉上一轮可能插入的同名节点，避免 meta 事件重复触发时堆出一串徽标。
        bubble.querySelectorAll('.mode-badge').forEach((el) => {
          const holder = el.parentElement;
          // 只在外面还包着一层容器时才删——绝不能让 parentElement 是 bubble 本身。
          if (holder && holder !== bubble) holder.remove();
          else el.remove();
        });
        bubble.insertAdjacentHTML('afterbegin', modeBadgeHtml(d));

        const citeBox = document.createElement('div');
        renderCitations(citeBox, d.citations);
        bubble.appendChild(citeBox);

        const fuBox = document.createElement('div');
        renderFollowups(fuBox, d.followup_questions);
        bubble.appendChild(fuBox);

        if (d.notes && d.notes.length) {
          const det = document.createElement('details');
          det.className = 'notes';
          det.innerHTML = `<summary>技术详情（${d.notes.length} 条）</summary><ul>` +
            d.notes.map((n) => `<li>${esc(n)}</li>`).join('') + '</ul>';
          bubble.appendChild(det);
        }

        const tools = document.createElement('div');
        tools.className = 'msg-tools';
        const b1 = document.createElement('button');
        b1.innerHTML = ICON('copy') + '复制回答';
        b1.onclick = () => navigator.clipboard.writeText(acc).then(() => toast('已复制到剪贴板'));
        tools.appendChild(b1);
        const b2 = document.createElement('button');
        b2.innerHTML = ICON('download') + '导出 Markdown';
        b2.onclick = () => exportSession();
        tools.appendChild(b2);
        const b3 = document.createElement('button');
        b3.innerHTML = ICON('filetext') + '导出 Word 报告';
        b3.onclick = () => exportSessionDocx();
        tools.appendChild(b3);
        if (d.elapsed) {
          const s = document.createElement('span');
          s.textContent = `耗时 ${d.elapsed} 秒`;
          tools.appendChild(s);
        }
        bubble.appendChild(tools);

        scrollTop();
        loadSessions();
      },
      error: (d) => {
        bubble.innerHTML = `<div class="msg-error">出错了：${esc(d.message || '未知错误')}</div>`;
      },
    });
  } catch (e) {
    bubble.innerHTML = `<div class="msg-error">请求失败：${esc(e.message)}</div>`;
  } finally {
    state.sending = false;
    document.getElementById('btnSend').disabled = false;
    scrollTop();
  }
}

async function exportSession() {
  if (!state.sessionId) {
    toast('本次对话还没有记录');
    return;
  }
  try {
    const data = await API.get(`/api/sessions/${state.sessionId}/export`);
    downloadText(data.filename, data.content);
    toast('已导出 Markdown 自查记录');
  } catch (e) {
    toast('导出失败：' + e.message);
  }
}

/* 导出 Word 报告：后端直接返回 .docx 文件流，让浏览器下载 */
function exportSessionDocx() {
  if (!state.sessionId) {
    toast('本次对话还没有记录');
    return;
  }
  toast('正在生成 Word 报告…');
  // 用隐藏的 iframe 触发下载，避免整页跳转
  const frame = document.createElement('iframe');
  frame.style.display = 'none';
  frame.src = `/api/sessions/${state.sessionId}/export.docx`;
  document.body.appendChild(frame);
  setTimeout(() => frame.remove(), 15000);
}

/* ---------------- 输入框 ---------------- */
function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

/* ---------------- 初始化 ---------------- */
async function init() {
  renderEmptyState();

  const input = document.getElementById('input');
  input.addEventListener('input', () => autoGrow(input));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input.value);
    }
  });
  document.getElementById('btnSend').onclick = () => send(input.value);
  document.getElementById('btnNew').onclick = newChat;

  const ready = await pollStartup();
  if (ready) {
    await refreshHealth();
    await loadSessions();
    input.focus();
    setInterval(refreshHealth, 30000);
  }
}

init();
