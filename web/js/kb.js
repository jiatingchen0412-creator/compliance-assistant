/* 知识库管理页 */

const kb = {
  page: 1,
  pageSize: 20,
  total: 0,
  filters: { standard_id: '', q: '', risk_tag: '', level: '', chapter: '' },
};

const STD_LABELS = {
  'dengbao_2.0': '等保2.0',
  'nist_csf_2.0': 'NIST CSF 2.0',
  'owasp_top10_2021': 'OWASP Top 10 2021',
};

/* ---------------- 统计 ---------------- */
async function loadStats() {
  const grid = document.getElementById('statGrid');
  try {
    const s = await API.get('/api/kb/stats');
    const riskNames = {
      encryption: '数据加密',
      access_control: '访问控制',
      personal_info: '个人信息保护',
    };
    const riskCounts = Object.entries(s.by_risk)
      .filter(([k]) => riskNames[k])
      // 高风险类别的数字用琥珀色 + 小三角图标表示，不再用"⚠️"，
      // 也不写"（高风险项）"——那会让卡片标题在窄屏上折成两行。
      .map(([k, v]) => `<div class="stat-card"><div class="num warn">${v}</div>
                        <div class="lbl lbl-risk" title="高风险项，涉及${riskNames[k]}，建议重点关注">${ICON('alert')}${riskNames[k]}</div></div>`)
      .join('');

    grid.innerHTML = `
      <div class="stat-card"><div class="num">${s.standards}</div><div class="lbl">已加载标准</div></div>
      <div class="stat-card"><div class="num">${s.clauses}</div><div class="lbl">条款总数</div></div>
      <div class="stat-card"><div class="num">${s.with_vector}</div><div class="lbl">已建语义向量</div></div>
      <div class="stat-card"><div class="num">${(s.by_source && s.by_source.builtin) || 0}</div><div class="lbl">内置条款</div></div>
      <div class="stat-card"><div class="num ok">${(s.by_source && s.by_source.user) || 0}</div><div class="lbl">用户导入条款</div></div>
      <div class="stat-card"><div class="num muted">${s.batches || 0}</div><div class="lbl">导入批次</div></div>
      ${riskCounts}
    `;

    document.getElementById('kbSub').textContent =
      `检索方式：${s.vector_backend}${s.vector_model ? '（' + s.vector_model + '）' : ''} · ` +
      `匹配阈值 ${s.min_score} · 权重 关键词${s.keyword_weight}/语义${s.vector_weight} · ` +
      `索引时间 ${s.last_index}`;

    document.getElementById('statusChip').textContent =
      `${s.standards} 个标准 · ${s.clauses} 条条款 · ${s.with_vector} 条已向量化`;
    document.getElementById('statusChip').className = 'status-chip ok';
  } catch (e) {
    grid.innerHTML = `<div class="stat-card"><div class="num fail">!</div>
                      <div class="lbl">读取失败：${esc(e.message)}</div></div>`;
  }
}

/* ---------------- 目录树 ---------------- */

/**
 * 造一个目录节点。
 *
 * 用 <button> 而不是 <div>：整棵目录树是可滚动区域，如果里面一个可聚焦元素都没有，
 * 键盘用户既滚不动也选不了章节（axe 的 scrollable-region-focusable 报的就是这件事，
 * 之前这里全是带 onclick 的 div，等于只有鼠标能用）。
 * 「全部条款」没有条数，count 传 null 时不渲染计数徽标。
 */
function makeNode(stdId, chapter, label, count, onPick) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'node';
  btn.setAttribute('data-std', stdId);
  btn.setAttribute('data-chapter', chapter);
  btn.innerHTML = `<span>${esc(label)}</span>`
    + (count === null || count === undefined ? '' : `<span class="cnt">${count}</span>`);
  btn.onclick = (e) => {
    e.stopPropagation();
    onPick();
  };
  return btn;
}

