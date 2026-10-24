// ── State ──────────────────────────────────────────────────
let questions        = [];
let sections         = [];
let accentColor      = '#14140f';
let bgColor          = '#fafaf7';
let qFont            = "'Inter',sans-serif";
let headerFont = "'Instrument Serif',serif"; 
let previewTab       = null;
let headerImageBase64= null;

// Button state — tracked so saveSurvey and preview can include them
let btnColor    = '#14140f';
let btnRadius   = '6';
let btnPadding  = '10px 22px';
let btnFontSize = '14px';

// Undo / redo
let undoStack = [];
let undoIdx   = -1;

// ── Type definitions ───────────────────────────────────────
const TYPE_LABELS = {
  short:'Short answer', open:'Paragraph',
  mcq:'Multiple choice', checkbox:'Checkboxes', dropdown:'Dropdown',
  date:'Date', file:'File upload', rating:'Rating',
};
const TYPE_ICONS = {
  short:   svi('M3 12h11'),
  open:    svi('M3 6h18M3 12h18M3 18h10'),
  mcq:     `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5" fill="currentColor"/></svg>`,
  checkbox:`<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><polyline points="8,12 11,15 16,9"/></svg>`,
  dropdown:svi('M6 9l6 6 6-6'),
  date:    `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/></svg>`,
  file:    `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17,8 12,3 7,8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`,
  rating:  `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12,2 15,9 22,9 17,14 19,21 12,17 5,21 7,14 2,9 9,9"/></svg>`,
};
function svi(d){
  return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="${d}" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
const OPTION_TYPES = ['mcq','checkbox','dropdown'];
const LIKERT_LABELS = ['Strongly Disagree','Disagree','Neutral','Agree','Strongly Agree'];
const CONSTRUCT_LABELS = {
  peou:'Perceived Ease of Use', pu:'Perceived Usefulness',
  joy:'Joy', curiosity:'Curiosity', control:'Control',
  fi:'Focused Immersion', biu:'Behavioural Intention to Use',
};

// ── Init ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

  // ── Edit mode: restore saved questions from database ──
  if (window.PREFILL && window.PREFILL.questions && window.PREFILL.questions.length > 0){

    // Restore title / description
    const titleEl = document.getElementById('formTitle');
    const descEl  = document.getElementById('formDesc');
    if (titleEl) titleEl.value = window.PREFILL.title     || '';
    if (descEl)  descEl.value  = window.PREFILL.description || '';

    // Restore theme colours
    accentColor = window.PREFILL.accent    || accentColor;
    bgColor     = window.PREFILL.bg        || bgColor;
    btnColor    = window.PREFILL.btnColor  || btnColor;
    btnRadius   = window.PREFILL.btnRadius || btnRadius;

    // Apply accent colour visually
    applyAccent(accentColor);

    // Rebuild sections from question data
    const sectionTitles = [];
    window.PREFILL.questions.forEach(q => {
      if (q.sectionTitle && !sectionTitles.includes(q.sectionTitle)){
        sectionTitles.push(q.sectionTitle);
      }
    });

    if (sectionTitles.length > 0){
      sections = sectionTitles.map((title, i) => ({
        id:    's' + i,
        title: title,
        desc:  '',
      }));
    } else {
      sections = [{ id: 's0', title: '', desc: '' }];
    }

    // Restore questions — map each back to its section
    questions = window.PREFILL.questions.map((q, i) => {
      const secIdx = sectionTitles.indexOf(q.sectionTitle);
      const sec    = secIdx >= 0 ? sections[secIdx] : sections[0];
      return {
        id: 'q' + i,
        dbId: q.id || null,
        sectionId: sec.id,
        text: q.text || '',
        code: q.code || '',
        type: q.type || 'mcq',
        construct: q.construct || null,
        required: q.required !== false,
        options: q.options || [],
        ratingMax: q.ratingMax || 5,
        ratingStyle: q.ratingStyle || 'stars',
        sectionTitle: q.sectionTitle || '',
        sectionNum: q.sectionNum || 1,
      };
    });

    renderAll();

  } else if (IS_HMSAM) {
    // ── New HMSAM survey ──
    sections  = [{ id: 's0', title: '', desc: '' }];
    questions = HMSAM_QUESTIONS.map((q, i) => ({
      ...q, id: 'q'+i, sectionId: 's0',
      sectionTitle: '', sectionNum: 1,
      ratingStyle: 'stars', ratingMax: 5,
    }));
    renderAll();
    renderConstructList();

  } else {
    // ── New standard survey ──
    sections  = [{ id: 's0', title: '', desc: '' }];
    questions = [{
      id: 'q0', sectionId: 's0',
      text: '', type: 'mcq', code: '', construct: null,
      required: true, options: ['Option 1'],
      ratingMax: 5, ratingStyle: 'stars',
      sectionTitle: '', sectionNum: 1,
    }];
    renderAll();
  }

  saveHistory();
  bindPublish();
  bindPanel();
  bindAddButtons();
  bindUndoRedo();
  bindPreview();
  document.querySelector('.topbar-tab[data-action="features"]')
    ?.addEventListener('click', saveAsDraft);

  // ── Restore theme AFTER bindPanel so UI reflects saved values ──
  if (window.PREFILL){

    // Accent colour
    if (window.PREFILL.accent){
      accentColor = window.PREFILL.accent;
      applyAccent(accentColor);
      document.querySelectorAll('.swatches[data-target="accent"] .swatch').forEach(s => {
        s.classList.toggle('active', s.dataset.color === accentColor);
      });
    }

    // Background
    if (window.PREFILL.bg){
      bgColor = window.PREFILL.bg;
      const canvas = document.querySelector('.canvas');
      if (canvas) canvas.style.background = bgColor;
      document.querySelectorAll('#bgSeg .seg-item').forEach(b => {
        b.classList.toggle('active', b.dataset.bg === bgColor);
      });
    }

    // Button colour
    if (window.PREFILL.btnColor){
      btnColor = window.PREFILL.btnColor;
      document.documentElement.style.setProperty('--btn-bg', btnColor);
      const p = document.getElementById('btnPreview');
      if (p){ p.style.background = btnColor; p.style.color = '#fff'; p.style.border = 'none'; }
      document.querySelectorAll('.swatches[data-target="btncolor"] .swatch').forEach(s => {
        s.classList.toggle('active', s.dataset.color === btnColor);
      });
    }

    // Button radius
    if (window.PREFILL.btnRadius){
      btnRadius = window.PREFILL.btnRadius;
      const p = document.getElementById('btnPreview');
      if (p) p.style.borderRadius = btnRadius + 'px';
      document.querySelectorAll('#shapeRow .seg-item').forEach(b => {
        b.classList.toggle('active', b.dataset.radius === btnRadius);
      });
    }

    // Submit label
    if (window.PREFILL.submitLabel){
      const btnTextEl = document.getElementById('btnText');
      if (btnTextEl) btnTextEl.value = window.PREFILL.submitLabel;
      const btnPreview = document.getElementById('btnPreview');
      if (btnPreview) btnPreview.textContent = window.PREFILL.submitLabel;
    }

    // Header image
    if (window.PREFILL.headerImage){
      headerImageBase64 = window.PREFILL.headerImage;
      const imgEl   = document.getElementById('headerImg');
      const imgWrap = document.getElementById('headerImgWrap');
      const imgName = document.getElementById('imgFilename');
      if (imgEl)   imgEl.src = headerImageBase64;
      if (imgWrap) imgWrap.style.display = 'block';
      if (imgName) imgName.textContent = 'Saved image';
    }

    // Title and description (also set after renderAll in case formTitle
    // element is created dynamically)
    if (window.PREFILL.title){
      const t = document.getElementById('formTitle');
      if (t) t.value = window.PREFILL.title;
    }
    if (window.PREFILL.description){
      const d = document.getElementById('formDesc');
      if (d) d.value = window.PREFILL.description;
    }

    // Question font
    if (window.PREFILL.qFont){
      qFont = window.PREFILL.qFont;
      document.querySelectorAll('.q-title-input').forEach(el => {
        el.style.fontFamily = qFont;
      });
      document.querySelectorAll('#qFontList .font-row').forEach(r => {
        r.classList.toggle('active', r.dataset.font === qFont);
      });
    }

    // Header font
    if (window.PREFILL.headerFont){
      headerFont = window.PREFILL.headerFont;
      const ft = document.getElementById('formTitle');
      if (ft) ft.style.fontFamily = headerFont;
      document.querySelectorAll('#headerFontList .font-row').forEach(r => {
        r.classList.toggle('active', r.dataset.font === headerFont);
      });
    }
  }

});

