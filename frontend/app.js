const API = '';
const state = {
  settings: { font_zoom: 100, haptic: true, timer: true, external_knowledge: false, task_detail: false, model: 'gemini-1.5-flash' },
  currentView: 'dashboard',
  currentNote: null,
  noteSaveTimer: null,
  grid: null,
  graphNetwork: null,
  graphData: null,
  quiz: null,
  selectedText: '',
};

function toast(msg, ms = 4000) {
  const c = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = 'card px-4 py-2 shadow-lg text-sm';
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => { el.remove(); }, ms);
}

function haptic() { if (state.settings.haptic && navigator.vibrate) navigator.vibrate(10); }

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
window.openModal = openModal; window.closeModal = closeModal;

function openMenu() {
  document.getElementById('slideMenu').classList.add('open');
  document.getElementById('menuBackdrop').classList.remove('hidden');
}
function closeMenu() {
  document.getElementById('slideMenu').classList.remove('open');
  document.getElementById('menuBackdrop').classList.add('hidden');
}
window.openMenu = openMenu; window.closeMenu = closeMenu;

function toggleSidebar() {
  const sb = document.getElementById('desktopSidebar');
  sb.classList.toggle('hidden');
}

function switchView(view) {
  state.currentView = view;
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  const el = document.getElementById('view-' + view);
  if (el) el.classList.remove('hidden');
  if (view === 'graph') loadGraph();
  if (view === 'learning') loadLearning();
  if (view === 'notes') loadNotes();
}

document.querySelectorAll('[data-view]').forEach(b => {
  b.addEventListener('click', () => { switchView(b.dataset.view); closeMenu(); });
});

// Swipe from edge to open menu (mobile)
let touchStartX = 0;
document.addEventListener('touchstart', (e) => { touchStartX = e.touches[0].clientX; });
document.addEventListener('touchend', (e) => {
  const dx = e.changedTouches[0].clientX - touchStartX;
  if (touchStartX < 20 && dx > 60) openMenu();
  if (dx < -60 && document.getElementById('slideMenu').classList.contains('open')) closeMenu();
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    openModal('searchModal');
    setTimeout(() => document.getElementById('globalSearchInput').focus(), 50);
  }
  if (e.key === 'Escape') {
    document.querySelectorAll('.overlay.open').forEach(o => o.classList.remove('open'));
  }
});

// Gridstack
function initGrid() {
  if (typeof GridStack === 'undefined') return;
  const saved = localStorage.getItem('dashboardLayout');
  try {
    const grid = GridStack.init({
      cellHeight: 80,
      margin: 8,
      float: false,
      animate: true,
      alwaysShowResizeHandle: true,
    }, '#dashboardGrid');
    state.grid = grid;
    if (saved) {
      try { grid.load(JSON.parse(saved)); } catch (e) { console.error('load layout failed', e); }
    } else {
      grid.addWidget({ x: 0, y: 0, w: 12, h: 2, content: '<div class="font-semibold">Deadlines & Countdowns</div><div id="deadlinesBox" class="text-sm mt-1"></div>' });
      grid.addWidget({ x: 0, y: 2, w: 6, h: 6, content: '<div class="font-semibold">Learning Assistant</div><div class="drop-zone mt-2 p-6 text-center" id="learnDropZone">Drop files or click to ingest</div><button class="btn-primary mt-2 px-3 py-1 rounded text-sm" onclick="triggerUpload()">Upload</button>' });
      grid.addWidget({ x: 6, y: 2, w: 6, h: 6, content: '<div class="font-semibold">Notes Stream</div><div id="notesStream" class="text-sm mt-1 space-y-1"></div><div class="mt-2"><input id="dashSearch" placeholder="Search..." class="w-full px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-soft)] text-sm" /></div>' });
    }
    grid.on('change', () => {
      try { localStorage.setItem('dashboardLayout', JSON.stringify(grid.save())); } catch (e) {}
    });
    grid.on('resize', (event, items) => {
      try { grid.compact(); } catch (e) {}
    });
    grid.on('resizestop', (event, items) => {
      try { grid.compact(); localStorage.setItem('dashboardLayout', JSON.stringify(grid.save())); } catch (e) {}
    });
    grid.on('dragstop', (event, items) => {
      try { grid.compact(); localStorage.setItem('dashboardLayout', JSON.stringify(grid.save())); } catch (e) {}
    });
    const dz = document.getElementById('learnDropZone');
    if (dz) {
      dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
      dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
      dz.addEventListener('drop', (e) => { e.preventDefault(); dz.classList.remove('dragover'); handleFiles(e.dataTransfer.files); });
      dz.addEventListener('click', () => { const i = document.createElement('input'); i.type='file'; i.multiple=true; i.onchange=()=>handleFiles(i.files); i.click(); });
    }
    const fab = document.getElementById('fabBtn');
    if (fab) fab.addEventListener('click', () => { haptic(); triggerUpload(); });
    console.log('GridStack initialized');
  } catch (e) {
    console.error('GridStack init failed', e);
  }
}
function resetLayout() {
  try {
    if (state.grid) {
      state.grid.destroy(false);
      state.grid = null;
    }
    localStorage.removeItem('dashboardLayout');
    const container = document.getElementById('dashboardGrid');
    if (container) container.innerHTML = '';
    initGrid();
    toast('Layout reset');
  } catch (e) {
    console.error('reset layout failed', e);
    toast('Reset failed');
  }
}