async function loadTree() {
  const box = document.getElementById('treeBox');
  try {
    const data = await API.get('/api/kb/tree');
    if (!data.tree.length) {
      box.innerHTML = '<div class="list-hint">知识库为空</div>';
      return;
    }
    box.innerHTML = '';
    const allNode = document.createElement('div');
    allNode.className = 'tree-std';
    allNode.appendChild(makeNode('', '', '全部条款', null,
      () => applyFilter({ standard_id: '', chapter: '' })));
    box.appendChild(allNode);

    for (const std of data.tree) {
      const wrap = document.createElement('div');
      wrap.className = 'tree-std';
      wrap.appendChild(makeNode(std.standard_id, '', std.standard_name, std.count,
        () => applyFilter({ standard_id: std.standard_id, chapter: '' })));

      for (const l1 of std.children) {
        const l1Box = document.createElement('div');
        l1Box.className = 'tree-l1';
        l1Box.appendChild(makeNode(std.standard_id, l1.name, l1.name, l1.count,
          () => applyFilter({ standard_id: std.standard_id, chapter: l1.name })));

        for (const l2 of l1.children) {
          const l2Box = document.createElement('div');
          l2Box.className = 'tree-l2';
          l2Box.appendChild(makeNode(std.standard_id, l2.name, l2.name, l2.count,
            () => applyFilter({ standard_id: std.standard_id, chapter: l2.name })));
          l1Box.appendChild(l2Box);
        }
        wrap.appendChild(l1Box);
      }
      box.appendChild(wrap);
    }
    markActiveNode();
  } catch (e) {
    box.innerHTML = `<div class="list-hint error">目录读取失败</div>`;
  }
}

/**
 * 给当前正在筛选的那个目录节点加高亮。
 * 原来完全没有选中态，用户点了章节之后看不出自己现在看的是哪一段。
 * 判断依据就是节点上的 data-std / data-chapter 和当前筛选条件比对。
 */
function markActiveNode() {
  const stdNow = kb.filters.standard_id || '';
  const chNow = kb.filters.chapter || '';
  document.querySelectorAll('#treeBox .node').forEach((n) => {
    const hit = (n.getAttribute('data-std') || '') === stdNow
      && (n.getAttribute('data-chapter') || '') === chNow;
    n.classList.toggle('active', hit);
    // aria-current 和视觉高亮同步：屏幕阅读器用户也要能知道"当前筛选的是哪一段"
    if (hit) n.setAttribute('aria-current', 'true');
    else n.removeAttribute('aria-current');
  });
}