// ── History ────────────────────────────────────────────────
function saveHistory(){
  window.markBuilderDirty?.(); 
  const snap = JSON.stringify({ questions, sections });
  undoStack = undoStack.slice(0, undoIdx + 1);
  undoStack.push(snap);
  undoIdx = undoStack.length - 1;
  updateUndoRedoBtns();
}
function updateUndoRedoBtns(){
  const u = document.getElementById('btnUndo');
  const r = document.getElementById('btnRedo');
  if (u) u.disabled = undoIdx <= 0;
  if (r) r.disabled = undoIdx >= undoStack.length - 1;
}
function bindUndoRedo(){
  document.getElementById('btnUndo')?.addEventListener('click', () => {
    if (undoIdx <= 0) return;
    undoIdx--;
    const snap = JSON.parse(undoStack[undoIdx]);
    questions = snap.questions; sections = snap.sections;
    renderAll(); updateUndoRedoBtns();
  });
  document.getElementById('btnRedo')?.addEventListener('click', () => {
    if (undoIdx >= undoStack.length - 1) return;
    undoIdx++;
    const snap = JSON.parse(undoStack[undoIdx]);
    questions = snap.questions; sections = snap.sections;
    renderAll(); updateUndoRedoBtns();
  });
}

// ── Preview ────────────────────────────────────────────────
function bindPreview(){
  document.getElementById('btnPreviewForm')?.addEventListener('click', () => {
    // Sync options from DOM
    document.querySelectorAll('.q-row').forEach(row => {
      const idx = parseInt(row.dataset.idx);
      if (isNaN(idx)) return;
      const q = questions[idx];
      if (!q) return;
      if (OPTION_TYPES.includes(q.type)){
        q.options = [...row.querySelectorAll('.opt-text')]
        .map(i => i.value.trim())
        .filter(Boolean);
      }
      if (q.type === 'rating'){
        const inp = row.querySelector('.rating-max-input');
        if (inp) q.ratingMax = parseInt(inp.value) || 5;
      }
    });

    // Attach section info to questions before serialising
    sections.forEach((sec, si) => {
      questions.forEach(q => {
        if (q.sectionId === sec.id){
          q.sectionTitle = sec.title || '';
          q.sectionNum   = si + 1;
        }
      });
    });

    const previewData = {
      title:       document.getElementById('formTitle').value.trim() || 'Untitled Survey',
      description: document.getElementById('formDesc').value.trim(),
      accent:      accentColor,
      bg:          bgColor,
      submitLabel: document.getElementById('btnText')?.value || 'Submit',
      headerImage: headerImageBase64,
      sections:    sections,
      questions:   questions,
      // Button styling — needed so preview renders correct button
      btnColor:    btnColor,
      btnRadius:   btnRadius,
      btnPadding:  btnPadding,
      btnFontSize: btnFontSize,
    };
    sessionStorage.setItem('feedora_preview', JSON.stringify(previewData));

    if (previewTab && !previewTab.closed) {
      previewTab.location.reload();
      previewTab.focus();
    } else {
      previewTab = window.open('/surveys/preview/', '_blank');
    }
  });
}

