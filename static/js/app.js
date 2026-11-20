const msg = document.getElementById('msg');
const view = document.getElementById('view');
let windows = [];

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(j.error || ('HTTP ' + r.status));
    err.details = j.details || null;
    err.status = r.status;
    throw err;
  }
  return j;
}
function setMsg(t, err = false) { msg.textContent = t || ''; msg.className = err ? 'err' : 'muted'; }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
async function loadWindows() { windows = (await api('/api/windows')).items || []; }
function winOptions() { return windows.map(w => `<option value="${w.id}">#${w.id} ${esc(w.table_name)}</option>`).join(''); }

function outcomeTag(o) {
  if (o === '占片成功' || o === '已落库') return `<span class="tag ok">${esc(o)}</span>`;
  if (o === '拒重') return `<span class="tag danger">拒重</span>`;
  return `<span class="tag">${esc(o)}</span>`;
}
function lockChip(no) { return no ? `<span class="chip">片锁 ${esc(no)}</span>` : `<span class="chip muted">无片锁</span>`; }
function rejectChip(no) { return no ? `<span class="chip danger">拒重号 ${esc(no)}</span>` : ''; }

function renderWindows() {
  view.innerHTML = `<div class="wall">${windows.map(w => `
    <article class="card">
      <h3>${esc(w.table_name)}</h3>
      <p class="muted">${esc(w.start_date)} → ${esc(w.end_date)}</p>
      <span class="tag">${esc(w.status)}</span>
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count}</p>
    </article>`).join('')}</div>`;
}

