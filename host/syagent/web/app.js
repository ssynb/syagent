import { marked } from './vendor/marked.js';
import DOMPurify from './vendor/purify.js';

const STATUS_POLL_MS = 10_000;
const SHORT_COMMIT = 7;
const FOLLOW_THRESHOLD_PX = 48;
const EXCERPT_CHARS = 60;
const TOAST_MS = 4000;
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|avif|bmp|svg)$/i;
const ROUTE = /^#\/c\/(.+)$/;

const $ = (id) => document.getElementById(id);
const els = {
  sidebar: $('sidebar'), scrim: $('scrim'), menuBtn: $('menu-btn'), convList: $('conv-list'),
  newChat: $('new-chat'), title: $('conv-title'), pill: $('status-pill'), pillText: $('status-text'),
  banner: $('login-banner'), thread: $('thread'), messages: $('messages'), empty: $('empty'),
  composer: $('composer'), box: $('composer-box'), input: $('input'), fileInput: $('file-input'), attach: $('attach-btn'),
  pending: $('pending'), hint: $('composer-hint'), send: $('send-btn'),
  panel: $('panel'), settingsBtn: $('settings-btn'), panelStatus: $('panel-status'),
  restartBtn: $('restart-btn'), rollbackHealthyBtn: $('rollback-healthy-btn'), commitList: $('commit-list'),
  snapshotBtn: $('snapshot-btn'), snapshotList: $('snapshot-list'),
  confirm: $('confirm'), confirmTitle: $('confirm-title'), confirmBody: $('confirm-body'), confirmOk: $('confirm-ok'),
  toasts: $('toasts'), topbar: document.querySelector('.topbar'),
};

const state = {
  conversations: [],
  currentId: null,
  threads: new Map(), // conversation id -> { messages: [], busy: boolean }
  status: null,
  pending: [], // [{ file, url }] attachments waiting to be sent
  following: true,
};
const nodes = new WeakMap(); // message object -> rendered element

/* ---------- Icons ---------- */

const ICONS = {
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4.5M12 16h.01"/>',
  star: '<path d="m12 3.5 2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.2-4.1 5.8-.8z"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  el.append(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

/* ---------- Formatting ---------- */

const shortCommit = (c) => (c ? c.slice(0, SHORT_COMMIT) : '');
const sameCommit = (a, b) => Boolean(a && b) && (a.startsWith(b) || b.startsWith(a));
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const dayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

function shortDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toDateString() === new Date().toDateString() ? timeFmt.format(d) : dayFmt.format(d);
}
function longDate(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : fullFmt.format(d);
}
function excerpt(text) {
  const flat = (text || '').replace(/\s+/g, ' ').trim();
  return flat.length > EXCERPT_CHARS ? `${flat.slice(0, EXCERPT_CHARS)}…` : flat;
}

/* ---------- Markdown (agent output is untrusted) ---------- */

marked.use({ gfm: true, breaks: true });
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.hasAttribute('href')) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});
const PURIFY = {
  FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'iframe', 'object', 'embed'],
  FORBID_ATTR: ['style', 'id', 'class'],
  ALLOW_DATA_ATTR: false,
};
const blockCache = new WeakMap(); // markdown container -> [{ raw, nodes }] per top-level block

// Re-renders only the blocks whose source changed, so streaming does not rebuild (and re-fetch) earlier content.
function paintMarkdown(container, text) {
  const tokens = marked.lexer(text || '');
  const prev = blockCache.get(container) || [];
  let same = 0;
  while (same < prev.length && same < tokens.length && prev[same].raw === tokens[same].raw) same += 1;
  for (const block of prev.slice(same)) block.nodes.forEach((n) => n.remove());
  const next = prev.slice(0, same);
  for (const token of tokens.slice(same)) {
    const html = marked.parser(Object.assign([token], { links: tokens.links }));
    const frag = DOMPurify.sanitize(html, { ...PURIFY, RETURN_DOM_FRAGMENT: true });
    const blockNodes = [...frag.childNodes];
    container.append(frag);
    next.push({ raw: token.raw, nodes: blockNodes });
  }
  blockCache.set(container, next);
}
function plainText(markdown) {
  const root = DOMPurify.sanitize(marked.parse(markdown || ''), { ...PURIFY, RETURN_DOM: true });
  for (const block of root.querySelectorAll('p, li, th, td, h1, h2, h3, h4, h5, h6, pre, br')) block.append(' ');
  return root.textContent;
}