// ── Render all ─────────────────────────────────────────────
function renderAll(){
  const container = document.getElementById('sectionsContainer');
  container.innerHTML = '';

  sections.forEach((sec, si) => {
    const block = document.createElement('div');
    block.className = 'section-block';
    block.dataset.secId = sec.id;

    // Show section card for ALL sections when there are 2+
    if (sections.length > 1) {
      block.appendChild(buildSectionCard(sec, si));
    }

    const secQs = questions.filter(q => q.sectionId === sec.id);
    secQs.forEach(q => {
      const idx = questions.indexOf(q);
      block.appendChild(buildCard(q, idx));
    });

    container.appendChild(block);
  });
}

// ── Section card ───────────────────────────────────────────
function buildSectionCard(sec, si){
  const div = document.createElement('div');
  div.className = 'section-header-card';
  div.innerHTML = `
    <div class="section-label">Section ${si + 1} of ${sections.length}</div>
    <input class="section-title-input" value="${esc(sec.title)}" placeholder="Untitled Section">
    <textarea class="section-desc-input" placeholder="Description (optional)" rows="1">${esc(sec.desc)}</textarea>
    <div class="section-foot">
      <button class="section-del-btn">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3,6 5,6 21,6"/>
          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
        </svg>
        Delete section
      </button>
    </div>`;

  div.querySelector('.section-title-input').addEventListener('input', e => {
    sec.title = e.target.value;
    // Propagate title to questions in this section
    questions.forEach(q => {
      if (q.sectionId === sec.id) q.sectionTitle = sec.title;
    });
  });
  div.querySelector('.section-desc-input').addEventListener('input', e => {
    sec.desc = e.target.value;
  });
  div.querySelector('.section-del-btn').addEventListener('click', () => {
    if (sections.length <= 1) return;
    const prevSec = sections[si - 1] || sections[0];
    questions.forEach(q => {
      if (q.sectionId === sec.id){
        q.sectionId    = prevSec.id;
        q.sectionTitle = prevSec.title || '';
        q.sectionNum   = sections.indexOf(prevSec) + 1;
      }
    });
    sections.splice(si, 1);
    saveHistory(); renderAll();
  });

  return div;
}

// ── Build question card ────────────────────────────────────
function buildCard(q, idx){
  const row = document.createElement('div');
  row.className = 'q-row';
  row.dataset.idx = idx;

  const typeSel = IS_HMSAM ? '' : buildTypeSel(q.type);
  const badge   = q.construct
    ? `<span class="construct-badge">${CONSTRUCT_LABELS[q.construct]||q.construct}</span>` : '';
  const code    = q.code
    ? `<span class="q-code-lbl">${esc(q.code)}</span>` : '';

  row.innerHTML = `
    <div class="q-card">
      <div class="q-card-top">
        <div class="q-title-wrap">
          ${code}
          <input class="q-title-input" value="${esc(q.text)}"
                 placeholder="Question" style="font-family:${qFont}">
          ${badge}
        </div>
        ${typeSel}
      </div>
      <div class="q-card-body">${buildBody(q)}</div>
      <div class="q-card-foot">
        ${!IS_HMSAM ? `
          <button class="q-icon-btn q-dup" title="Duplicate">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2"/>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
          </button>
          <button class="q-icon-btn q-del" title="Delete">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3,6 5,6 21,6"/>
              <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
            </svg>
          </button>
          <div class="foot-div"></div>
        ` : ''}
        <div class="q-req-toggle">
          <span>Required</span>
          <div class="toggle ${q.required !== false ? 'on' : ''}"></div>
        </div>
      </div>
    </div>`;

  row.querySelector('.q-title-input').addEventListener('input', e => {
    questions[idx].text = e.target.value;
  });
  row.querySelector('.toggle').addEventListener('click', function(e){
    e.stopPropagation();
    this.classList.toggle('on');
    questions[idx].required = this.classList.contains('on');
    saveHistory();
  });
  row.querySelector('.q-del')?.addEventListener('click', () => {
    questions.splice(idx, 1); saveHistory(); renderAll();
  });
  row.querySelector('.q-dup')?.addEventListener('click', () => {
    const c = JSON.parse(JSON.stringify(questions[idx]));
    c.id = 'q'+Date.now(); c.code = '';
    questions.splice(idx+1, 0, c); saveHistory(); renderAll();
  });

  if (!IS_HMSAM) bindTypeSel(row, idx);
  bindOptEvents(row, idx);
  return row;
}