/* ---------------- 条款列表 ---------------- */
async function loadClauses() {
  const list = document.getElementById('clauseList');
  const pager = document.getElementById('pager');
  list.innerHTML = '<div class="list-hint">加载中…</div>';

  const p = new URLSearchParams();
  if (kb.filters.standard_id) p.set('standard_id', kb.filters.standard_id);
  if (kb.filters.q) p.set('q', kb.filters.q);
  if (kb.filters.risk_tag) p.set('risk_tag', kb.filters.risk_tag);
  if (kb.filters.level) p.set('level', kb.filters.level);
  if (kb.filters.chapter) p.set('chapter', kb.filters.chapter);
  p.set('page', kb.page);
  p.set('page_size', kb.pageSize);

  try {
    const data = await API.get('/api/kb/clauses?' + p.toString());
    kb.total = data.total;

    const parts = [];
    if (kb.filters.standard_id) parts.push('标准：' + (STD_LABELS[kb.filters.standard_id] || kb.filters.standard_id));
    if (kb.filters.chapter) parts.push('章节：' + kb.filters.chapter);
    if (kb.filters.q) parts.push('搜索：' + kb.filters.q);
    if (kb.filters.risk_tag) parts.push('风险类型：' + kb.filters.risk_tag);
    if (kb.filters.level) parts.push('等级：' + kb.filters.level);
    document.getElementById('activeFilter').textContent =
      parts.length ? '当前筛选 → ' + parts.join(' | ') + `（共 ${data.total} 条）` : '';

    if (!data.items.length) {
      list.innerHTML = '<div class="list-hint">没有匹配的条款</div>';
      pager.innerHTML = '';
      return;
    }

    list.innerHTML = '';
    for (const c of data.items) {
      const risky = (c.risk_tags || []).some((t) =>
        ['encryption', 'access_control', 'personal_info'].includes(t));
      const el = document.createElement('div');
      el.className = 'clause-item' + (risky ? ' has-risk' : '');
      const tagHtml = (c.risk_tags || []).map((t) => {
        const label = {
          encryption: '数据加密', access_control: '访问控制', personal_info: '个人信息保护',
        }[t];
        return label
          ? `<span class="tag risk">${ICON('alert')}${label}</span>`
          : `<span class="tag">${esc(t)}</span>`;
      }).join('');
      const lv = (c.level_scope || []).length
        ? `<span class="tag">适用等级 ${esc(c.level_scope.join('/'))}</span>` : '';
      const typeTag = c.text_type === 'summary'
        ? '<span class="tag summary-mark">要点摘要（非标准原文）</span>' : '';
      const kw = (c.keywords || []).slice(0, 8)
        .map((k) => `<span class="tag">${esc(k)}</span>`).join('');

      // 来源追溯（P2-1）
      const srcBadge = c.source_kind === 'user'
        ? '<span class="src-badge user">用户导入</span>'
        : '<span class="src-badge">内置</span>';
      const srcLink = c.source_url
        ? `<a class="src-link" href="${esc(c.source_url)}" target="_blank" rel="noopener">${ICON('external')}官方来源</a>`
        : '';
      const batchInfo = c.batch_id
        ? `<span class="src-meta">批次 #${c.batch_id}${c.batch_filename ? ' · ' + esc(c.batch_filename) : ''}</span>`
        : '';
      const importedAt = c.imported_at
        ? `<span class="src-meta">导入于 ${esc(c.imported_at)}</span>` : '';

      el.innerHTML = `
        <div class="ci-head">
          <span class="cite-id">${esc(c.clause_id)}</span>
          <span class="cite-std">${esc(c.standard_name || STD_LABELS[c.standard_id] || c.standard_id)}</span>
          <strong>${esc(c.title)}</strong>
          ${srcBadge}
        </div>
        <div class="ci-path">${esc((c.chapter_path || []).join(' › '))}</div>
        ${c.summary ? `<div class="ci-sum">${esc(c.summary)}</div>` : ''}
        <div class="ci-text">${esc(c.text).slice(0, 400)}${(c.text || '').length > 400 ? '…' : ''}</div>
        <div class="cite-tags">${tagHtml}${lv}${typeTag}${kw}</div>
        <div class="cite-tags">${batchInfo}${importedAt}${srcLink}</div>
      `;
      list.appendChild(el);
    }

    const pages = Math.max(1, Math.ceil(kb.total / kb.pageSize));
    pager.innerHTML = `
      <button class="btn" ${kb.page <= 1 ? 'disabled' : ''} id="prevPage">上一页</button>
      <span>第 ${kb.page} / ${pages} 页（共 ${kb.total} 条）</span>
      <button class="btn" ${kb.page >= pages ? 'disabled' : ''} id="nextPage">下一页</button>`;
    const prev = document.getElementById('prevPage');
    const next = document.getElementById('nextPage');
    if (prev) prev.onclick = () => { kb.page--; loadClauses(); };
    if (next) next.onclick = () => { kb.page++; loadClauses(); };
  } catch (e) {
    list.innerHTML = `<div class="list-hint error">加载失败：${esc(e.message)}</div>`;
  }
}

function applyFilter(patch) {
  Object.assign(kb.filters, patch || {});
  kb.page = 1;
  if (patch && 'standard_id' in patch) document.getElementById('stdSelect').value = kb.filters.standard_id;
  markActiveNode();
  loadClauses();
}

/* ---------------- 导入 / 维护 ---------------- */
async function upload() {
  const input = document.getElementById('fileInput');
  if (!input.files || !input.files.length) {
    toast('请先选择文件');
    return;
  }
  const fd = new FormData();
  fd.append('file', input.files[0]);
  toast('正在导入并重建索引，请稍候…', 12000);
  try {
    const r = await API.upload('/api/kb/import', fd);
    toast(`导入完成：新增 ${r.added} 条，更新 ${r.updated} 条`);
    input.value = '';
    await loadStats();
    await loadTree();
    await loadClauses();
  } catch (e) {
    toast('导入失败：' + e.message, 5000);
  }
}

