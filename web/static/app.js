/* 에이전트고삐 — 공통 유틸 */

const API = {
  async get(path) { return handle(await fetch(path)); },
  async post(path, body) {
    return handle(await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }));
  }
};

async function handle(res) {
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || ('요청 실패 (' + res.status + ')'));
  return data;
}

const PERM_LABEL = {
  AUTO: '자동 실행',
  VERIFY: '추가 승인 필요',
  READ_ONLY: '조회만 허용',
  STOP: '실행권한 중단'
};
const PERM_CAP = {
  AUTO: '조회 · 송금 · 결제',
  VERIFY: '조회 / 송금·결제는 승인 후',
  READ_ONLY: '조회만',
  STOP: '전면 중단'
};
const PERM_COLOR = {
  AUTO: 'var(--auto)',
  VERIFY: 'var(--verify)',
  READ_ONLY: 'var(--readonly)',
  STOP: 'var(--stop)'
};
const PERM_ORDER = ['AUTO', 'VERIFY', 'READ_ONLY', 'STOP'];

function permBadge(p) {
  return `<span class="perm ${p}"><span class="dot"></span>${p}</span>`;
}
function stagePill(p) {
  return `<span class="stage-pill" style="color:${PERM_COLOR[p]}">${p}</span>`;
}
function won(n) {
  if (n === null || n === undefined) return '-';
  return Math.round(n).toLocaleString('ko-KR') + '원';
}
function riskColor(v) {
  if (v >= 78) return 'var(--stop)';
  if (v >= 55) return 'var(--readonly)';
  if (v >= 30) return 'var(--verify)';
  return 'var(--auto)';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function markNav(id) {
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('on'));
  const el = document.getElementById(id);
  if (el) el.classList.add('on');
}
function policyRows(display) {
  return display.map(d => `
    <div class="policy-row">
      <dt>${esc(d.label)}</dt>
      <dd>${esc(d.value)}<span class="hint">${esc(d.hint)}</span></dd>
    </div>`).join('');
}