// ── Body by type ───────────────────────────────────────────
function buildBody(q){
  switch(q.type){
    case 'short': return `<div class="text-preview-short">Short answer text</div>`;
    case 'open':  return `<div class="text-preview-long">Long answer text</div>`;
    case 'mcq':      return buildOpts(q,'radio');
    case 'checkbox': return buildOpts(q,'checkbox');
    case 'dropdown': return buildOpts(q,'dropdown');
    case 'rating':   return buildRatingEdit(q);
    case 'date':
      return `<div class="field-preview">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#a3a39a" stroke-width="2">
          <rect x="3" y="4" width="18" height="18" rx="2"/>
          <line x1="3" y1="9" x2="21" y2="9"/>
          <line x1="8" y1="2" x2="8" y2="6"/>
          <line x1="16" y1="2" x2="16" y2="6"/>
        </svg>Month / Day / Year
      </div>`;
    case 'file':
      return `<div class="file-preview">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#a3a39a" stroke-width="1.5">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="17,8 12,3 7,8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>Add file
      </div>`;
    default: return '';
  }
}

// ── Rating edit ────────────────────────────────────────────
function buildRatingEdit(q){
  const max     = q.ratingMax   || 5;
  const subtype = q.ratingStyle || 'stars';

  const subtypeSel = `
    <div class="rating-subtype-row">
      <button class="rating-sub-btn ${subtype==='stars'  ?'active':''}" data-sub="stars">★ Stars</button>
      <button class="rating-sub-btn ${subtype==='numeric'?'active':''}" data-sub="numeric">1–N Scale</button>
      <button class="rating-sub-btn ${subtype==='likert' ?'active':''}" data-sub="likert">Likert</button>
    </div>`;

  let preview = '';
  if (subtype === 'stars'){
    const stars = Array.from({length:max},(_,i)=>
      `<button class="rating-star-btn" data-star="${i+1}">★</button>`).join('');
    preview = `
      <div class="rating-stars-row">${stars}</div>
      <div class="rating-count-row">
        <span>Max stars:</span>
        <input type="number" class="rating-max-input" value="${max}" min="1" max="10">
      </div>`;
  } else if (subtype === 'numeric'){
    const cells = Array.from({length:max},(_,i)=>
      `<div class="linear-cell">${i+1}</div>`).join('');
    preview = `
      <div class="linear-preview">${cells}</div>
      <div class="rating-count-row">
        <span>Max value:</span>
        <input type="number" class="rating-max-input" value="${max}" min="1" max="10">
      </div>`;
  } else {
    preview = `<div class="likert-preview">
      ${LIKERT_LABELS.map(l=>`<div class="likert-cell">${l}</div>`).join('')}
    </div>`;
  }

  return `<div class="rating-edit-wrap">${subtypeSel}${preview}</div>`;
}

// ── Editable options ───────────────────────────────────────
function buildOpts(q, style){
  if (!q.options||!q.options.length) q.options=['Option 1'];
  const mark = oi =>
    style==='radio'    ? `<span class="opt-radio-mark"></span>` :
    style==='checkbox' ? `<span class="opt-check-mark"></span>` :
                         `<span class="opt-num">${oi+1}.</span>`;
  const addMark =
    style==='radio'    ? `<span class="opt-radio-mark" style="opacity:0.3"></span>` :
    style==='checkbox' ? `<span class="opt-check-mark" style="opacity:0.3"></span>` :
                         `<span class="opt-num">${q.options.length+1}.</span>`;

  return `<div class="opts-list" data-style="${style}">
    ${q.options.map((o,oi)=>`
      <div class="opt-row" data-oi="${oi}">
        ${mark(oi)}
        <input class="opt-text" value="${esc(o)}" placeholder="Option ${oi+1}">
        <button class="opt-del-btn" title="Remove">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>`).join('')}
    <div class="opt-add-row">
      ${addMark}
      <button class="opt-add-btn">Add option</button>
    </div>
  </div>`;
}