async function rebuild() {
  toast('正在重建索引，请稍候…', 12000);
  try {
    const r = await API.post('/api/kb/rebuild');
    toast(`索引重建完成：全文 ${r.fts} 条，向量 ${r.vectors} 条${r.vector_note ? '（' + r.vector_note + '）' : ''}`);
    await loadStats();
  } catch (e) {
    toast('重建失败：' + e.message, 5000);
  }
}

async function showRiskTerms() {
  try {
    const r = await API.get('/api/kb/risk-terms');
    const lines = r.tags.map((t) => `⚠️ ${t.label}：${t.why}`).join('\n\n');
    alert('高风险词表（命中即标注 ⚠️）\n\n' + lines + '\n\n' + (r.disclaimer || ''));
  } catch (e) {
    toast('读取失败：' + e.message);
  }
}

/* 导入批次（来源追溯）：每次导入的来源、条数、时间 */
async function showBatches() {
  const panel = document.getElementById('batchPanel');
  if (panel && panel.style.display !== 'none') {
    panel.style.display = 'none';
    return;
  }
  let box = panel;
  if (!box) {
    box = document.createElement('div');
    box.className = 'import-panel';
    box.id = 'batchPanel';
    document.getElementById('clauseList').before(box);
  }
  box.style.display = 'block';
  box.innerHTML = '<h3>导入批次</h3><p>加载中…</p>';

  try {
    const r = await API.get('/api/kb/batches');
    const batches = r.batches || [];
    if (!batches.length) {
      box.innerHTML = '<h3>导入批次</h3><p>还没有导入记录。</p>';
      return;
    }
    const kindName = { seed: '内置种子', json: 'JSON 导入', document: '文档导入' };

    // 只有"最近一次带备份的用户导入"可以撤销，和后台的判断保持一致
    const latestRevertable = batches.find(
      (b) => b.source_kind === 'user' && !b.reverted && b.backup_path
    );

    const rows = batches.map((b) => {
      const canRevert = latestRevertable && latestRevertable.id === b.id;
      let action = '-';
      if (b.reverted) action = '已撤销';
      else if (canRevert) action = `<button class="btn sm" data-revert="${b.id}">撤销这次导入</button>`;
      else if (b.source_kind === 'user' && b.backup_path) action = '只能撤销最近一次';
      else if (b.source_kind === 'user') action = '无备份';

      return `
      <tr class="${b.reverted ? 'reverted' : ''}">
        <td>#${b.id}</td>
        <td>${esc(b.filename || '-')}</td>
        <td>${esc(b.standard_name || b.standard_id || '-')}</td>
        <td>${esc(kindName[b.kind] || b.kind || '-')}</td>
        <td>${b.source_kind === 'user' ? '<span class="src-badge user">用户导入</span>' : '<span class="src-badge">内置</span>'}</td>
        <td>+${b.added} / ~${b.updated}${b.skipped ? ' / 跳过' + b.skipped : ''}</td>
        <td>${b.clause_count} 条</td>
        <td>${esc(b.created_at || '')}</td>
        <td>${action}</td>
      </tr>`;
    }).join('');

    box.innerHTML = `
      <h3>导入批次与撤销</h3>
      <p>
        每一次导入都留一条记录。内置种子标记为「内置」，你自己导入的资料标记为「用户导入」。
        导入前系统会自动备份数据库，所以<b>最近一次导入可以一键撤销</b>；
        更早的批次不允许撤销——恢复备份会把之后的改动一起退掉，容易造成误解。
      </p>
      <table class="batch-table">
        <thead>
          <tr><th>批次</th><th>文件</th><th>标准</th><th>类型</th><th>来源</th>
              <th>新增/更新</th><th>现存条款</th><th>时间</th><th>操作</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <div id="backupList" class="backup-list"></div>`;

    box.querySelectorAll('[data-revert]').forEach((btn) => {
      btn.onclick = () => revertBatch(parseInt(btn.dataset.revert, 10));
    });

    loadBackupList();
  } catch (e) {
    box.innerHTML = `<h3>导入批次</h3><p class="panel-error">加载失败：${esc(e.message)}</p>`;
  }
}

