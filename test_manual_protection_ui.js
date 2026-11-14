// Run the actual modal handlers in a disposable DOM/API; never send real orders.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const html = fs.readFileSync('static/index.html', 'utf8');
const nodes = new Map(), calls = [];
let finishSave, confirmCount = 0;
const context = {
  $(id) {
    if (!nodes.has(id)) nodes.set(id, {_value:'', get value(){return this._value;}, set value(v){this._value=String(v);}, checked:false, disabled:false, textContent:'', open:false,
      showModal() {this.open = true;}, close() {this.open = false;}});
    return nodes.get(id);
  },
  mt:{state:null, product:null, catalog:{}},
  isNum:x => typeof x === 'number' && Number.isFinite(x),
  inr:x => `${x > 0 ? '+' : ''}₹${x.toFixed(2)}`,
  mtMsg:()=>{}, toast:()=>{}, mtLoadState:async()=>{},
  mtLiveConfirm:()=>{confirmCount++; return 'LIVE';},
  api:async(url, options)=>{
    calls.push({url, method:options.method, body:JSON.parse(options.body)});
    if (finishSave !== null) await new Promise(resolve=>{finishSave=resolve;});
    return {};
  }
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function decimals('), source.indexOf('\n}',source.indexOf('function decimals('))+2),context);
vm.runInContext(source.slice(source.indexOf('function mtBinding('), source.indexOf('\nfunction mtNewRequestId')),context);
vm.runInContext(source.slice(source.indexOf('function mtOpenProtect('), source.indexOf('\nasync function mtClose(')),context);
const $ = context.$;
const p = {symbol:'BTCUSD', side:'LONG', contracts:1, contract_value:.001, entry_price:68733.5,
  mark_price:68612.10422422, tick_size:.5, sl_price:null, tp_price:null};
context.mt.state = {mode:'live',mode_generation:5,real_money:true,usd_inr_rate:85,positions:[p]};
$('mtSlMode').value = $('mtTpMode').value = 'pct';
const submit = () => context.mtSaveProtect({preventDefault(){}});
(async()=>{
  finishSave=null;
  context.mtOpenProtect('BTCUSD');
  assert.equal($('mtPTpMode').value,'pct');
  assert.match($('mtProtectPosition').textContent,/BTCUSD.*LONG.*1 contracts.*Entry.*Mark/);
  $('mtPTp').value='6';context.mtProtectPreview();
  assert.match($('mtPTpPreview').textContent,/72857.5.*₹350.54/);
  assert.equal($('mtProtectSave').disabled,false);
  await submit();
  assert.equal(calls[0].body.tp_pct,6);
  assert.equal(calls[0].body.tp_price,undefined);
  assert.equal(calls[0].body.expected_mode,'live');
  assert.equal(calls[0].body.confirm,'LIVE');assert.equal(confirmCount,1);
  // Explicit Price mode catches the original raw-$6 mistake before an API call.
  context.mtOpenProtect('BTCUSD');$('mtPTpMode').value='price';$('mtPTp').value='6';
  context.mtProtectPreview();assert.equal($('mtProtectSave').disabled,true);
  assert.match($('mtProtectMsg').textContent,/trigger immediately/);
  await submit();assert.equal(calls.length,1);
  // An entry-based percentage already crossed by Mark requires an explicit ref change.
  $('mtPTpMode').value='pct';$('mtPTp').value='6';p.mark_price=80000;
  context.mtProtectPreview();assert.equal($('mtProtectSave').disabled,true);
  $('mtPReference').value='mark';context.mtProtectPreview();
  assert.match($('mtPTpPreview').textContent,/84800.0/);
  await submit();assert.equal(calls[1].body.reference,'mark');
  // Short direction and tick metadata for another symbol while BTC is selected.
  Object.assign(p,{symbol:'1000PEPEUSD', side:'SHORT',contracts:17,contract_value:1000,
    entry_price:.0045,mark_price:.0045,tick_size:1e-7});
  context.mtOpenProtect(p.symbol);$('mtPSl').value='2';$('mtPTp').value='6';
  context.mtProtectPreview();
  assert.match($('mtPSlPreview').textContent,/0.0045900/);
  assert.match($('mtPTpPreview').textContent,/0.0042300/);
  assert.equal($('mtProtectSave').disabled,false);
  // Preserve exact existing triggers in Price mode; a blank field leaves it unchanged.
  Object.assign(p,{sl_price:.0046,tp_price:.0042});context.mtOpenProtect(p.symbol);
  assert.equal($('mtPSlMode').value,'price');assert.equal($('mtPTpMode').value,'price');
  $('mtPSl').value='';$('mtPClearTp').checked=true;
  await submit();assert.equal(calls[2].body.clear_tp,true);assert.equal(calls[2].body.clear_sl,undefined);
  assert.equal(calls[2].body.sl_price,undefined);
  // Live mode/generation changes invalidate an open modal before confirmation/submission.
  context.mtOpenProtect(p.symbol);context.mt.state.mode_generation++;
  await submit();assert.equal(calls.length,3);assert.match($('mtProtectMsg').textContent,/mode changed/);
  // Reject invalid input; lock Save until the outstanding request completes.
  context.mtOpenProtect(p.symbol);$('mtPSl').value='NaN';await submit();assert.equal(calls.length,3);
  $('mtPSl').value='.0046';finishSave=undefined;
  const pending=submit();assert.equal($('mtProtectSave').disabled,true);
  await submit();assert.equal(calls.length,4);finishSave();await pending;
  assert.equal($('mtProtectSave').disabled,false);
  for (const id of ['mtPSlMode','mtPTpMode','mtPReference','mtProtectPosition','mtPSlPreview','mtPTpPreview'])
    assert.ok(html.includes(`id="${id}"`));
  assert.equal(context.decimals(1e-7),7);assert.equal(context.decimals(.5),1);
  console.log('PASS: percentage/price payloads, LONG/SHORT math, exact ticks, INR PnL, mark warnings, explicit clear, mode binding and duplicate Save guard');
})().catch(error=>{console.error(error);process.exitCode=1;});