function bindOptEvents(row, idx){
  row.querySelectorAll('.opt-text').forEach((inp,oi) => {
    inp.addEventListener('input', e => {
      if (!questions[idx].options) questions[idx].options=[];
      questions[idx].options[oi] = e.target.value;
    });
  });
  row.querySelectorAll('.opt-del-btn').forEach((_,oi) => {
    row.querySelectorAll('.opt-del-btn')[oi].addEventListener('click', e => {
      e.stopPropagation();
      if (questions[idx].options.length<=1) return;
      questions[idx].options.splice(oi,1);
      refreshBody(row,idx); saveHistory();
    });
  });
  const addBtn = row.querySelector('.opt-add-btn');
  if (addBtn){
    addBtn.addEventListener('click', e => {
      e.stopPropagation();
      if (!questions[idx].options) questions[idx].options=[];
      questions[idx].options.push(`Option ${questions[idx].options.length+1}`);
      refreshBody(row,idx); saveHistory();
      const inps = row.querySelectorAll('.opt-text');
      if (inps.length) inps[inps.length-1].focus();
    });
  }
  // Rating subtype switcher
  row.querySelectorAll('.rating-sub-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      questions[idx].ratingStyle = btn.dataset.sub;
      if (btn.dataset.sub === 'likert') questions[idx].ratingMax = 5;
      refreshBody(row,idx); saveHistory();
    });
  });
  // Rating max input
  const ratingMaxInput = row.querySelector('.rating-max-input');
  if (ratingMaxInput){
    ratingMaxInput.addEventListener('change', e => {
      const val = Math.min(10, Math.max(1, parseInt(e.target.value)||5));
      questions[idx].ratingMax = val;
      refreshBody(row,idx); saveHistory();
    });
  }
}

function refreshBody(row,idx){
  row.querySelector('.q-card-body').innerHTML = buildBody(questions[idx]);
  bindOptEvents(row,idx);
}

// ── Type selector ──────────────────────────────────────────
function buildTypeSel(cur){
  const all=['short','open','mcq','checkbox','dropdown','rating','date','file'];
  return `
    <div class="q-type-wrap">
      <button class="q-type-btn" type="button">
        <span class="q-type-icon">${TYPE_ICONS[cur]||''}</span>
        <span class="q-type-label">${TYPE_LABELS[cur]||cur}</span>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2.5" class="q-type-chev">
          <polyline points="6,9 12,15 18,9"/>
        </svg>
      </button>
      <div class="q-type-menu">
        ${all.map(t=>`
          <div class="q-type-opt ${t===cur?'active':''}" data-key="${t}">
            <span class="q-type-opt-icon">${TYPE_ICONS[t]||''}</span>
            ${TYPE_LABELS[t]}
          </div>`).join('')}
      </div>
    </div>`;
}

function bindTypeSel(row,idx){
  const wrap = row.querySelector('.q-type-wrap');
  const btn  = row.querySelector('.q-type-btn');
  if (!wrap||!btn) return;

  btn.addEventListener('click', e => {
    e.stopPropagation();
    const was = wrap.classList.contains('open');
    document.querySelectorAll('.q-type-wrap.open').forEach(w=>w.classList.remove('open'));
    if (!was) wrap.classList.add('open');
  });

  row.querySelectorAll('.q-type-opt').forEach(opt => {
    opt.addEventListener('click', e => {
      e.stopPropagation();
      const key = opt.dataset.key;
      questions[idx].type = key;
      if (OPTION_TYPES.includes(key) && !questions[idx].options?.length)
        questions[idx].options=['Option 1'];
      if (key==='rating' && !questions[idx].ratingMax)
        questions[idx].ratingMax=5;
      if (key==='rating' && !questions[idx].ratingStyle)
        questions[idx].ratingStyle='stars';
      btn.querySelector('.q-type-label').textContent = TYPE_LABELS[key];
      btn.querySelector('.q-type-icon').innerHTML    = TYPE_ICONS[key]||'';
      wrap.querySelectorAll('.q-type-opt').forEach(o=>o.classList.remove('active'));
      opt.classList.add('active');
      wrap.classList.remove('open');
      row.querySelector('.q-card-body').innerHTML = buildBody(questions[idx]);
      bindOptEvents(row,idx);
      saveHistory();
    });
  });
}

document.addEventListener('click', () => {
  document.querySelectorAll('.q-type-wrap.open').forEach(w=>w.classList.remove('open'));
});

// ── Add buttons ────────────────────────────────────────────
function bindAddButtons(){
  document.getElementById('addQ')?.addEventListener('click', () => {
    const lastSec = sections[sections.length-1];
    questions.push({
      id:'q'+Date.now(), sectionId:lastSec.id,
      text:'', type:'mcq', code:'', construct:null,
      required:true, options:['Option 1'], ratingMax:5,
      ratingStyle:'stars', sectionTitle:lastSec.title||'', sectionNum:sections.length,
    });
    saveHistory(); renderAll();
    const t = document.querySelectorAll('.q-title-input');
    if (t.length) t[t.length-1].focus();
  });

  document.getElementById('addSection')?.addEventListener('click', () => {
    const newSec = { id:'s'+Date.now(), title:'', desc:'' };
    sections.push(newSec);
    const secNum = sections.length;
    questions.push({
      id:'q'+Date.now(), sectionId:newSec.id,
      text:'', type:'mcq', code:'', construct:null,
      required:true, options:['Option 1'], ratingMax:5,
      ratingStyle:'stars', sectionTitle:'', sectionNum:secNum,
    });
    saveHistory(); renderAll();
    const blocks = document.querySelectorAll('.section-block');
    if (blocks.length) blocks[blocks.length-1].scrollIntoView({behavior:'smooth'});
  });
}

// ── HMSAM construct list ───────────────────────────────────
function renderConstructList(){
  const el = document.getElementById('constructList');
  if (!el) return;
  const counts = {};
  questions.forEach(q => {
    if (q.construct) counts[q.construct]=(counts[q.construct]||0)+1;
  });
  el.innerHTML = Object.entries(counts).map(([k,n])=>`
    <div class="construct-row">
      <span style="color:#6b6b62;">${CONSTRUCT_LABELS[k]||k}</span>
      <span class="c-count">${n}</span>
    </div>`).join('');
}

