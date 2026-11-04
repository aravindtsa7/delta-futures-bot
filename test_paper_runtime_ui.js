// Exercise error messages and recorded-evidence rendering without an app or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'static/app.js'), 'utf8');
const nodes = new Map();
const context = {
  $(id) { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
  esc: x => String(x), td: x => `<td>${x}</td>`,
  fetch: async () => ({ok:false,status:409,statusText:'Conflict',json:async () => ({detail:{
    code:'shared_wallet_mismatch',symbol:'BTCUSD',
    mismatches:{max_daily_loss_pct:{profile:25,wallet:5}},
    action:'Rerun/save with the existing paper wallet settings, then deploy.'
  }})})
};
vm.createContext(context);
for (const name of ['api','renderRunnerEvidence']) {
  const start = new RegExp(`(?:async )?function ${name}\\(`).exec(source).index;
  const end = source.indexOf('\n}', start)+2;
  vm.runInContext(source.slice(start,end),context);
}
(async () => {
  await assert.rejects(context.api('/offline'), error => error.status===409 &&
    /profile 25, wallet 5/.test(error.message) && /Rerun\/save/.test(error.message));
  context.renderRunnerEvidence({runner_evaluations:{BTCUSD:{strategy:'ema_pullback_scalp',
    bar_close:'2026-11-03T07:30:00Z',evaluated_at:'2026-11-03T07:30:01Z',raw_signal:1,
    decision:'skip',skip_reason:'profile differs from existing shared wallet',config_id:'a'.repeat(64),from_current_run:true}},
    wallet_provisional:true,pending_funding:[{symbol:'BTCUSD'}],signals:{BTCUSD:{signal_side:'SHORT'}}});
  const html = nodes.get('botEvaluations').innerHTML;
  assert.match(html,/LONG/);assert.match(html,/skip/);assert.match(html,/shared wallet/);
  assert.doesNotMatch(html,/SHORT/);assert.match(nodes.get('botAccounting').textContent,/ACCOUNTING PENDING/);
  context.renderRunnerEvidence({});
  assert.match(nodes.get('botEvaluations').innerHTML,/No recorded runner evaluations/);
  console.log('PASS: actionable wallet error, runner/dashboard separation, refused entry and accounting visibility');
})().catch(error => { console.error(error); process.exitCode=1; });