async function loadBackupList() {
  const holder = document.getElementById('backupList');
  if (!holder) return;
  try {
    const r = await API.get('/api/kb/backups');
    const list = r.backups || [];
    if (!list.length) {
      holder.innerHTML = '<div class="src-meta">还没有数据库备份（导入资料时会自动创建）。</div>';
      return;
    }
    holder.innerHTML = `
      <div class="src-meta backup-title">
        数据库自动备份（保留最近 20 份，位于 ${esc(r.dir)}）
      </div>
      <div class="cite-tags">
        ${list.slice(0, 8).map((b) => `<span class="tag">${esc(b.created_at)} · ${b.size_kb}KB</span>`).join('')}
      </div>`;
  } catch (e) {
    holder.innerHTML = `<div class="src-meta">备份列表读取失败：${esc(e.message)}</div>`;
  }
}

/* 撤销一次导入：把知识库退回到那次导入之前 */
async function revertBatch(batchId) {
  if (!confirm(
    `确定撤销批次 #${batchId} 吗？\n\n` +
    `知识库会退回到这次导入之前的状态。\n` +
    `撤销前系统会再备份一次当前状态，万一撤错了还能找回来。`
  )) return;

  toast('正在撤销，请稍候…', 15000);
  try {
    const r = await API.post(`/api/kb/batches/${batchId}/revert`);
    toast(`撤销完成：条款数 ${r.clauses_before} → ${r.clauses_after}`);
    await loadStats();
    await loadTree();
    await loadClauses();
    await showBatches();
    await showBatches();   // 第一次是收起再展开，确保内容刷新
  } catch (e) {
    toast('撤销失败：' + e.message, 6000);
  }
}

/* ---------------- 初始化 ---------------- */
async function initKb() {
  // 状态栏
  try {
    const h = await API.get('/api/health');
    if (h.llm && h.llm.status === 'ok') {
      document.getElementById('statusChip').textContent = `本地模型已就绪 · ${h.llm.model}`;
      document.getElementById('statusChip').className = 'status-chip ok';
    } else {
      document.getElementById('statusChip').textContent = '大模型未就绪（检索功能不受影响）';
      document.getElementById('statusChip').className = 'status-chip warn';
    }
  } catch (e) { /* 忽略 */ }

  // 标准下拉
  try {
    const s = await API.get('/api/kb/standards');
    const sel = document.getElementById('stdSelect');
    for (const std of s.standards) {
      const opt = document.createElement('option');
      opt.value = std.id;
      opt.textContent = `${std.name}（${std.clause_count} 条）`;
      sel.appendChild(opt);
    }
  } catch (e) { /* 忽略 */ }

  document.getElementById('searchBox').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') applyFilter({ q: e.target.value.trim() });
  });
  document.getElementById('stdSelect').onchange = (e) => applyFilter({ standard_id: e.target.value });
  document.getElementById('riskSelect').onchange = (e) => applyFilter({ risk_tag: e.target.value });
  document.getElementById('levelSelect').onchange = (e) => applyFilter({ level: e.target.value });
  document.getElementById('btnReset').onclick = () => {
    kb.filters = { standard_id: '', q: '', risk_tag: '', level: '', chapter: '' };
    document.getElementById('searchBox').value = '';
    document.getElementById('stdSelect').value = '';
    document.getElementById('riskSelect').value = '';
    document.getElementById('levelSelect').value = '';
    applyFilter({});
  };
  document.getElementById('btnUpload').onclick = upload;
  document.getElementById('btnRebuild').onclick = rebuild;
  document.getElementById('btnRisk').onclick = showRiskTerms;
  document.getElementById('btnBatches').onclick = showBatches;

  await loadStats();
  await loadTree();
  await loadClauses();
}

initKb();