// ── Panel interactions ─────────────────────────────────────
function bindPanel(){
  // Question colour
  document.querySelectorAll('.swatches[data-target="qcolor"] .swatch:not(.custom)')
    .forEach(s => s.addEventListener('click', () => {
      document.querySelectorAll('.swatches[data-target="qcolor"] .swatch')
        .forEach(x=>x.classList.remove('active'));
      s.classList.add('active');
      document.querySelectorAll('.q-title-input').forEach(el=>el.style.color=s.dataset.color);
    }));
  const qcp = document.getElementById('qColorPicker');
  document.getElementById('qColorCustom')?.addEventListener('click', ()=>qcp?.click());
  qcp?.addEventListener('input', e => {
    document.querySelectorAll('.q-title-input').forEach(el=>el.style.color=e.target.value);
  });

  // Accent colour
  document.querySelectorAll('.swatches[data-target="accent"] .swatch:not(.custom)')
    .forEach(s => s.addEventListener('click', () => {
      document.querySelectorAll('.swatches[data-target="accent"] .swatch')
        .forEach(x=>x.classList.remove('active'));
      s.classList.add('active');
      accentColor = s.dataset.color;
      applyAccent(accentColor);
    }));
  const acp = document.getElementById('accentPicker');
  document.getElementById('accentCustom')?.addEventListener('click', ()=>acp?.click());
  acp?.addEventListener('input', e => { accentColor=e.target.value; applyAccent(accentColor); });

  // Accent slider
  document.getElementById('accentSlider')?.addEventListener('input', e => {
    const v = e.target.value;
    document.getElementById('accentVal').textContent = v+' px';
    const hc = document.getElementById('headerCard');
    if (hc) hc.style.borderTopWidth = v+'px';
  });

  // Header font
  document.querySelectorAll('#headerFontList .font-row').forEach(r => {
    r.addEventListener('click', () => {
      document.querySelectorAll('#headerFontList .font-row').forEach(x=>x.classList.remove('active'));
      r.classList.add('active');
      headerFont = r.dataset.font; 
      const ft = document.getElementById('formTitle');
      if (ft) ft.style.fontFamily = r.dataset.font;
    });
  });

  // Header colour
  document.querySelectorAll('.swatches[data-target="headercolor"] .swatch:not(.custom)')
    .forEach(s => s.addEventListener('click', () => {
      document.querySelectorAll('.swatches[data-target="headercolor"] .swatch')
        .forEach(x=>x.classList.remove('active'));
      s.classList.add('active');
      const ft = document.getElementById('formTitle');
      if (ft) ft.style.color = s.dataset.color;
    }));
  const hcp = document.getElementById('headerColorPicker');
  document.getElementById('headerColorCustom')?.addEventListener('click', ()=>hcp?.click());
  hcp?.addEventListener('input', e => {
    const ft = document.getElementById('formTitle');
    if (ft) ft.style.color = e.target.value;
  });

  // Header image
  const imgInput  = document.getElementById('headerImgInput');
  const imgWrap   = document.getElementById('headerImgWrap');
  const imgEl     = document.getElementById('headerImg');
  const imgName   = document.getElementById('imgFilename');
  const removeBtn = document.getElementById('headerImgRemove');

  document.getElementById('headerImgBtn')?.addEventListener('click', ()=>imgInput?.click());
  imgInput?.addEventListener('change', e => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => {
      headerImageBase64 = ev.target.result;
      if (imgEl)   imgEl.src  = headerImageBase64;
      if (imgWrap) imgWrap.style.display = 'block';
      if (imgName) imgName.textContent   = file.name;
    };
    reader.readAsDataURL(file);
  });
  removeBtn?.addEventListener('click', () => {
    headerImageBase64 = null;
    if (imgEl)    imgEl.src = '';
    if (imgWrap)  imgWrap.style.display = 'none';
    if (imgInput) imgInput.value = '';
    if (imgName)  imgName.textContent = '';
  });

  // Background
  document.querySelectorAll('#bgSeg .seg-item').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#bgSeg .seg-item').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      bgColor = b.dataset.bg;
      document.querySelector('.canvas').style.background = bgColor;
    });
  });

  // Question font
  document.querySelectorAll('#qFontList .font-row').forEach(r => {
    r.addEventListener('click', () => {
      document.querySelectorAll('#qFontList .font-row').forEach(x=>x.classList.remove('active'));
      r.classList.add('active');
      qFont = r.dataset.font;
      document.querySelectorAll('.q-title-input').forEach(el=>el.style.fontFamily=qFont);
    });
  });

  // Button text
  document.getElementById('btnText')?.addEventListener('input', e => {
    document.getElementById('btnPreview').textContent = e.target.value;
  });

  // Button colour — FIX: update btnColor state
  document.querySelectorAll('.swatches[data-target="btncolor"] .swatch:not(.custom)')
    .forEach(s => s.addEventListener('click', () => {
      document.querySelectorAll('.swatches[data-target="btncolor"] .swatch')
        .forEach(x=>x.classList.remove('active'));
      s.classList.add('active');
      btnColor = s.dataset.color;
      document.documentElement.style.setProperty('--btn-bg', btnColor);
      const p = document.getElementById('btnPreview');
      if (p){ p.style.background=btnColor; p.style.color='#fff'; p.style.border='none'; }
    }));
  const bcp = document.getElementById('btnColorPicker');
  document.getElementById('btnColorCustom')?.addEventListener('click', ()=>bcp?.click());
  bcp?.addEventListener('input', e => {
    btnColor = e.target.value;
    document.documentElement.style.setProperty('--btn-bg', btnColor);
    const p = document.getElementById('btnPreview');
    if (p){ p.style.background=btnColor; p.style.color='#fff'; p.style.border='none'; }
  });

  // Button shape — FIX: update btnRadius state
  document.querySelectorAll('#shapeRow .seg-item').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#shapeRow .seg-item').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      const p = document.getElementById('btnPreview');
      if (!p) return;
      if (b.dataset.style==='outline'){
        btnRadius = '6';
        p.style.background='transparent'; p.style.color='#14140f';
        p.style.border='1.5px solid #14140f'; p.style.borderRadius='6px';
      } else {
        btnRadius = b.dataset.radius;
        p.style.border='none';
        p.style.background=btnColor;
        p.style.color='#fff';
        p.style.borderRadius=btnRadius+'px';
      }
    });
  });

  // Button size — FIX: update btnPadding and btnFontSize state
  document.querySelectorAll('#sizeRow .seg-item').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#sizeRow .seg-item').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      btnPadding  = b.dataset.pad;
      btnFontSize = b.dataset.fs;
      const p = document.getElementById('btnPreview');
      if (!p) return;
      p.style.padding  = btnPadding;
      p.style.fontSize = btnFontSize;
    });
  });

  // All required toggle
  const allReqToggle = document.getElementById('allReqToggle');
  allReqToggle?.addEventListener('click', () => {
    allReqToggle.classList.toggle('on');
    const isOn = allReqToggle.classList.contains('on');
    questions.forEach(q => q.required = isOn);
    document.querySelectorAll('.q-req-toggle .toggle').forEach(t=>{
      t.classList.toggle('on', isOn);
    });
    saveHistory();
  });
}