/* ---------- HTTP ---------- */

async function errorText(res) {
  const body = await res.text().catch(() => '');
  return `${res.status} ${res.statusText}${body ? ` — ${body.slice(0, 300)}` : ''}`;
}
async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await errorText(res));
  const type = res.headers.get('content-type') || '';
  return type.includes('application/json') ? res.json() : null;
}
const postJSON = (path, body) => api(path, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});

async function* sseEvents(res) {
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += value.replace(/\r\n?/g, '\n');
    let cut;
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const data = buf.slice(0, cut).split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).replace(/^ /, ''))
        .join('\n');
      buf = buf.slice(cut + 2);
      if (data) yield JSON.parse(data);
    }
  }
}

/* ---------- Toasts and confirm ---------- */

function toast(text, isError = false) {
  const t = h('div', { class: `toast${isError ? ' error' : ''}` }, text);
  els.toasts.append(t);
  // Re-showing moves the popover above any open modal dialog in the top layer.
  if (els.toasts.matches(':popover-open')) els.toasts.hidePopover();
  els.toasts.showPopover();
  setTimeout(() => {
    t.remove();
    if (!els.toasts.childElementCount) els.toasts.hidePopover();
  }, TOAST_MS);
}

function confirmAction({ title, body, action }) {
  els.confirmTitle.textContent = title;
  els.confirmBody.textContent = body;
  els.confirmOk.textContent = action;
  els.confirm.returnValue = '';
  els.confirm.showModal();
  return new Promise((resolve) => {
    els.confirm.addEventListener('close', () => resolve(els.confirm.returnValue === 'ok'), { once: true });
  });
}

/* ---------- Status ---------- */

async function refreshStatus() {
  try {
    state.status = await api('/api/status');
  } catch {
    state.status = null;
  }
  renderStatus();
}

function renderStatus() {
  const s = state.status;
  const pillState = s ? s.agent : 'unreachable';
  els.pill.dataset.state = pillState;
  els.pillText.replaceChildren(
    s ? (s.agent === 'online' ? 'Online' : 'Offline') : 'Host unreachable',
    s?.commit ? h('span', { class: 'status-commit' }, ` · ${shortCommit(s.commit)}`) : '',
  );
  els.banner.hidden = s?.auth !== 'login_required';
  if (els.panel.open) renderPanelStatus();
}

/* ---------- Conversations ---------- */

async function loadConversations() {
  try {
    state.conversations = await api('/api/conversations');
  } catch (e) {
    toast(`Could not load conversations: ${e.message}`, true);
  }
  renderConversations();
}

function conversationTitle(id) {
  const c = state.conversations.find((x) => x.id === id);
  return c?.title || 'New conversation';
}

function renderConversations() {
  const items = state.conversations.map((c) => h('a', {
    class: 'conv',
    href: `#/c/${encodeURIComponent(c.id)}`,
    'aria-current': c.id === state.currentId ? 'page' : null,
    title: c.title || 'New conversation',
  }, h('span', { class: 'conv-name' }, c.title || 'New conversation'),
  h('span', { class: 'conv-time' }, shortDate(c.updated_at))));
  els.convList.replaceChildren(...(items.length ? items : [h('p', { class: 'conv-empty' }, 'No conversations yet')]));
  const title = state.currentId ? conversationTitle(state.currentId) : 'New conversation';
  els.title.textContent = title;
  document.title = `${title} · syagent`;
}

function threadOf(id) {
  if (!state.threads.has(id)) state.threads.set(id, { messages: [], busy: false });
  return state.threads.get(id);
}

