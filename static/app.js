let state = null;
let viewedProfileId = null;
let composerBusy = false;
let refreshInFlight = false;

const els = {
  hdrTech: document.getElementById('hdr-tech'),
  hdrAI: document.getElementById('hdr-ai'),
  hdrAdmin: document.getElementById('hdr-admin'),
  headPersona: document.getElementById('head-persona'),
  techPhone: document.getElementById('tech-phone'),
  assignPhone: document.getElementById('assign-phone'),
  techThread: document.getElementById('tech-thread'),
  adminThread: document.getElementById('admin-thread'),
  techMemory: document.getElementById('tech-memory'),
  adminMemory: document.getElementById('admin-memory'),
  profilesList: document.getElementById('profiles-list'),
  techForm: document.getElementById('tech-form'),
  techInput: document.getElementById('tech-input'),
  adminForm: document.getElementById('admin-form'),
  adminInput: document.getElementById('admin-input'),
  assignForm: document.getElementById('assign-form'),
  assignJobId: document.getElementById('assign-jobid'),
  liveDot: document.getElementById('live-dot'),
};

async function fetchJson(url, opts = {}) {
  const resp = await fetch(url, {
    headers: {'Content-Type': 'application/json'},
    ...opts,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status}: ${text}`);
  }
  return resp.json();
}

function fmtTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  } catch { return iso; }
}

function fmtRelative(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
  } catch { return iso; }
}

function renderHeader(s) {
  els.hdrTech.textContent = s.techPhone;
  els.hdrAI.textContent = s.aiNumber;
  els.hdrAdmin.textContent = s.adminPersona || 'Dispatcher';
  if (els.headPersona) els.headPersona.textContent = s.adminPersona || 'Dispatcher';
  els.techPhone.textContent = s.techPhone;
  els.assignPhone.textContent = s.techPhone;
}

function messageBubble(msg, viewpoint /* 'tech' or 'admin' */) {
  const div = document.createElement('div');
  div.className = `msg ${msg.author || 'unknown'}`;
  const who = document.createElement('div');
  who.className = 'who';
  const label = {tech: 'Technician', ai: 'AI Agent', admin: 'Admin', unknown: 'Unknown'}[msg.author] || 'Unknown';
  who.textContent = viewpoint === 'tech' && msg.author === 'tech' ? 'You' : label;
  const text = document.createElement('div');
  text.textContent = msg.text || '';
  const when = document.createElement('div');
  when.className = 'when';
  when.textContent = fmtTime(msg.occurredAt);
  div.appendChild(who);
  div.appendChild(text);
  div.appendChild(when);
  return div;
}

function renderThread(container, messages, viewpoint) {
  const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
  container.innerHTML = '';
  if (!messages || messages.length === 0) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'No messages yet.';
    container.appendChild(e);
    return;
  }
  for (const m of messages) {
    container.appendChild(messageBubble(m, viewpoint));
  }
  if (wasAtBottom) {
    container.scrollTop = container.scrollHeight;
  }
}

function renderProfiles(s) {
  els.profilesList.innerHTML = '';
  if (!s.profiles || s.profiles.length === 0) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'No profiles yet. Assign a job to create one.';
    els.profilesList.appendChild(e);
    return;
  }
  for (const p of s.profiles) {
    const card = document.createElement('div');
    card.className = 'profile-card' + (viewedProfileId === p.id ? ' viewed' : '');
    card.dataset.profileId = p.id;

    const top = document.createElement('div');
    top.className = 'top';
    const jobid = document.createElement('div');
    jobid.className = 'jobid';
    jobid.textContent = `job ${p.jobID || '?'}`;
    const status = document.createElement('div');
    status.className = `status ${p.status || 'completed'}`;
    status.textContent = p.status || '—';
    top.appendChild(jobid);
    top.appendChild(status);

    const meta = document.createElement('div');
    meta.className = 'meta';
    const convBadge = p.conversationStatus
      ? `<span class="conv-state ${p.conversationStatus.toLowerCase()}">${p.conversationStatus}</span>`
      : '';
    meta.innerHTML = `
      <div>started ${fmtRelative(p.startedAt)}</div>
      <div>last active ${fmtRelative(p.lastActiveAt)}</div>
      ${p.completedAt ? `<div>completed ${fmtRelative(p.completedAt)}</div>` : ''}
      <div class="idrow"><span class="idlabel">profile</span> <code class="idval">${p.id}</code></div>
      <div class="idrow"><span class="idlabel">conv</span> <code class="idval">${p.conversationId || '—'}</code> ${convBadge}</div>
    `;

    const actions = document.createElement('div');
    actions.className = 'actions';
    if (p.status === 'active') {
      const btn = document.createElement('button');
      btn.textContent = 'Complete job';
      btn.className = 'danger';
      btn.addEventListener('click', (e) => { e.stopPropagation(); completeJob(p.id); });
      actions.appendChild(btn);
    } else if (p.status === 'paused' || p.status === 'completed') {
      const btn = document.createElement('button');
      btn.textContent = p.status === 'paused' ? 'Resume' : 'Reactivate';
      btn.addEventListener('click', (e) => { e.stopPropagation(); reactivateJob(p.id); });
      actions.appendChild(btn);
    }
    const del = document.createElement('button');
    del.textContent = 'Delete';
    del.className = 'danger outline';
    del.addEventListener('click', (e) => { e.stopPropagation(); deleteProfile(p.id, p.jobID); });
    actions.appendChild(del);

    card.appendChild(top);
    card.appendChild(meta);
    card.appendChild(actions);
    card.addEventListener('click', () => {
      viewedProfileId = p.id;
      refresh();
    });
    els.profilesList.appendChild(card);
  }
}

function renderMemoryPanel(container, memory, currentConvId) {
  container.innerHTML = '';
  if (!memory) return;
  if (memory.error) {
    const e = document.createElement('div');
    e.className = 'mem-error';
    e.textContent = `Memory unavailable: ${memory.error}`;
    container.appendChild(e);
    return;
  }
  const summaries = (memory.summaries || []).slice().sort((a, b) => (b.occurredAt || '').localeCompare(a.occurredAt || ''));
  const observations = (memory.observations || []).slice().sort((a, b) => (b.occurredAt || '').localeCompare(a.occurredAt || ''));
  if (summaries.length === 0 && observations.length === 0) {
    const e = document.createElement('div');
    e.className = 'mem-empty';
    e.textContent = 'No job memory yet. Summaries and observations appear here after a conversation closes.';
    container.appendChild(e);
    return;
  }

  const details = document.createElement('details');
  details.className = 'mem-details';
  details.open = true;
  const summary = document.createElement('summary');
  summary.innerHTML = `<span class="mem-title">Job memory</span> <span class="mem-counts">${summaries.length} summary · ${observations.length} obs</span>`;
  details.appendChild(summary);

  if (summaries.length > 0) {
    const h = document.createElement('div');
    h.className = 'mem-section-head';
    h.textContent = 'Summaries (across conversations)';
    details.appendChild(h);
    for (const s of summaries) {
      const row = document.createElement('div');
      row.className = 'mem-row summary';
      if (s.conversationId && currentConvId && s.conversationId === currentConvId) row.classList.add('current');
      const meta = document.createElement('div');
      meta.className = 'mem-meta';
      const convTag = s.conversationId
        ? `<code class="mem-conv">${s.conversationId.slice(0, 10)}…${s.conversationId === currentConvId ? ' <em>current</em>' : ''}</code>`
        : '';
      meta.innerHTML = `${convTag} <span class="mem-when">${fmtRelative(s.occurredAt)}</span> <span class="mem-source">${s.source || ''}</span>`;
      const body = document.createElement('div');
      body.className = 'mem-body';
      body.textContent = s.content || '';
      row.appendChild(meta);
      row.appendChild(body);
      details.appendChild(row);
    }
  }

  if (observations.length > 0) {
    const h = document.createElement('div');
    h.className = 'mem-section-head';
    h.textContent = 'Observations';
    details.appendChild(h);
    for (const o of observations) {
      const row = document.createElement('div');
      row.className = 'mem-row obs';
      const meta = document.createElement('div');
      meta.className = 'mem-meta';
      meta.innerHTML = `<span class="mem-when">${fmtRelative(o.occurredAt)}</span> <span class="mem-source">${o.source || ''}</span>`;
      const body = document.createElement('div');
      body.className = 'mem-body';
      body.textContent = o.content || '';
      row.appendChild(meta);
      row.appendChild(body);
      details.appendChild(row);
    }
  }

  container.appendChild(details);
}

function renderViewNotice(s) {
  // Remove any prior notice.
  document.querySelectorAll('.notice').forEach(n => n.remove());
  if (!s.viewedProfileId || s.viewedProfileId === s.activeProfileId) return;
  const viewed = (s.profiles || []).find(p => p.id === s.viewedProfileId);
  if (!viewed) return;
  const notice = document.createElement('div');
  notice.className = 'notice';
  notice.textContent = `Viewing a ${viewed.status || 'closed'} job (${viewed.jobID}). Inbound tech SMS will land on the active job, not this one.`;
  els.adminThread.parentElement.insertBefore(notice, els.adminThread);
}

function render(s) {
  state = s;
  renderHeader(s);
  const conv = s.viewedConversation || {};
  const msgs = conv.messages || [];
  renderMemoryPanel(els.techMemory, s.viewedProfileMemory, conv.id);
  renderMemoryPanel(els.adminMemory, s.viewedProfileMemory, conv.id);
  renderThread(els.techThread, msgs, 'tech');
  renderThread(els.adminThread, msgs, 'admin');
  renderProfiles(s);
  renderViewNotice(s);
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const s = await fetchJson('/api/state' + (viewedProfileId ? `?viewedProfileId=${encodeURIComponent(viewedProfileId)}` : ''));
    if (viewedProfileId == null) viewedProfileId = s.activeProfileId || null;
    els.liveDot.style.color = '';
    render(s);
  } catch (e) {
    console.error(e);
    els.liveDot.style.color = 'red';
  } finally {
    refreshInFlight = false;
  }
}

async function assignJob(jobID) {
  const resp = await fetchJson('/api/assign-job', {
    method: 'POST',
    body: JSON.stringify({jobID}),
  });
  viewedProfileId = resp.profileId;
  await refresh();
}

async function adminSend(text) {
  const conv = state?.viewedConversation;
  if (!conv?.id) throw new Error('No conversation to send to.');
  await fetchJson('/api/admin/send', {
    method: 'POST',
    body: JSON.stringify({conversationId: conv.id, text}),
  });
  await refresh();
}

async function techSimulate(text) {
  const conv = state?.viewedConversation;
  if (!conv?.id) throw new Error('No conversation to send to.');
  await fetchJson('/api/tech/simulate', {
    method: 'POST',
    body: JSON.stringify({conversationId: conv.id, text}),
  });
  await refresh();
}

async function completeJob(profileId) {
  if (!confirm('Mark this job completed and close its conversation?')) return;
  await fetchJson('/api/complete-job', {
    method: 'POST',
    body: JSON.stringify({profileId}),
  });
  await refresh();
}

async function deleteProfile(profileId, jobID) {
  const label = jobID ? `job ${jobID}` : 'this profile';
  if (!confirm(`Delete ${label}? This closes its conversation and removes the profile from Memory. Cannot be undone.`)) return;
  await fetchJson(`/api/profile/${encodeURIComponent(profileId)}`, {method: 'DELETE'});
  if (viewedProfileId === profileId) viewedProfileId = null;
  await refresh();
}

async function reactivateJob(profileId) {
  await fetchJson('/api/reactivate-job', {
    method: 'POST',
    body: JSON.stringify({profileId}),
  });
  viewedProfileId = profileId;
  await refresh();
}

function guard(fn) {
  return async (ev) => {
    ev.preventDefault();
    if (composerBusy) return;
    composerBusy = true;
    try {
      await fn(ev);
    } catch (e) {
      alert(e.message);
      console.error(e);
    } finally {
      composerBusy = false;
    }
  };
}

els.assignForm.addEventListener('submit', guard(async () => {
  const v = els.assignJobId.value.trim();
  if (!v) return;
  await assignJob(v);
  els.assignJobId.value = '';
}));

els.adminForm.addEventListener('submit', guard(async () => {
  const v = els.adminInput.value.trim();
  if (!v) return;
  await adminSend(v);
  els.adminInput.value = '';
}));

els.techForm.addEventListener('submit', guard(async () => {
  const v = els.techInput.value.trim();
  if (!v) return;
  await techSimulate(v);
  els.techInput.value = '';
}));

document.querySelectorAll('[data-refresh], #refresh-all').forEach(btn => {
  btn.addEventListener('click', () => { refresh(); });
});

refresh();