function applyAccent(color){
  document.documentElement.style.setProperty('--accent', color);
  const hc = document.getElementById('headerCard');
  if (hc) hc.style.borderTopColor = color;
}

// ── Publish ────────────────────────────────────────────────
function buildPayload(action){
  // Sync DOM state into the questions array
  document.querySelectorAll('.q-row').forEach(row => {
    const idx = parseInt(row.dataset.idx);
    if (isNaN(idx)) return;
    const q = questions[idx];
    if (!q) return;
    if (OPTION_TYPES.includes(q.type)){
      q.options = [...row.querySelectorAll('.opt-text')]
        .map(i => i.value.trim()).filter(Boolean);
    }
    if (q.type === 'rating'){
      const inp = row.querySelector('.rating-max-input');
      if (inp) q.ratingMax = parseInt(inp.value)||5;
    }
  });

  sections.forEach((sec, si) => {
    questions.forEach(q => {
      if (q.sectionId === sec.id){
        q.sectionTitle = sec.title || '';
        q.sectionNum   = si + 1;
      }
    });
  });

  return {
    action:       action,
    organization_id: document.getElementById('organizationScopeSelect')?.value || '',
    title:        document.getElementById('formTitle').value.trim()||'Untitled Survey',
    description:  document.getElementById('formDesc').value.trim(),
    theme_accent: accentColor,
    theme_bg:     bgColor,
    submit_label: document.getElementById('btnText')?.value||'Submit',
    header_image: headerImageBase64 || '',
    btn_color:    btnColor,
    btn_radius:   btnRadius,
    btn_padding:  btnPadding,
    btn_font_size:btnFontSize,
    q_font:       qFont,
    header_font:  headerFont,
    questions: questions.map(q=>({
      id:           q.dbId || null,
      text:         q.text,
      code:         q.code||'',
      type:         q.type,
      construct:    q.construct||'',
      required:     q.required!==false,
      options:      q.options||[],
      ratingMax:    q.ratingMax||5,
      ratingStyle:  q.ratingStyle||'stars',
      sectionTitle: q.sectionTitle||'',
      sectionNum:   q.sectionNum||1,
    })),
  };
}

function bindPublish(){
  document.getElementById('btnPublish')?.addEventListener('click', publishSurvey);
  document.getElementById('createOrganizationButton')?.addEventListener('click', createOrganizationScope);
}

async function createOrganizationScope(){
  const input = document.getElementById('newOrganizationName');
  const select = document.getElementById('organizationScopeSelect');
  const status = document.getElementById('saveStatus');
  const name = input?.value.trim();
  if (!name){
    if (status) status.textContent = 'Enter an organization name first.';
    input?.focus();
    return;
  }
  const button = document.getElementById('createOrganizationButton');
  if (button) button.disabled = true;
  try {
    const response = await fetch(_d.create_organization_url, {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
      body: JSON.stringify({name, type: 'research'}),
    });
    const data = await response.json();
    if (!response.ok || !data?.entity?.id){
      if (status) status.textContent = data?.error || 'Could not create organization.';
      return;
    }
    const option = document.createElement('option');
    option.value = String(data.entity.id);
    option.textContent = data.entity.name;
    select?.append(option);
    if (select) select.value = option.value;
    if (input) input.value = '';
    if (status) status.textContent = 'Organization created.';
  } catch {
    if (status) status.textContent = 'Network error while creating organization.';
  } finally {
    if (button) button.disabled = false;
  }
}