async function openRoute() {
  const match = ROUTE.exec(location.hash);
  state.currentId = match ? decodeURIComponent(match[1]) : null;
  closeDrawer();
  renderConversations();
  renderThread();
  updateComposer();
  if (!state.currentId) return;
  const id = state.currentId;
  const thread = threadOf(id);
  if (thread.busy) return;
  try {
    const messages = await api(`/api/conversations/${encodeURIComponent(id)}/messages`);
    if (thread.busy) return;
    thread.messages = messages;
    if (state.currentId === id) renderThread();
  } catch (e) {
    toast(`Could not load messages: ${e.message}`, true);
  }
}

function startNewConversation() {
  els.input.focus();
  if (state.currentId) location.hash = '';
  else closeDrawer();
}

/* ---------- Thread rendering ---------- */

const currentThread = () => (state.currentId ? threadOf(state.currentId) : { messages: [], busy: false });

function renderThread() {
  const { messages } = currentThread();
  els.messages.replaceChildren(...messages.map(renderMessage));
  els.empty.hidden = messages.length > 0;
  state.following = true;
  scrollToBottom();
}

function renderMessage(msg) {
  let el;
  if (msg.role === 'agent') el = renderAgent(msg);
  else if (msg.kind === 'feedback') el = renderFeedbackNote(msg);
  else el = renderOwner(msg);
  el.id = `m-${msg.id}`;
  nodes.set(msg, el);
  return el;
}

function rerender(msg) {
  const old = nodes.get(msg);
  if (old?.isConnected) old.replaceWith(renderMessage(msg));
}

function appendMessage(msg) {
  els.messages.append(renderMessage(msg));
  els.empty.hidden = true;
}

function renderFiles(files = []) {
  if (!files.length) return null;
  return h('div', { class: 'files' }, files.map((f) => (IMAGE_EXT.test(f.name)
    ? h('a', { class: 'thumb', href: f.url, target: '_blank', rel: 'noopener', title: f.name },
      h('img', { src: f.url, alt: f.name, loading: 'lazy' }))
    : h('a', { class: 'chip', href: f.url, download: f.name, title: f.name, html: icon('file') },
      h('span', { class: 'chip-name' }, f.name)))));
}

function renderOwner(msg) {
  return h('article', { class: 'msg owner', 'aria-label': 'You' },
    msg.text ? h('div', { class: 'bubble' }, msg.text) : null,
    renderFiles(msg.files));
}

function scoreBadge(score, label) {
  return h('span', { class: 'score', html: icon('star'), 'aria-label': `${label} ${score} out of 5` },
    h('span', { 'aria-hidden': 'true' }, `${score}/5`));
}

function renderFeedbackNote(msg) {
  const target = currentThread().messages.find((m) => m.id === msg.target_message_id);
  const label = target ? `on “${excerpt(plainText(target.text)) || 'reply'}”` : 'on an earlier reply';
  return h('article', { class: 'msg feedback', 'aria-label': 'Your feedback' },
    h('div', { class: 'feedback-head' },
      h('strong', {}, 'Feedback'),
      msg.score != null ? scoreBadge(msg.score, 'Score') : null,
      target
        ? h('button', { class: 'feedback-target', type: 'button', onclick: () => jumpTo(target.id) }, label)
        : h('span', {}, label)),
    msg.text ? h('div', { class: 'feedback-text' }, msg.text) : null);
}

function latestScore(msg) {
  const fb = currentThread().messages.filter((m) => m.kind === 'feedback' && m.target_message_id === msg.id && m.score != null);
  return fb.length ? fb[fb.length - 1].score : null;
}

function renderAgent(msg) {
  const el = h('article', { class: 'msg agent', 'aria-label': 'Agent', 'aria-busy': msg.streaming ? 'true' : null },
    h('div', { class: 'md' }),
    renderFiles(msg.files),
    msg.error != null || msg.status === 'error'
      ? h('div', { class: 'msg-error', role: 'alert', html: icon('alert') },
        h('span', {}, msg.error || 'This reply failed'))
      : null,
    msg.streaming ? null : renderActions(msg));
  paintBody(msg, el.querySelector('.md'));
  return el;
}

