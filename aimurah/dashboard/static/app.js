// AIMurahV3 dashboard UI logic (vanilla JS).
async function api(path, opts = {}) {
    const resp = await fetch(path, Object.assign({
        headers: {'Content-Type': 'application/json'},
    }, opts));
    let data = {};
    const text = await resp.text();
    try { data = text ? JSON.parse(text) : {}; } catch(e){}
    if (!resp.ok) throw new Error(data.detail || data.error || resp.statusText);
    return data;
}

function toast(message, kind='info') {
    const el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3500);
}

function fmtTs(ts) {
    if (!ts) return '—';
    const d = new Date((typeof ts === 'number' ? ts * 1000 : ts));
    return d.toLocaleString();
}

function fmtNum(n) {
    if (n === null || n === undefined) return '—';
    return Number(n).toLocaleString();
}

// ---------- View switching ----------
const views = ['overview','accounts','models','usage','settings'];
document.querySelectorAll('.sidebar button').forEach(btn => {
    btn.addEventListener('click', () => {
        const target = btn.dataset.view;
        views.forEach(v => {
            document.getElementById('view-'+v).style.display = (v === target) ? 'block' : 'none';
        });
        document.querySelectorAll('.sidebar button').forEach(b => b.classList.toggle('active', b === btn));
        loadView(target);
    });
});

async function loadView(name) {
    if (name === 'overview') return loadOverview();
    if (name === 'accounts') return loadAccounts();
    if (name === 'models') return loadModels();
    if (name === 'usage') return loadUsage();
    if (name === 'settings') return loadSettings();
}

// ---------- Overview ----------
async function loadOverview() {
    try {
        const [dash, keyData, settings] = await Promise.all([
            api('/api/dashboard'),
            api('/api/apikey'),
            api('/api/settings'),
        ]);
        const a = dash.accounts;
        const stats = [
            ['Total accounts', a.total],
            ['Pro', a.pro],
            ['Free', a.free],
            ['Active', a.active || 0],
            ['Remaining credits', fmtNum(Math.round(a.remaining_credits))],
            ['Credit limit', fmtNum(Math.round(a.credit_limit))],
        ];
        document.getElementById('stats').innerHTML = stats.map(
            ([label, value]) => `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div></div>`
        ).join('');
        document.getElementById('api-key').textContent = keyData.api_key || '—';
        const proxyPort = settings.proxy_port || 7830;
        document.getElementById('base-url').textContent = `http://${window.location.hostname}:${proxyPort}`;
        document.getElementById('proxy-status').textContent = `proxy :${proxyPort}`;
        document.getElementById('dash-status').textContent = `dashboard :${settings.dashboard_port || window.location.port}`;
    } catch (e) { toast(e.message, 'error'); }
}

document.getElementById('regen-key-btn').addEventListener('click', async () => {
    if (!confirm('Regenerate API key? Any client using the old key will stop working.')) return;
    const res = await api('/api/apikey/regen', {method: 'POST'});
    document.getElementById('api-key').textContent = res.api_key;
    toast('API key rotated', 'success');
});

// ---------- Accounts ----------
async function loadAccounts() {
    const {accounts} = await api('/api/accounts');
    document.getElementById('accounts-count').textContent = `${accounts.length} accounts`;
    const tbody = document.getElementById('accounts-table');
    if (!accounts.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="muted" style="padding:20px; text-align:center">No accounts yet. Click "Add Kiro account" to get started.</td></tr>';
        return;
    }
    tbody.innerHTML = accounts.map(a => `
        <tr>
            <td>${a.email || '<span class="muted">unknown</span>'}<br>
                <span class="mono muted">${a.id.slice(0,8)}</span></td>
            <td><span class="badge ${a.plan_type === 'pro' ? 'pro' : 'free'}">${a.plan_type || 'free'}</span></td>
            <td><span class="badge status-${a.status}">${a.status}</span></td>
            <td>${fmtNum(Math.round(a.remaining_credits||0))} / ${fmtNum(Math.round(a.credit_limit||0))}</td>
            <td class="muted">${fmtTs(a.last_usage_sync_at)}</td>
            <td class="muted">${fmtTs(a.created_at)}</td>
            <td>
                <button class="btn" data-act="sync" data-id="${a.id}">Sync</button>
                <button class="btn danger" data-act="del" data-id="${a.id}">Delete</button>
            </td>
        </tr>
    `).join('');
}