async function publishSurvey(){
  const st = document.getElementById('saveStatus');
  if (st) st.textContent = 'Publishing…';

  const payload = buildPayload('publish');

  try {
    const res = await fetch(SAVE_URL, {
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':CSRF_TOKEN},
      body: JSON.stringify(payload),
    });
    const data = await res.json();

    if (data.status === 'created'){
      window.clearBuilderDirty?.();
      showCreatedModal(data.dashboard_url);
      if (st) st.textContent = '';
    } else if (data.status === 'published'){
      window.clearBuilderDirty?.();
      showPublishModal(data.dashboard_url);
      if (st) st.textContent = '';
    } else if (data.redirect){
      window.clearBuilderDirty?.();
      window.location.href = data.redirect;
    } else {
      if (st) st.textContent = data?.error || 'Error — please try again.';
    }

  } catch(err){
    if (st) st.textContent = 'Network error.';
    console.error(err);
  }
}

async function saveAsDraft(){
  const st = document.getElementById('saveStatus');
  if (st) st.textContent = 'Saving…';

  const payload = buildPayload('draft');

  try {
    const res = await fetch(SAVE_URL, {
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':CSRF_TOKEN},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.redirect){
      window.clearBuilderDirty?.();
      window.location.href = data.redirect;
    } else {
      if (st) st.textContent = data?.error || 'Error — please try again.';
    }
  } catch(err){
    if (st) st.textContent = 'Network error.';
    console.error(err);
  }
}

// ── Utility ────────────────────────────────────────────────
function esc(s){
  return String(s||'')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Unsaved-changes exit guard ─────────────────────────────
(function(){
  let builderDirty = false;
  let pendingHref  = null;

  // Expose a setter so other parts of the app (e.g. saveHistory,
  // swatch/font pickers) can mark the form dirty.
  window.markBuilderDirty = () => { builderDirty = true; };
  window.clearBuilderDirty = () => { builderDirty = false; };

  // Delegated input listener on document — survives re-renders of
  // formTitle / sectionsContainer, and covers dynamically created fields.
  document.addEventListener('input', e => {
    if (e.target.closest('#sectionsContainer, #formTitle, #formDesc, #surveyNameInput, #mainCanvas')){
      builderDirty = true;
    }
  });
  // Cover clicks on swatches, fonts, toggles, add/delete buttons, etc.
  document.addEventListener('change', e => {
    if (e.target.closest('#mainCanvas, .panel, #sectionsContainer')){
      builderDirty = true;
    }
  });

  // Clear the flag whenever a draft is saved.
  if (typeof window.saveAsDraft === 'function'){
    const _orig = window.saveAsDraft;
    window.saveAsDraft = async function(){ builderDirty = false; return _orig.apply(this, arguments); };
  } else if (typeof saveAsDraft === 'function'){
    try {
      const _orig = saveAsDraft;
      window.saveAsDraft = async function(){ builderDirty = false; return _orig.apply(this, arguments); };
    } catch(err){ console.warn('saveAsDraft wrap skipped:', err); }
  }

  const overlay    = document.getElementById('exitModalOverlay');
  const btnClose   = document.getElementById('exitModalClose');
  const btnConfirm = document.getElementById('exitModalConfirm');
  const btnCancel  = document.getElementById('exitModalCancel');

  function openModal(href){ pendingHref = href; overlay?.classList.add('show'); }
  function closeModal(){ pendingHref = null; overlay?.classList.remove('show'); }

  btnConfirm?.addEventListener('click', () => {
    builderDirty = false;
    const href = pendingHref;
    closeModal();
    if (href) window.location.href = href;
  });
  btnCancel?.addEventListener('click', closeModal);
  btnClose ?.addEventListener('click', closeModal);
  overlay  ?.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && overlay?.classList.contains('show')) closeModal();
  });

  document.addEventListener('click', e => {
    const link = e.target.closest('a[href]');
    if (!link || !builderDirty) return;
    const href = link.getAttribute('href');
    if (!href || href.startsWith('#') || link.target === '_blank') return;
    e.preventDefault();
    openModal(href);
  });

  window.addEventListener('beforeunload', e => {
    if (builderDirty){ e.preventDefault(); e.returnValue = ''; }
  });
})();

// - Publish modals

function showPublishModal(dashboardUrl){
  const modal = document.getElementById('publishModal');
  const btn   = document.getElementById('goToSurveyBtn');
  if (btn) btn.href = dashboardUrl;
  if (modal) modal.style.display = 'flex';
}

function showCreatedModal(dashboardUrl){
  const modal = document.getElementById('createdModal');
  const btn   = document.getElementById('createdOkBtn');
  if (btn) btn.href = dashboardUrl;
  if (modal) modal.style.display = 'flex';
}

function closePublishModal(){
  const modal = document.getElementById('publishModal');
  if (modal) modal.style.display = 'none';
}
