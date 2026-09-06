markNav('nav-sim');

let scenarios = [];
let selected = null;

async function load() {
  try {
    const [s, p] = await Promise.all([API.get('/api/scenarios'), API.get('/api/policy')]);
    scenarios = s.scenarios;
    const pol = p.policy;
    document.getElementById('policy-line').innerHTML =
      `건당 자동실행 <b>${won(pol.auto_limit)}</b> · 1일 누적 <b>${won(pol.daily_limit)}</b> ·
       신규 수취인 <b>${pol.new_recipient.action === 'BLOCK' ? '차단' : pol.new_recipient.action === 'AUTO' ? '자동 허용' : '본인 승인'}</b>
       ${pol.source === 'default' ? '<span class="faint">(기본값 — 위임정책 화면에서 직접 설정하실 수 있습니다)</span>' : ''}`;

    document.getElementById('scn-grid').innerHTML = scenarios.map(s => `
      <button class="scn" data-id="${s.id}">
        <div class="scn-top">
          <h3>${esc(s.title)}</h3>
        </div>
        <p>${esc(s.summary)}</p>
      </button>`).join('');
  } catch (e) {
    document.getElementById('scn-grid').innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
  }
}

document.getElementById('scn-grid').addEventListener('click', ev => {
  const b = ev.target.closest('.scn');
  if (!b) return;
  document.querySelectorAll('.scn').forEach(x => x.classList.remove('sel'));
  b.classList.add('sel');
  selected = scenarios.find(s => s.id === b.dataset.id);
  document.getElementById('picked').innerHTML =
    `선택: <b>${esc(selected.title)}</b> — ${esc(selected.detail)}`;
  document.getElementById('btn-run').disabled = false;
});

document.getElementById('btn-run').addEventListener('click', async () => {
  if (!selected) return;
  const btn = document.getElementById('btn-run');
  const msg = document.getElementById('run-msg');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Agent 행동을 평가하는 중';
  msg.innerHTML = '';
  try {
    const r = await API.post('/api/simulate', { scenario_id: selected.id, explain: true });
    sessionStorage.setItem('lastRun', r.run_id);
    sessionStorage.setItem('restoreToken:' + r.run_id, r.restore_approval_token || '');
    location.href = '/result.html?run=' + r.run_id;
  } catch (e) {
    msg.innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
    btn.disabled = false;
    btn.textContent = '시뮬레이션 시작';
  }
});

load();