function paintBody(msg, body) {
  if (msg.streaming && !msg.text) {
    body.replaceChildren(h('span', { class: 'typing', 'aria-label': 'Agent is replying' }, h('span'), h('span'), h('span')));
    return;
  }
  if (!blockCache.has(body)) body.replaceChildren();
  paintMarkdown(body, msg.text);
}

function renderActions(msg) {
  const score = latestScore(msg);
  return h('div', { class: 'msg-actions' },
    score != null ? scoreBadge(score, 'Rated') : null,
    h('button', {
      class: 'btn ghost small', type: 'button', html: icon('star'),
      onclick: (e) => toggleFeedbackForm(msg, e.currentTarget),
      'aria-expanded': 'false',
    }, score != null ? 'Rate again' : 'Rate reply'));
}

function jumpTo(id) {
  const el = $(`m-${id}`);
  if (!el) return;
  el.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  el.classList.remove('flash');
  void el.offsetWidth; // restart the highlight when jumping to the same message twice
  el.classList.add('flash');
}

/* ---------- Scroll follow ---------- */

const atBottom = () => els.thread.scrollHeight - els.thread.scrollTop - els.thread.clientHeight < FOLLOW_THRESHOLD_PX;
function scrollToBottom() {
  els.thread.scrollTop = els.thread.scrollHeight;
}
els.thread.addEventListener('scroll', () => {
  state.following = atBottom();
  els.topbar.classList.toggle('scrolled', els.thread.scrollTop > 0);
}, { passive: true });
new ResizeObserver(() => { if (state.following) scrollToBottom(); }).observe(els.messages);

/* ---------- Feedback form ---------- */

function toggleFeedbackForm(msg, button) {
  const article = nodes.get(msg);
  const existing = article.querySelector('.fb-form');
  if (existing) {
    existing.remove();
    button.setAttribute('aria-expanded', 'false');
    return;
  }
  const form = feedbackForm(msg, (restoreFocus) => {
    form.remove();
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button.focus();
  });
  article.append(form);
  button.setAttribute('aria-expanded', 'true');
  form.querySelector('input')?.focus();
}

function feedbackForm(msg, close) {
  const name = `score-${msg.id}`;
  const submit = h('button', { class: 'btn primary small', type: 'submit', disabled: true }, 'Send feedback');
  const text = h('textarea', { name: 'text', placeholder: 'What should change? (optional)', 'aria-label': 'Critique' });
  const form = h('form', { class: 'fb-form' },
    h('fieldset', {},
      h('legend', {}, 'How good was this reply?'),
      h('div', { class: 'scores' }, [1, 2, 3, 4, 5].map((n) => h('label', { class: 'score-opt' },
        h('input', { type: 'radio', name, value: String(n), 'aria-label': `${n} out of 5` }),
        h('span', { 'aria-hidden': 'true' }, String(n))))),
      h('div', { class: 'scale', 'aria-hidden': 'true' }, h('span', {}, 'Poor'), h('span', {}, 'Great'))),
    text,
    h('div', { class: 'fb-actions' },
      h('button', { class: 'btn ghost small', type: 'button', onclick: () => close(true) }, 'Cancel'),
      submit));
  const sync = () => {
    const ready = form.querySelector('input:checked') || text.value.trim();
    submit.disabled = !ready || currentThread().busy;
  };
  form.addEventListener('input', sync);
  form.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(true); });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    sync();
    if (submit.disabled) return;
    const score = form.querySelector('input:checked')?.value ?? null;
    close(false);
    sendFeedback(msg, score == null ? null : Number(score), text.value.trim());
  });
  return form;
}

/* ---------- Turns ---------- */

async function ensureConversation() {
  if (state.currentId) return state.currentId;
  const { id } = await postJSON('/api/conversations');
  threadOf(id);
  history.pushState(null, '', `#/c/${encodeURIComponent(id)}`);
  state.currentId = id;
  loadConversations();
  return id;
}