document.getElementById('accounts-table').addEventListener('click', async (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const id = btn.dataset.id;
    try {
        if (btn.dataset.act === 'del') {
            if (!confirm('Delete this account?')) return;
            await api('/api/accounts/' + encodeURIComponent(id), {method: 'DELETE'});
            toast('Deleted', 'success');
        } else if (btn.dataset.act === 'sync') {
            await api('/api/accounts/' + encodeURIComponent(id) + '/refresh-usage', {method: 'POST'});
            toast('Usage synced', 'success');
        }
        loadAccounts();
    } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('refresh-accounts-btn').addEventListener('click', loadAccounts);

// ---------- Add account modal ----------
let currentOAuthSession = null;
const addModal = document.getElementById('add-account-modal');

function switchTab(kind) {
    document.getElementById('tab-import').style.display = kind === 'import' ? 'block' : 'none';
    document.getElementById('tab-oauth').style.display = kind === 'oauth' ? 'block' : 'none';
    document.getElementById('tab-import-btn').classList.toggle('primary', kind === 'import');
    document.getElementById('tab-oauth-btn').classList.toggle('primary', kind === 'oauth');
}

document.getElementById('tab-import-btn').addEventListener('click', () => switchTab('import'));
document.getElementById('tab-oauth-btn').addEventListener('click', async () => {
    switchTab('oauth');
    if (!currentOAuthSession) {
        try {
            const settings = await api('/api/settings');
            const mode = settings.oauth_engine || 'manual';
            const res = await api('/api/accounts/kiro-oauth/start', {
                method: 'POST', body: JSON.stringify({mode})
            });
            currentOAuthSession = res.session;
            const link = document.getElementById('auth-url-link');
            link.href = res.session.auth_url;
            link.textContent = res.session.auth_url;
            document.getElementById('oauth-message').textContent =
                mode === 'camoufox' ? 'Camoufox starting — or paste the code manually.'
                                    : 'Open the link, sign in, then paste the resulting kiro:// URL.';
        } catch (e) { toast(e.message, 'error'); }
    }
});

document.getElementById('add-account-btn').addEventListener('click', () => {
    currentOAuthSession = null;
    document.getElementById('rt-token').value = '';
    document.getElementById('callback-url').value = '';
    document.getElementById('import-message').textContent = '';
    document.getElementById('oauth-message').textContent = '';
    switchTab('import');
    addModal.style.display = 'flex';
});

document.getElementById('cancel-import-btn').addEventListener('click', () => {
    addModal.style.display = 'none';
});
document.getElementById('cancel-oauth-btn').addEventListener('click', () => {
    addModal.style.display = 'none';
});

document.getElementById('submit-rt-btn').addEventListener('click', async () => {
    const token = document.getElementById('rt-token').value.trim();
    const msg = document.getElementById('import-message');
    if (!token) { msg.textContent = 'paste a refresh token first'; return; }
    msg.textContent = 'Validating token...';
    try {
        const res = await api('/api/accounts/kiro/import-token', {
            method: 'POST', body: JSON.stringify({refresh_token: token})
        });
        toast('Added: ' + (res.account.email || res.account.id) + ' [' + (res.account.plan_type || 'free') + ']', 'success');
        addModal.style.display = 'none';
        loadAccounts();
    } catch (e) {
        msg.textContent = '';
        toast(e.message, 'error');
    }
});

document.getElementById('submit-callback-btn').addEventListener('click', async () => {
    if (!currentOAuthSession) { toast('open the OAuth link first', 'error'); return; }
    const cb = document.getElementById('callback-url').value.trim();
    if (!cb) { toast('Paste the callback URL first', 'error'); return; }
    try {
        const res = await api('/api/accounts/kiro-oauth/complete', {method: 'POST',
            body: JSON.stringify({session_id: currentOAuthSession.id, callback_url: cb})});
        toast('Added: ' + (res.account.email || res.account.id), 'success');
        addModal.style.display = 'none';
        loadAccounts();
    } catch (e) { toast(e.message, 'error'); }
});

// ---------- Models ----------
async function loadModels() {
    const {models} = await api('/api/models');
    document.getElementById('models-count').textContent = `${models.length} models`;
    document.getElementById('models-table').innerHTML = models.map(m => `
        <tr>
            <td class="mono">${m.id}
                ${m.requires_pro ? '<span class="badge pro" style="margin-left:8px">pro</span>' : ''}</td>
            <td>${m.owned_by || '—'}</td>
            <td>${m.tier}</td>
            <td>${fmtNum(m.max_input_tokens)}</td>
            <td>${fmtNum(m.max_output_tokens)}</td>
        </tr>
    `).join('');
}

document.getElementById('refresh-models-btn').addEventListener('click', async () => {
    try {
        const res = await api('/api/models/kiro-pro', {method: 'POST'});
        toast(`Catalog refreshed (${res.pro_only_models.length} pro-only)`, 'success');
        loadModels();
    } catch (e) { toast(e.message, 'error'); }
});

// ---------- Usage ----------
async function loadUsage() {
    const {data} = await api('/api/usage');
    // RTK summary stats
    const total = data.total || {};
    const rtkSaved = total.rtk_saved_bytes || 0;
    const rtkOrig = total.rtk_original_bytes || 0;
    const rtkTokens = total.rtk_saved_tokens || 0;
    const rtkPct = rtkOrig > 0 ? (rtkSaved / rtkOrig * 100).toFixed(1) : '0.0';
    const statsHtml = [
        ['Total requests', fmtNum(total.request_count || 0)],
        ['Total tokens used', fmtNum(total.total_tokens || 0)],
        ['RTK tokens saved', fmtNum(rtkTokens)],
        ['RTK bytes saved', `${fmtNum(rtkSaved)} B`],
        ['RTK original size', `${fmtNum(rtkOrig)} B`],
        ['RTK compression', `${rtkPct}%`],
    ].map(([label, value]) => `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div></div>`).join('');
    document.getElementById('rtk-stats').innerHTML = statsHtml;

    // By model table
    const byModel = (data.by_model || []).map(r => `
        <tr><td class="mono">${r.model}</td>
            <td>${fmtNum(r.request_count)}</td>
            <td>${fmtNum(r.prompt_tokens)}</td>
            <td>${fmtNum(r.completion_tokens)}</td>
            <td>${fmtNum(r.total_tokens)}</td>
            <td>${fmtNum(r.rtk_saved_tokens || 0)} tok <span class="muted">(${fmtNum(r.rtk_saved_bytes || 0)} B)</span></td></tr>
    `).join('') || '<tr><td colspan="6" class="muted">No usage yet.</td></tr>';
    document.getElementById('usage-by-model').innerHTML = byModel;

    // Daily table
    const daily = (data.daily || []).map(r => `
        <tr><td>${r.date}</td>
            <td>${fmtNum(r.request_count)}</td>
            <td>${fmtNum(r.total_tokens)}</td>
            <td>${fmtNum(r.rtk_saved_tokens || 0)}</td>
            <td>${fmtNum(r.rtk_saved_bytes || 0)} B</td></tr>
    `).join('') || '<tr><td colspan="5" class="muted">No usage yet.</td></tr>';
    document.getElementById('usage-daily').innerHTML = daily;

    // Request logs
    loadRequestLogs();
}

async function loadRequestLogs() {
    try {
        const {logs} = await api('/api/usage/request-logs');
        document.getElementById('logs-count').textContent = `${logs.length} recent requests`;
        const tbody = document.getElementById('request-logs-table');
        if (!logs.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="muted">No request logs yet.</td></tr>';
            return;
        }
        tbody.innerHTML = logs.map(r => {
            const time = r.timestamp ? new Date(r.timestamp).toLocaleTimeString() : '—';
            const model = r.model || '—';
            const account = r.account_email ? r.account_email.split('@')[0] : '—';
            const status = r.status_code || 0;
            const statusClass = status === 200 ? 'status-active' : status >= 400 ? 'status-error' : '';
            const tokens = r.completion_tokens || 0;
            const prompt = r.prompt_tokens || 0;
            const latency = r.latency_ms ? (r.latency_ms >= 1000 ? (r.latency_ms / 1000).toFixed(1) + 's' : r.latency_ms + 'ms') : '—';
            const rtk = r.rtk ? `${Math.round(r.rtk.saved_bytes/1024)}KB` : '—';
            const inputPrev = r.input_preview ? `<span title="${r.input_preview.replace(/"/g,'&quot;')}">${r.input_preview.substring(0,40)}${r.input_preview.length > 40 ? '…' : ''}</span>` : '—';
            const outputPrev = r.output_preview ? `<span title="${r.output_preview.replace(/"/g,'&quot;')}">${r.output_preview.substring(0,40)}${r.output_preview.length > 40 ? '…' : ''}</span>` : '—';
            const error = r.error ? `<br><span class="muted" style="font-size:10px">${r.error.substring(0,60)}</span>` : '';
            return `<tr>
                <td class="muted">${time}</td>
                <td class="mono">${model}</td>
                <td class="muted">${account}</td>
                <td><span class="badge ${statusClass}">${status}</span>${error}</td>
                <td>${fmtNum(prompt)}/${fmtNum(tokens)}</td>
                <td class="muted">${latency}</td>
                <td class="muted" style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${inputPrev}</td>
                <td class="muted" style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${outputPrev}</td>
                <td class="muted">${rtk}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        document.getElementById('request-logs-table').innerHTML = `<tr><td colspan="7" class="muted">Error: ${e.message}</td></tr>`;
    }
}

document.getElementById('refresh-logs-btn').addEventListener('click', loadRequestLogs);

// ---------- Settings ----------
async function loadSettings() {
    const cfg = await api('/api/settings');
    document.getElementById('s-token-saver').value = cfg.token_saver_enabled ? 'true' : 'false';
    document.getElementById('s-oauth-engine').value = cfg.oauth_engine || 'manual';
    document.getElementById('s-oauth-headless').value = cfg.oauth_headless ? 'true' : 'false';
    document.getElementById('s-sticky-limit').value = cfg.sticky_round_robin_limit || 3;
    document.getElementById('s-usage-poll').value = cfg.usage_poll_minutes || 10;
    document.getElementById('s-refresh').value = cfg.auto_refresh_minutes || 20;
    document.getElementById('s-proxy-url').value = cfg.proxy_url || '';
}

document.getElementById('save-settings-btn').addEventListener('click', async () => {
    try {
        await api('/api/settings', {method: 'POST', body: JSON.stringify({
            token_saver_enabled: document.getElementById('s-token-saver').value === 'true',
            oauth_engine: document.getElementById('s-oauth-engine').value,
            oauth_headless: document.getElementById('s-oauth-headless').value === 'true',
            sticky_round_robin_limit: Number(document.getElementById('s-sticky-limit').value) || 3,
            usage_poll_minutes: Number(document.getElementById('s-usage-poll').value) || 10,
            auto_refresh_minutes: Number(document.getElementById('s-refresh').value) || 20,
            proxy_url: document.getElementById('s-proxy-url').value.trim(),
        })});
        toast('Settings saved', 'success');
    } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('change-password-btn').addEventListener('click', async () => {
    const current = document.getElementById('cp-current').value;
    const next = document.getElementById('cp-new').value;
    if (!next) { toast('new password required', 'error'); return; }
    try {
        await api('/api/auth/change-password', {method: 'POST',
            body: JSON.stringify({current_password: current, new_password: next})});
        toast('Password changed', 'success');
        document.getElementById('cp-current').value = '';
        document.getElementById('cp-new').value = '';
    } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('logout-btn').addEventListener('click', async () => {
    await api('/api/auth/logout', {method: 'POST'});
    window.location.href = '/login';
});

// ---------- Boot ----------
loadOverview();