window.resetLayout = resetLayout;

window.addEventListener('load', initGrid);

function triggerUpload() {
  const inp = document.createElement('input');
  inp.type = 'file'; inp.multiple = true;
  inp.onchange = () => handleFiles(inp.files);
  inp.click();
}
window.triggerUpload = triggerUpload;

// ------------------------------------------------------------------
// Chunked upload manager
// ------------------------------------------------------------------
const CHUNK_SIZE = 18 * 1024 * 1024;
async function handleFiles(files) {
  for (const file of files) await uploadFile(file);
}
async function uploadFile(file) {
  const uploadId = crypto.randomUUID();
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE) || 1;
  let duplicateInfo = null;
  for (let i = 0; i < totalChunks; i++) {
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const chunk = file.slice(start, end);
    const form = new FormData();
    form.append('upload_id', uploadId);
    form.append('chunk_index', i);
    form.append('total_chunks', totalChunks);
    form.append('file_name', file.name);
    form.append('file', chunk, file.name);
    const res = await fetch('/api/notes/upload/chunk', { method: 'POST', body: form });
    const data = await res.json();
    showProgress(file.name, i + 1, totalChunks);
    if (data.status === 'duplicate') duplicateInfo = data;
    if (data.status === 'processed') {
      toast('Uploaded: ' + (data.title || file.name));
      if (data.markdown) insertMarkdown(data.markdown);
    }
  }
  if (duplicateInfo) {
    const action = await showDuplicateModal(duplicateInfo);
    if (action && action !== 'cancel') {
      const r = await fetch('/api/notes/upload/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ upload_id: duplicateInfo.upload_id, action }) });
      const d = await r.json();
      toast('Resolve: ' + d.status);
      if (d.note_id) loadNotes();
    }
  }
  hideProgress(file.name);
}
function showProgress(name, done, total) {
  let bar = document.getElementById('progress-' + name);
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'progress-' + name;
    bar.className = 'card p-2 text-xs fixed top-16 right-4 z-50 w-64';
    bar.innerHTML = `<div>${name}</div><div class="w-full bg-[var(--border)] rounded h-2 mt-1"><div class="bg-[var(--primary)] h-2 rounded" style="width:0%"></div></div>`;
    document.body.appendChild(bar);
  }
  const pct = Math.round((done / total) * 100);
  bar.querySelector('div > div').style.width = pct + '%';
  bar.firstChild.textContent = name + ' ' + pct + '%';
}
function hideProgress(name) {
  const bar = document.getElementById('progress-' + name);
  if (bar) setTimeout(() => bar.remove(), 1500);
}
function showDuplicateModal(info) {
  return new Promise((resolve) => {
    openModal('duplicateModal');
    document.getElementById('dupCancelBtn').onclick = () => { closeModal('duplicateModal'); resolve('cancel'); };
    document.getElementById('dupSkipBtn').onclick = () => { closeModal('duplicateModal'); resolve('skip'); };
    document.getElementById('dupOverwriteBtn').onclick = () => { closeModal('duplicateModal'); resolve('overwrite'); };
  });
}
document.addEventListener('DOMContentLoaded', () => {
  const dz = document.getElementById('learnDropZone');
  if (dz) {
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => { e.preventDefault(); dz.classList.remove('dragover'); handleFiles(e.dataTransfer.files); });
    dz.addEventListener('click', () => { const i = document.createElement('input'); i.type='file'; i.multiple=true; i.onchange=()=>handleFiles(i.files); i.click(); });
  }
  const fab = document.getElementById('fabBtn');
  if (fab) fab.addEventListener('click', () => { haptic(); triggerUpload(); });
});
function triggerUpload() {
  const inp = document.createElement('input');
  inp.type = 'file'; inp.multiple = true;
  inp.onchange = () => handleFiles(inp.files);
  inp.click();
}
window.triggerUpload = triggerUpload;
// ------------------------------------------------------------------
// Learning
// ------------------------------------------------------------------
async function loadLearning() {
  const r = await fetch('/api/learning/subjects');
  const subjects = await r.json();
  const el = document.getElementById('subjectsList');
  if (el) el.innerHTML = subjects.map(s => `<div class="p-2 rounded card">${escapeHtml(s.name)}</div>`).join('') || '<div class="text-[var(--text-muted)]">No subjects</div>';
  const tr = await fetch('/api/learning/timeline');
  const t = await tr.json();
  const tl = document.getElementById('timelineView');
  if (tl) tl.innerHTML = Object.keys(t.by_date || {}).map(d => `<div><strong>${d}</strong>: ${(t.by_date[d]||[]).map(l=>escapeHtml(l.title)).join(', ')}</div>`).join('') || '<div class="text-[var(--text-muted)]">No lessons</div>';
  const fr = await fetch('/api/learning/flashcards/due');
  const cards = await fr.json();
  const fa = document.getElementById('flashcardArea');
  if (fa) {
    if (!cards.length) fa.innerHTML = '<div class="text-[var(--text-muted)]">No cards due</div>';
    else {
      const c = cards[0];
      fa.innerHTML = `<div class="card p-3"><div class="font-semibold">${escapeHtml(c.front_text)}</div><div class="mt-2 text-[var(--text-muted)]">${escapeHtml(c.back_text)}</div>
        <div class="flex gap-2 mt-3">
          <button class="px-3 py-1 rounded bg-[#F5F5F5] text-[#1a1a1a] border border-[#ECECEC]" onclick="reviewCard('${c.id}','again')">Nezināju</button>
          <button class="px-3 py-1 rounded bg-[#F5F5F5] text-[#1a1a1a] border border-[#ECECEC]" onclick="reviewCard('${c.id}','hard')">Bija grūti</button>
          <button class="px-3 py-1 rounded bg-[#76C457] text-[#1a1a1a]" onclick="reviewCard('${c.id}','easy')">Viegli</button>
        </div></div>`;
    }
  }
  const gr = await fetch('/api/learning/knowledge-gaps?active_only=true');
  const gaps = await gr.json();
  const ga = document.getElementById('gapsArea');
  if (ga) ga.innerHTML = gaps.map(g => `<div class="p-2 rounded error-tint mb-1">${escapeHtml(g.concept_name)} (${g.consecutive_mistakes} mistakes)</div>`).join('') || '<div class="text-[var(--text-muted)]">No gaps</div>';
}
async function reviewCard(id, rating) {
  await fetch('/api/learning/flashcards/' + id + '/review', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rating }) });
  haptic(); loadLearning();
}
document.getElementById('addSubjectBtn')?.addEventListener('click', async () => {
  const name = prompt('Subject name');
  if (!name) return;
  await fetch('/api/learning/subjects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
  loadLearning();
});

// ------------------------------------------------------------------
// Notes editor auto-save
// ------------------------------------------------------------------
const noteTitle = document.getElementById('noteTitle');
const noteContent = document.getElementById('noteContent');
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
async function saveNote() {
  if (!state.currentNote) {
    const r = await fetch('/api/notes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: noteTitle.value, content_raw: noteContent.value }) });
    const d = await r.json();
    state.currentNote = d.id;
    toast('Note created');
  } else {
    await fetch('/api/notes/' + state.currentNote, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: noteTitle.value, content_raw: noteContent.value }) });
  }
  updateOutline();
}
const debouncedSave = debounce(saveNote, 400);
if (noteContent) noteContent.addEventListener('input', () => { debouncedSave(); });
if (noteTitle) noteTitle.addEventListener('input', () => { debouncedSave(); });