async function sendMessage() {
  const text = els.input.value.trim();
  const attachments = state.pending;
  if ((!text && !attachments.length) || currentThread().busy) return;
  let convId;
  try {
    convId = await ensureConversation();
  } catch (e) {
    toast(`Could not start a conversation: ${e.message}`, true);
    return;
  }
  const form = new FormData();
  form.append('kind', 'message');
  form.append('text', text);
  for (const { file } of attachments) form.append('files[]', file, file.name);
  const owner = {
    id: `local-${crypto.randomUUID()}`, role: 'owner', kind: 'message', text,
    files: attachments.map(({ file, url }) => ({ name: file.name, url })),
  };
  els.input.value = '';
  state.pending = [];
  renderPending();
  autosize();
  renderConversations();
  runTurn(convId, form, owner);
}

function sendFeedback(target, score, text) {
  const convId = state.currentId;
  const form = new FormData();
  form.append('kind', 'feedback');
  form.append('text', text);
  form.append('target_message_id', target.id);
  if (score != null) form.append('score', String(score));
  const owner = { id: `local-${crypto.randomUUID()}`, role: 'owner', kind: 'feedback', text, score, target_message_id: target.id, files: [] };
  runTurn(convId, form, owner, target);
}

async function runTurn(convId, form, owner, target = null) {
  const thread = threadOf(convId);
  const agent = { id: `local-${crypto.randomUUID()}`, role: 'agent', kind: owner.kind, text: '', files: [], streaming: true };
  thread.busy = true;
  thread.messages.push(owner, agent);
  const visible = () => state.currentId === convId;
  if (visible()) {
    state.following = true;
    appendMessage(owner);
    appendMessage(agent);
    if (target) rerender(target);
    scrollToBottom();
    updateComposer();
  }
  try {
    const res = await fetch(`/api/conversations/${encodeURIComponent(convId)}/messages`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(await errorText(res));
    const finished = await consumeTurn(res, owner, agent, visible);
    if (!finished) agent.error = 'The connection closed before the agent finished.';
  } catch (e) {
    agent.error = e.message || String(e);
  }
  agent.streaming = false;
  agent.status = agent.error == null ? 'done' : 'error';
  thread.busy = false;
  if (visible()) {
    rerender(agent);
    updateComposer();
  }
  loadConversations();
}

async function consumeTurn(res, owner, agent, visible) {
  let frame = 0;
  const paint = () => {
    frame = 0;
    const el = nodes.get(agent);
    if (visible() && el?.isConnected) paintBody(agent, el.querySelector('.md'));
  };
  for await (const ev of sseEvents(res)) {
    if (ev.type === 'ack') {
      owner.id = ev.owner_message_id;
      agent.id = ev.agent_message_id;
      for (const m of [owner, agent]) { const el = nodes.get(m); if (el) el.id = `m-${m.id}`; }
    } else if (ev.type === 'text') {
      agent.text += ev.delta;
      frame ||= requestAnimationFrame(paint);
    } else if (ev.type === 'file') {
      agent.files.push({ name: ev.name, url: ev.url });
      if (visible()) rerender(agent);
    } else if (ev.type === 'error') {
      agent.error = ev.message || 'The agent reported an error.';
      return true;
    } else if (ev.type === 'done') {
      return true;
    }
  }
  return false;
}

/* ---------- Composer ---------- */

function updateComposer() {
  const busy = currentThread().busy;
  const hasContent = els.input.value.trim() || state.pending.length;
  els.send.disabled = busy || !hasContent;
  els.hint.textContent = busy ? 'Agent is replying…' : '';
}

function autosize() {
  els.input.style.height = 'auto';
  els.input.style.height = `${els.input.scrollHeight}px`;
  updateComposer();
}

function addFiles(files) {
  for (const file of files) state.pending.push({ file, url: URL.createObjectURL(file) });
  renderPending();
  updateComposer();
}

function removePending(item) {
  state.pending = state.pending.filter((p) => p !== item);
  URL.revokeObjectURL(item.url);
  renderPending();
  updateComposer();
}

function renderPending() {
  els.pending.hidden = state.pending.length === 0;
  els.pending.replaceChildren(...state.pending.map((item) => h('div', { class: 'pending-item', title: item.file.name },
    IMAGE_EXT.test(item.file.name) || item.file.type.startsWith('image/')
      ? h('img', { src: item.url, alt: '' })
      : h('span', { class: 'file-glyph', html: icon('file') }),
    h('span', { class: 'pending-name' }, item.file.name),
    h('button', {
      class: 'icon-btn', type: 'button', 'aria-label': `Remove ${item.file.name}`, html: icon('x'),
      onclick: () => removePending(item),
    }))));
}

// On touch keyboards there is no Shift+Enter, so Enter inserts a newline and the send button sends.
const enterSends = matchMedia('(hover: hover) and (pointer: fine)');

els.input.addEventListener('input', autosize);
els.input.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229 || !enterSends.matches) return;
  e.preventDefault();
  sendMessage();
});
els.input.addEventListener('paste', (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (!files.length) return;
  e.preventDefault();
  addFiles(files);
});
els.composer.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
els.attach.addEventListener('click', () => els.fileInput.click());
els.fileInput.addEventListener('change', () => { addFiles([...els.fileInput.files]); els.fileInput.value = ''; });