function renderPlan() {
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">生成切片计划</button>
    <pre id="out" class="muted"></pre>
  </div>`;
  document.getElementById('go').onclick = async () => {
    try {
      const wid = document.getElementById('wid').value;
      const j = await api(`/api/windows/${wid}/plan`, { method: 'POST', body: '{}' });
      document.getElementById('out').textContent = JSON.stringify(j.slices, null, 2);
      setMsg(`已生成 ${j.slices.length} 片`);
      await loadWindows();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function renderReplay() {
  await loadWindows();
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <label>值班人<input id="claimer" value="回灌值班" /></label>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async () => {
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s => `
      <article class="card">
        <h3>${esc(s.slice_date)}</h3>
        <p class="mono muted">${esc(s.slice_key)}</p>
        <span class="tag">${esc(s.status)}</span>
        ${lockChip(s.lock_no)}
        <p class="muted">行数 ${s.row_count} · 同片成功 ${s.success_count}</p>
        <button data-id="${s.id}" class="primary" type="button" ${s.status !== '待重放' ? 'disabled' : ''}>重放</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn => {
      btn.onclick = async () => {
        try {
          const claimer = document.getElementById('claimer').value.trim() || '回灌值班';
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, { method: 'POST', body: JSON.stringify({ claimer }) });
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行 · 片锁 ${j.result.lock_no}`);
          await draw(); await loadWindows();
        } catch (e) {
          setMsg(`${e.message}${e.details?.reject_no ? ' · 拒重号 ' + e.details.reject_no : ''}`, true);
          await draw();
        }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

/* ---------------- 占片闸口 ---------------- */
async function renderGate() {
  await loadWindows();
  view.innerHTML = `<div class="card">
    <h3>占片闸口</h3>
    <p class="muted">同片连点只许成功一次；拒重会记入争用台账并亮拒重号，闸口亮片锁号。</p>
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <label>切片<select id="sid"></select></label>
    <label>值班人<input id="claimer" value="调度甲" /></label>
    <div class="row">
      <button class="primary" id="claim" type="button">占片</button>
      <button id="race" type="button">同片争抢一轮</button>
    </div>
    <div id="gate-out" class="card inner" hidden></div>
  </div>`;
  const loadSlices = async () => {
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('sid').innerHTML = items.map(s =>
      `<option value="${s.id}">${esc(s.slice_date)} · ${esc(s.slice_key)} · ${esc(s.status)}</option>`).join('')
      || '<option value="">无切片</option>';
  };
  const drawOut = (r) => {
    const out = document.getElementById('gate-out');
    out.hidden = false;
    const rej = r.outcome === '拒重';
    out.innerHTML = `
      <div class="row">${outcomeTag(r.outcome)} ${lockChip(r.lock_no)} ${rejectChip(r.reject_no)}</div>
      <p class="mono muted">片键 ${esc(r.slice_key)} · 值班 ${esc(r.claimer)} · 尝试 #${r.attempt_no}</p>
      <p class="${rej ? 'err' : 'muted'}">${esc(r.detail || '')}</p>`;
  };
  document.getElementById('wid').onchange = loadSlices;
  await loadSlices();

  document.getElementById('claim').onclick = async () => {
    const sid = document.getElementById('sid').value;
    if (!sid) { setMsg('先选择切片', true); return; }
    try {
      const claimer = document.getElementById('claimer').value.trim();
      const j = await api(`/api/slices/${sid}/claim`, { method: 'POST', body: JSON.stringify({ claimer }) });
      drawOut(j);
      setMsg(`占片成功 · 片锁 ${j.lock_no}`);
    } catch (e) {
      const d = e.details;
      if (d) { drawOut(d); setMsg(`${e.message} · 拒重号 ${d.reject_no || '-'}`, true); }
      else setMsg(e.message, true);
    }
  };

  document.getElementById('race').onclick = async () => {
    const sid = document.getElementById('sid').value;
    if (!sid) { setMsg('先选择切片', true); return; }
    try {
      const j = await api('/api/contention', { method: 'POST', body: JSON.stringify({ slice_id: Number(sid) }) });
      const out = document.getElementById('gate-out');
      out.hidden = false;
      out.innerHTML = `
        <h3>争抢 ${esc(j.round.round_no)}</h3>
        <div class="row">${lockChip(j.gate.lock_no)} ${rejectChip(j.gate.reject_no)}</div>
        <div class="wall inner-wall">${j.claims.map(c => `
          <article class="card inner">
            <div class="row">${outcomeTag(c.outcome)} ${lockChip(c.lock_no)} ${rejectChip(c.reject_no)}</div>
            <p class="muted">${esc(c.claimer)}</p>
            <p class="muted mono">${esc(c.detail)}</p>
          </article>`).join('')}</div>
        <p class="muted">目标表同片键行数 ${j.target_count} · 落败方二次重放：${esc(j.replay?.duplicate_replay?.reject_no || '-')}</p>`;
      setMsg(`争抢一轮完成 · 锁 ${j.gate.lock_no} · 拒重 ${j.gate.reject_no}`);
      await loadSlices();
    } catch (e) { setMsg(e.message, true); }
  };
}

/* ---------------- 争用台账 ---------------- */
async function renderLedger() {
  await loadWindows();
  view.innerHTML = `<div class="card">
    <h3>争用台账</h3>
    <div class="row">
      <label style="flex:1">片键过滤<input id="key" placeholder="表名#日期，可空" /></label>
      <button class="primary" id="go" type="button">查询</button>
    </div>
    <div id="ledger" class="wall" style="margin-top:0.8rem"></div>
    <h3 style="margin-top:1.2rem">编排争抢记录</h3>
    <div id="rounds" class="wall"></div>
  </div>`;
  const load = async () => {
    const key = document.getElementById('key').value.trim();
    const q = key ? `?slice_key=${encodeURIComponent(key)}` : '';
    const items = (await api('/api/claim-ledger' + q)).items || [];
    document.getElementById('ledger').innerHTML = items.map(e => `
      <article class="card ${e.outcome === '拒重' ? 'card-reject' : ''}">
        <div class="row">${outcomeTag(e.outcome)} ${lockChip(e.lock_no)} ${rejectChip(e.reject_no)}</div>
        <p class="mono muted">${esc(e.slice_key)}</p>
        <p class="muted">值班 ${esc(e.claimer)} · 尝试 #${e.attempt_no}${e.round_no ? ' · ' + esc(e.round_no) : ''}</p>
        <p class="${e.outcome === '拒重' ? 'err' : 'muted'}">${esc(e.detail)}</p>
      </article>`).join('') || '<p class="muted">暂无争用记录</p>';
    const rounds = (await api('/api/contention')).items || [];
    document.getElementById('rounds').innerHTML = rounds.map(r => `
      <article class="card">
        <h3>${esc(r.round_no)}</h3>
        <p class="mono muted">${esc(r.slice_key)}</p>
        <div class="row">${lockChip(r.lock_no)} ${rejectChip(r.reject_no)}</div>
        <p class="muted">尝试 ${r.attempts} 次</p>
      </article>`).join('') || '<p class="muted">暂无争抢记录</p>';
  };
  document.getElementById('go').onclick = load;
  await load();
}

/* ---------------- 片键详情 ---------------- */
async function renderDetail() {
  await loadWindows();
  view.innerHTML = `<div class="card">
    <h3>片键详情</h3>
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <label>切片<select id="sid"></select></label>
    <div id="detail" style="margin-top:0.8rem"></div>
  </div>`;
  const loadSlices = async () => {
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('sid').innerHTML = items.map(s =>
      `<option value="${s.id}">${esc(s.slice_date)} · ${esc(s.slice_key)}</option>`).join('')
      || '<option value="">无切片</option>';
    await draw();
  };
  const draw = async () => {
    const sid = document.getElementById('sid').value;
    const box = document.getElementById('detail');
    if (!sid) { box.innerHTML = '<p class="muted">先生成切片计划</p>'; return; }
    const d = await api(`/api/slices/${sid}/detail`);
    box.innerHTML = `
      <div class="card inner">
        <div class="row">
          <span class="tag">${esc(d.slice.status)}</span>
          <span class="tag ${d.aligned ? 'ok' : 'danger'}">${d.aligned ? '片锁与可写对齐' : '片锁与可写未对齐'}</span>
          <span class="tag ${d.writable ? 'ok' : ''}">${d.writable ? '可写' : '不可写'}</span>
        </div>
        <p class="mono muted">片键 ${esc(d.slice.slice_key)} · ${esc(d.slice.table_name)} · ${esc(d.slice.slice_date)}</p>
        <p class="muted">片锁 ${esc(d.last_lock_no || '无')} · 同片成功次数 ${d.success_count} · 拒绝 ${d.reject_count} 次</p>
        <p class="muted">最近拒重号 ${esc(d.last_reject_no || '无')} · 目标表行 ${d.target ? d.target.row_count : 0}</p>
      </div>
      <div class="wall inner-wall">${d.ledger.map(e => `
        <article class="card ${e.outcome === '拒重' ? 'card-reject' : ''}">
          <div class="row">${outcomeTag(e.outcome)} ${lockChip(e.lock_no)} ${rejectChip(e.reject_no)}</div>
          <p class="muted">${esc(e.claimer)} · 尝试 #${e.attempt_no}</p>
          <p class="${e.outcome === '拒重' ? 'err' : 'muted'}">${esc(e.detail)}</p>
        </article>`).join('') || '<p class="muted">该片键暂无台账</p>'}</div>`;
  };
  document.getElementById('wid').onchange = loadSlices;
  document.getElementById('sid').onchange = draw;
  await loadSlices();
}

/* ---------------- 回灌进度 ---------------- */
async function renderProgress() {
  const p = await api('/api/progress');
  view.innerHTML = `<div class="card">
    <h3>回灌进度</h3>
    <p class="muted">按唯一片键聚合，拒重不计数；同片键成功次数恒 ≤ 1，进度不会翻倍。</p>
    <div class="wall">
      ${p.windows.map(w => `
        <article class="card">
          <h3>窗口 #${w.window_id} ${esc(w.table_name)}</h3>
          <div class="bar"><i style="width:${w.progress_pct}%"></i></div>
          <p class="muted">${w.progress_pct}% · 计划 ${w.planned} · 成功片键 ${w.success_keys} · 缺口 ${w.missing}</p>
          <p class="muted">目标行 ${w.target_rows} · 拒重 ${w.reject_count} 次</p>
          <span class="tag">${esc(w.status)}</span>
        </article>`).join('')}
    </div>
    <div class="card inner" style="margin-top:0.9rem">
      <h3>全局</h3>
      <p class="muted">计划片键 ${p.totals.planned_keys} · 成功片键 ${p.totals.success_keys} · 目标行 ${p.totals.target_rows}</p>
      <p class="muted">拒重 ${p.totals.reject_count} 次 · 全局进度 ${p.totals.progress_pct}%</p>
    </div>
    <div class="row" style="margin-top:0.8rem"><button id="reload" type="button">刷新</button></div>
  </div>`;
  document.getElementById('reload').onclick = () => renderProgress();
}

/* ---------------- 覆盖对账 ---------------- */
async function renderCoverage() {
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <h3>覆盖对账</h3>
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r => `
      <article class="card"><h3>窗口 #${r.window_id}</h3><p>${esc(r.summary)}</p></article>`).join('')}</div>
  </div>`;
  document.getElementById('go').onclick = async () => {
    try {
      const j = await api('/api/coverage-reports', { method: 'POST', body: JSON.stringify({ window_id: Number(document.getElementById('wid').value) }) });
      setMsg(j.report.summary);
      await renderCoverage();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg(''); await loadWindows();
  if (tab === 'windows') renderWindows();
  if (tab === 'plan') renderPlan();
  if (tab === 'replay') await renderReplay();
  if (tab === 'gate') await renderGate();
  if (tab === 'ledger') await renderLedger();
  if (tab === 'detail') await renderDetail();
  if (tab === 'progress') await renderProgress();
  if (tab === 'coverage') await renderCoverage();
}
document.querySelectorAll('.chips button').forEach(btn => {
  btn.onclick = () => { document.querySelectorAll('.chips button').forEach(b => b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async () => { await api('/api/logout', { method: 'POST', body: '{}' }); location.href = '/login'; };
show('windows');