// Clipboard image paste
document.addEventListener('paste', async (e) => {
  const items = e.clipboardData?.items || [];
  for (const it of items) {
    if (it.type.startsWith('image/')) {
      e.preventDefault();
      const file = it.getAsFile();
      const form = new FormData();
      form.append('file', file, 'pasted.png');
      const r = await fetch('/api/notes/paste-image', { method: 'POST', body: form });
      const d = await r.json();
      if (d.markdown) insertMarkdown(d.markdown);
      haptic();
    }
  }
});
function insertMarkdown(md) {
  if (!noteContent) return;
  const pos = noteContent.selectionStart || noteContent.value.length;
  const v = noteContent.value;
  noteContent.value = v.slice(0, pos) + md + v.slice(pos);
  noteContent.selectionStart = noteContent.selectionEnd = pos + md.length;
  debouncedSave();
}
function updateOutline() {
  const content = noteContent ? noteContent.value : '';
  const items = [];
  content.split('\n').forEach(line => {
    const m = line.match(/^(#{1,6})\s+(.+)$/);
    if (m) items.push({ level: m[1].length, text: m[2] });
  });
  const ul = document.getElementById('noteOutline');
  if (ul) ul.innerHTML = items.map(i => `<li style="padding-left:${(i.level-1)*12}px">${i.text}</li>`).join('');
}
async function loadNotes() {
  const r = await fetch('/api/notes');
  const notes = await r.json();
  const stream = document.getElementById('notesStream');
  if (stream) stream.innerHTML = notes.map(n => `<div class="p-2 rounded hover:bg-[var(--primary-soft)] cursor-pointer" onclick="openNote('${n.id}')">${escapeHtml(n.title)}</div>`).join('');
}
async function openNote(id) {
  const r = await fetch('/api/notes/' + id);
  const n = await r.json();
  state.currentNote = id;
  if (noteTitle) noteTitle.value = n.title || '';
  if (noteContent) noteContent.value = n.content_raw || '';
  switchView('notes');
  updateOutline();
  const tagsEl = document.getElementById('noteTags');
  if (tagsEl) tagsEl.innerHTML = (n.tags || []).map(t => `<span class="px-2 py-0.5 rounded bg-[var(--primary-soft)] text-xs">${escapeHtml(t)}</span>`).join('');
  const tasksEl = document.getElementById('noteTasks');
  if (tasksEl) tasksEl.innerHTML = (n.tasks || []).map(t => `<li>${escapeHtml(t.title)} <span class="text-[var(--text-muted)]">(${t.status})</span></li>`).join('');
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

const searchInput = document.getElementById('noteSearchInput');
if (searchInput) searchInput.addEventListener('input', debounce(async () => {
  const q = searchInput.value.trim();
  if (!q) { loadNotes(); return; }
  const r = await fetch('/api/notes/search?q=' + encodeURIComponent(q));
  const res = await r.json();
  const stream = document.getElementById('notesStream');
  if (stream) stream.innerHTML = res.map(n => `<div class="p-2 rounded hover:bg-[var(--primary-soft)] cursor-pointer" onclick="openNote('${n.id}')">${escapeHtml(n.title)}</div>`).join('');
}, 300));

const globalSearchInput = document.getElementById('globalSearchInput');
if (globalSearchInput) globalSearchInput.addEventListener('input', debounce(async () => {
  const q = globalSearchInput.value.trim();
  if (!q) { document.getElementById('searchResults').innerHTML = ''; return; }
  const r = await fetch('/api/notes/search?q=' + encodeURIComponent(q));
  const res = await r.json();
  document.getElementById('searchResults').innerHTML = res.map(n => `<div class="p-2 rounded hover:bg-[var(--primary-soft)] cursor-pointer" onclick="closeModal('searchModal');openNote('${n.id}')">${escapeHtml(n.title)}</div>`).join('');
}, 300));
window.openNote = openNote;

// ------------------------------------------------------------------
// Quiz client
// ------------------------------------------------------------------
async function startQuiz(payload) {
  const r = await fetch('/api/learning/quizzes/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  const q = await r.json();
  state.quiz = q;
  renderQuiz();
  openModal('quizModal');
}
function renderQuiz() {
  const q = state.quiz;
  if (!q) return;
  document.getElementById('quizTitle').textContent = q.title;
  const bs = q.bubble_states || {};
  document.getElementById('quizStepper').innerHTML = q.questions.map((qq, idx) => {
    const b = bs[qq.id] || 4;
    const cls = b === 1 ? 'green' : b === 2 ? 'yellow' : b === 3 ? 'red' : 'gray';
    return `<span class="bubble ${cls}">${idx + 1}</span>`;
  }).join('');
  if (q.time_limit_seconds) document.getElementById('quizTimer').textContent = 'Time: ' + q.time_limit_seconds + 's';
  let ci = q.questions.findIndex(qq => (q.bubble_states || {})[qq.id] !== 1);
  if (ci < 0) ci = 0;
  q.currentIndex = ci;
  renderQuestion(q.questions[ci]);
}
function renderQuestion(qq) {
  const el = document.getElementById('quizQuestion');
  let html = `<div class="font-semibold mb-2">${escapeHtml(qq.text)}</div>`;
  if (qq.type === 'single_choice' || qq.type === 'multiple_choice') {
    const inputType = qq.type === 'multiple_choice' ? 'checkbox' : 'radio';
    html += (qq.options || []).map((opt, idx) => `<label class="flex items-center gap-2 p-2 rounded border border-[var(--border)] mb-1"><input type="${inputType}" name="qopt" value="${idx}" /> ${escapeHtml(opt)}</label>`).join('');
  } else {
    html += `<textarea id="openAnswer" class="w-full border border-[var(--border)] rounded p-2" rows="3"></textarea>`;
  }
  el.innerHTML = html;
  document.getElementById('quizFeedback').innerHTML = '';
}
async function submitAnswer() {
  const q = state.quiz;
  const qq = q.questions[q.currentIndex];
  let ua;
  if (qq.type === 'single_choice' || qq.type === 'multiple_choice') {
    const checked = [...document.querySelectorAll('input[name="qopt"]:checked')].map(i => i.value);
    ua = qq.type === 'single_choice' ? checked[0] : checked;
  } else {
    ua = document.getElementById('openAnswer').value;
  }
  const r = await fetch('/api/learning/quizzes/' + q.quiz_id + '/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question_id: qq.id, user_answer: ua, math_mode: 'value' }) });
  const fb = await r.json();
  if (!fb.correct) {
    document.querySelectorAll('input[name="qopt"]:checked').forEach(i => { i.parentElement.classList.add('error-tint'); });
  }
  const sol = fb.solution || {};
  let feedbackHtml = `<div class="font-semibold">${escapeHtml(fb.message)}</div>`;
  if (!fb.correct) {
    feedbackHtml += `<div class="hint-box mt-2"><strong>Padoms:</strong> ${escapeHtml(fb.hint || '')}</div>`;
  }
  feedbackHtml += `<div class="mt-3"><strong>Risinājuma soļi:</strong><div class="mt-1">${escapeHtml(sol.steps || '')}</div></div>`;
  if (sol.theory) feedbackHtml += `<div class="mt-2 p-2 rounded bg-[var(--primary-soft)]"><span class="font-semibold">Teorija (✏️):</span> ${escapeHtml(sol.theory)}</div>`;
  if (sol.analogy) feedbackHtml += `<div class="mt-2 text-sm text-[var(--text-muted)]"><strong>Analogy:</strong> ${escapeHtml(sol.analogy)}</div>`;
  if (sol.note_snippet) feedbackHtml += `<div class="mt-2 text-xs text-[var(--text-muted)]">Source: ${escapeHtml(sol.note_snippet)}</div>`;
  document.getElementById('quizFeedback').innerHTML = feedbackHtml;
  const bs = await (await fetch('/api/learning/quizzes/' + q.quiz_id + '/bubbles')).json();
  q.bubble_states = bs;
  renderQuizStepper();
  haptic();
}
function renderQuizStepper() {
  const q = state.quiz;
  const bs = q.bubble_states || {};
  document.getElementById('quizStepper').innerHTML = q.questions.map((qq, idx) => {
    const b = bs[qq.id] || 4;
    const cls = b === 1 ? 'green' : b === 2 ? 'yellow' : b === 3 ? 'red' : 'gray';
    return `<span class="bubble ${cls}">${idx + 1}</span>`;
  }).join('');
}
document.getElementById('retryBtn')?.addEventListener('click', () => {
  const q = state.quiz;
  renderQuestion(q.questions[q.currentIndex]);
});
document.getElementById('nextBtn')?.addEventListener('click', async () => {
  const q = state.quiz;
  if (q.currentIndex >= q.questions.length - 1) {
    await fetch('/api/learning/quizzes/' + q.quiz_id + '/close', { method: 'POST' });
    closeModal('quizModal');
    toast('Quiz finished');
    return;
  }
  q.currentIndex = q.currentIndex + 1;
  renderQuestion(q.questions[q.currentIndex]);
  renderQuizStepper();
});
// submit button injection
const quizQuestionEl = document.getElementById('quizQuestion');
if (quizQuestionEl) {
  const submitBtn = document.createElement('button');
  submitBtn.className = 'btn-primary px-4 py-2 rounded mt-3';
  submitBtn.textContent = 'Submit';
  submitBtn.onclick = submitAnswer;
  quizQuestionEl.parentElement.insertBefore(submitBtn, document.getElementById('quizFeedback'));
}
window.startQuiz = startQuiz;
window.submitAnswer = submitAnswer;

// ------------------------------------------------------------------
// Graph (Vis.js)
// ------------------------------------------------------------------
async function loadGraph() {
  const threshold = (document.getElementById('thresholdSlider')?.value || 72) / 100;
  const r = await fetch('/api/notes/graph?threshold=' + threshold);
  const data = await r.json();
  state.graphData = data;
  renderGraph();
}
function renderGraph() {
  const data = state.graphData;
  if (!data || typeof vis === 'undefined') return;
  const container = document.getElementById('graphCanvas');
  const nodeSize = parseInt(document.getElementById('nodeSizeSlider')?.value || 10);
  const linkThick = parseInt(document.getElementById('linkThickSlider')?.value || 2);
  const arrows = document.getElementById('arrowsToggle')?.checked;
  const nodes = new vis.DataSet(data.nodes.map(n => ({
    id: n.id, label: n.label, color: n.color, size: Math.max(5, n.size + (nodeSize - 10)),
    title: n.metadata?.summary || n.label,
  })));
  const edges = new vis.DataSet(data.edges.map(e => ({
    from: e.source, to: e.target, width: linkThick, arrows: arrows ? 'to' : '',
    color: { opacity: 0.4 },
  })));
  const options = {
    physics: {
      enabled: true,
      barnesHut: { gravitationalConstant: -(document.getElementById('repelForceSlider')?.value || 50) * 10, centralGravity: (document.getElementById('centerForceSlider')?.value || 30) / 100, springLength: 120, springConstant: (document.getElementById('linkForceSlider')?.value || 40) / 1000 },
    },
    interaction: { hover: true },
  };
  if (state.graphNetwork) state.graphNetwork.destroy();
  state.graphNetwork = new vis.Network(container, { nodes, edges }, options);
  state.graphNetwork.on('click', (params) => {
    if (params.nodes.length) {
      const nid = params.nodes[0];
      const node = data.nodes.find(n => n.id === nid);
      openGraphDrawer(node);
    }
  });
  setTimeout(() => { if (state.graphNetwork) state.graphNetwork.setOptions({ physics: { enabled: false } }); }, 3000);
}
function openGraphDrawer(node) {
  const body = document.getElementById('graphDrawerBody');
  if (!node) { body.innerHTML = ''; return; }
  body.innerHTML = `<div class="font-semibold">${escapeHtml(node.label)}</div>
    <div class="text-xs text-[var(--text-muted)] mt-1">${escapeHtml(node.type)}</div>
    <div class="mt-2">${escapeHtml(node.metadata?.summary || '')}</div>
    <div class="mt-2 text-xs">Tags: ${(node.metadata?.tags || []).join(', ')}</div>`;
  document.getElementById('graphDrawer').classList.add('open');
}
function closeGraphDrawer() { document.getElementById('graphDrawer').classList.remove('open'); }
window.closeGraphDrawer = closeGraphDrawer;
['nodeSizeSlider','linkThickSlider','centerForceSlider','repelForceSlider','linkForceSlider','thresholdSlider'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('change', () => { if (id === 'thresholdSlider') loadGraph(); else renderGraph(); });
});
document.getElementById('arrowsToggle')?.addEventListener('change', renderGraph);

// ------------------------------------------------------------------
// Settings
// ------------------------------------------------------------------
async function loadSettings() {
  const r = await fetch('/api/notes/settings');
  const s = await r.json();
  state.settings = Object.assign(state.settings, s || {});
  const lr = await fetch('/api/learning/settings');
  const ls = await lr.json();
  state.settings.external_knowledge = ls.external_knowledge;
  state.settings.timer = ls.timer_enabled;
  state.settings.task_detail = ls.task_description_style === 'detailed';
  applyFontZoom(state.settings.font_zoom || 100);
  const ms = document.getElementById('modelSelect');
  if (ms) ms.value = state.settings.model || 'gemini-1.5-flash';
}
function applyFontZoom(z) {
  document.documentElement.style.fontSize = z + '%';
}
document.getElementById('saveSettingsBtn')?.addEventListener('click', async () => {
  const payload = { task_description_style: document.getElementById('taskDetailToggle').checked ? 'detailed' : 'short' };
  await fetch('/api/notes/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  await fetch('/api/learning/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
    external_knowledge: document.getElementById('externalKnowledgeToggle').checked,
    timer_enabled: document.getElementById('timerToggle').checked,
    task_description_style: document.getElementById('taskDetailToggle').checked ? 'detailed' : 'short',
  }) });
  state.settings.haptic = document.getElementById('hapticToggle').checked;
  state.settings.font_zoom = parseInt(document.getElementById('fontZoomSlider').value);
  applyFontZoom(state.settings.font_zoom);
  toast('Settings saved');
});
document.getElementById('fontZoomSlider')?.addEventListener('input', (e) => applyFontZoom(e.target.value));

// Explain simpler: text selection
document.addEventListener('mouseup', async () => {
  const sel = window.getSelection();
  const text = sel?.toString().trim();
  if (!text || text.length < 3) return;
  state.selectedText = text;
  const ctx = (document.getElementById('noteContent')?.value || '').slice(0, 2000);
  try {
    const r = await fetch('/api/learning/explain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, context: ctx }) });
    const d = await r.json();
    toast('Explain: ' + (d.explanation || '').slice(0, 120));
  } catch (e) {}
});

// Service worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// Init
window.addEventListener('load', () => {
  initGrid();
  loadSettings();
  loadNotes();
  loadLearning();
  switchView('dashboard');
});