let dragDepth = 0;
window.addEventListener('dragenter', (e) => {
  if (!e.dataTransfer?.types.includes('Files')) return;
  dragDepth += 1;
  els.box.classList.add('dragging');
});
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) els.box.classList.remove('dragging');
});
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  els.box.classList.remove('dragging');
  if (e.dataTransfer?.files.length) addFiles([...e.dataTransfer.files]);
});

/* ---------- Drawer ---------- */

function openDrawer() {
  els.sidebar.classList.add('open');
  els.scrim.hidden = false;
  requestAnimationFrame(() => els.scrim.classList.add('open'));
  els.menuBtn.setAttribute('aria-expanded', 'true');
  const current = els.sidebar.querySelector('[aria-current]');
  current?.scrollIntoView({ block: 'nearest' });
  (current || els.newChat).focus();
}
function closeDrawer() {
  if (!els.sidebar.classList.contains('open')) return;
  els.sidebar.classList.remove('open');
  els.scrim.classList.remove('open');
  els.scrim.hidden = true;
  els.menuBtn.setAttribute('aria-expanded', 'false');
}
els.menuBtn.addEventListener('click', openDrawer);
els.scrim.addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
els.newChat.addEventListener('click', startNewConversation);

/* ---------- Agent panel ---------- */

function renderPanelStatus() {
  const s = state.status;
  if (!s) {
    els.panelStatus.textContent = 'The host is not responding.';
  } else {
    els.panelStatus.replaceChildren(
      s.agent === 'online' ? 'Running ' : 'Offline · last seen at ',
      s.commit ? h('code', {}, shortCommit(s.commit)) : '',
      s.healthy_commit ? ` · last healthy ${shortCommit(s.healthy_commit)}` : '',
    );
  }
  els.rollbackHealthyBtn.disabled = !s?.healthy_commit || sameCommit(s.commit, s.healthy_commit);
}

function listNote(list, text, isError = false) {
  list.replaceChildren(h('li', { class: `rows-note${isError ? ' error' : ''}` }, text));
}

async function loadCommits() {
  try {
    const commits = await api('/api/agent/commits');
    if (!commits.length) return listNote(els.commitList, 'No commits yet');
    els.commitList.replaceChildren(...commits.map(commitRow));
  } catch (e) {
    listNote(els.commitList, `Could not load history: ${e.message}`, true);
  }
}

