// Exercise the real Research Lab selection/request functions without a browser,
// server, network requests, backtests, saves or deployments.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'static/app.js'), 'utf8');
const presets = JSON.parse(fs.readFileSync(path.join(__dirname, 'data/strategies.json'), 'utf8'));
const name = 'BTCUSD 5m EMA Pullback (9/21) — Paper Signal Test';
const matches = presets.filter(s => s.symbol === 'BTCUSD' && s.name === name);
assert.equal(matches.length, 1);
const preset = matches[0];
const nodes = new Map();
function $(id) {
  if (!nodes.has(id)) nodes.set(id, {value: '', checked: true, textContent: '', options: [],
    classList: {toggle() {}}, disabled: false});
  return nodes.get(id);
}
$('symbolSelect').value = 'BTCUSD';
$('daysSelect').value = '180';
$('endSelect').value = 'latest';
$('dailyLoss').value = 99;
$('triggerSource').value = 'mark';
$('strictLot').checked = false;
const context = {state: {customStrategies: []}, $, document: {querySelectorAll: () => []},
  setOptions(element, options) {
    element.options = options.map(([value, label]) => ({value, label}));
    element.value = element.options[0]?.value || '';
  },
  async api(url) {
    assert.equal(url, '/api/strategies?symbol=BTCUSD');
    return {strategies: presets.filter(s => ['ALL', 'BTCUSD'].includes(s.symbol))};
  }};
vm.createContext(context);
for (const name of ['loadCustomStrategies', 'currentStrategyObj', 'applySelectedStrategyDefaults',
  'setTimeframePill', 'setRiskValue', 'updateAdvSummary', 'updateOverviewHeaderPreview', 'toggleCustomRange', 'readRequest']) {
  const match = new RegExp(`(?:async )?function ${name}\\(`).exec(source);
  assert.ok(match, name);
  const end = source.indexOf('\n}', match.index) + 2;
  vm.runInContext(source.slice(match.index, end), context);
}
(async () => {
  await context.loadCustomStrategies('BTCUSD', preset.id);
  assert.equal($('strategySelect').options.find(o => o.value === preset.id).label, name);
  assert.equal($('strategySelect').value, preset.id);
  assert.equal($('tfSelect').value, '5m');
  assert.equal($('capital').value, 50000);
  assert.equal($('leverage').value, 10);
  assert.equal($('risk').value, 1);
  assert.equal($('slType').value, 'atr_mult');
  assert.equal($('slValue').value, 1.5);
  assert.equal($('tpType').value, 'atr_mult');
  assert.equal($('tpValue').value, 3);
  assert.equal($('feeMode').value, 'taker');
  assert.equal($('slippage').value, '5');
  assert.equal($('triggerSource').value, 'trade');
  assert.equal($('dailyLoss').value, 5);
  for (const id of ['trailEnabled', 'trend4h', 'costGate']) assert.equal($(id).checked, false, id);
  assert.equal($('strictLot').checked, true);
  for (const [key, value] of Object.entries(preset.params)) {
    assert.ok($('strategyRuleHint').textContent.includes(`${key}=${value}`), key);
  }
  const request = context.readRequest();
  assert.equal(request.rule_type, 'ema_pullback_scalp');
  assert.equal(request.exit_mode, 'fixed');
  assert.equal(request.trailing_stop, false);
  assert.equal(request.trail_mult, 0);
  assert.equal(request.history_start, '2026-05-07T00:00:00Z');
  assert.equal(request.start, '2026-08-01T00:00:00Z');
  assert.equal(request.end, '2026-10-01T00:00:00Z');
  assert.equal(request.cooldown_bars, 1);
  assert.equal(request.mark_fill_mode, 'trigger');
  process.stdout.write(JSON.stringify(request));
})().catch(error => { console.error(error); process.exitCode = 1; });