function commitRow(c) {
  const isHead = sameCommit(c.commit, state.status?.commit);
  const isHealthy = sameCommit(c.commit, state.status?.healthy_commit);
  return h('li', { class: 'row' },
    h('div', { class: 'row-main' },
      h('div', { class: 'row-title', title: c.subject }, c.subject || '(no message)'),
      h('div', { class: 'row-meta' },
        h('code', {}, shortCommit(c.commit)),
        h('span', { title: longDate(c.date) }, shortDate(c.date)),
        isHead ? h('span', { class: 'tag' }, 'Current') : null,
        isHealthy ? h('span', { class: 'tag ok' }, 'Healthy') : null)),
    isHead ? null : h('button', {
      class: 'btn small', type: 'button', onclick: (e) => rollback(e.currentTarget, c.commit, c.subject),
    }, 'Roll back'));
}

async function loadSnapshots() {
  try {
    const snaps = await api('/api/agent/snapshots');
    if (!snaps.length) return listNote(els.snapshotList, 'No snapshots yet');
    els.snapshotList.replaceChildren(...snaps.map((s) => h('li', { class: 'row' },
      h('div', { class: 'row-main' },
        h('div', { class: 'row-title', title: s.tag }, s.tag),
        h('div', { class: 'row-meta' }, h('span', {}, longDate(s.created_at)))),
      h('button', { class: 'btn small', type: 'button', onclick: (e) => restoreSnapshot(e.currentTarget, s.tag) }, 'Restore'))));
  } catch (e) {
    listNote(els.snapshotList, `Could not load snapshots: ${e.message}`, true);
  }
}

async function openPanel() {
  els.panel.showModal();
  renderPanelStatus();
  await refreshStatus();
  loadCommits();
  loadSnapshots();
}

async function runAction(button, work, success) {
  button.disabled = true;
  try {
    await work();
    toast(success);
  } catch (e) {
    toast(e.message, true);
  }
  button.disabled = false;
  await refreshStatus();
  loadCommits();
  loadSnapshots();
}

async function rollback(button, commit, subject) {
  const target = commit ? `${shortCommit(commit)} “${excerpt(subject)}”` : `the last healthy commit (${shortCommit(state.status?.healthy_commit)})`;
  const ok = await confirmAction({
    title: 'Roll back the agent?',
    body: `The agent's code is reset to ${target} and restarted. Newer commits are kept on an abandoned/ branch.`,
    action: 'Roll back',
  });
  if (!ok) return;
  runAction(button, () => postJSON('/api/agent/rollback', commit ? { commit } : {}), 'Rolled back');
}

async function restoreSnapshot(button, tag) {
  const ok = await confirmAction({
    title: 'Restore this snapshot?',
    body: `The container is recreated from ${tag}. Everything inside it that changed since then is lost.`,
    action: 'Restore',
  });
  if (ok) runAction(button, () => api(`/api/agent/snapshots/${encodeURIComponent(tag)}/restore`, { method: 'POST' }), 'Snapshot restored');
}

els.settingsBtn.addEventListener('click', openPanel);
els.pill.addEventListener('click', openPanel);
els.panel.querySelector('[data-close]').addEventListener('click', () => els.panel.close());
els.panel.addEventListener('click', (e) => { if (e.target === els.panel) els.panel.close(); });
els.restartBtn.addEventListener('click', async () => {
  const ok = await confirmAction({
    title: 'Restart the agent?',
    body: 'The agent process stops and starts again. A reply that is still streaming may be cut off.',
    action: 'Restart',
  });
  if (ok) runAction(els.restartBtn, () => api('/api/agent/restart', { method: 'POST' }), 'Restart requested');
});
els.rollbackHealthyBtn.addEventListener('click', () => rollback(els.rollbackHealthyBtn, null));
els.snapshotBtn.addEventListener('click', () => runAction(els.snapshotBtn, () => api('/api/agent/snapshots', { method: 'POST' }), 'Snapshot created'));

/* ---------- Boot ---------- */

window.addEventListener('hashchange', openRoute);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStatus(); });
setInterval(() => { if (!document.hidden) refreshStatus(); }, STATUS_POLL_MS);

refreshStatus();
loadConversations();
openRoute();
