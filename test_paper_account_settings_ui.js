// Exercise the actual UI handlers with a disposable DOM/API, never the real app.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const nodes = new Map(), storage = new Map(), calls = [];
let confirmResult = true, confirmation = '', reply = async () => ({message:'Saved; halt preserved'});
const context = {
  $(id) {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, {value:'',textContent:'',innerHTML:'',disabled:false,
        classList:{toggle(name,on) { if(on) classes.add(name); else classes.delete(name); },contains:name=>classes.has(name)}});
    }
    return nodes.get(id);
  },
  esc: x=>String(x), td:x=>`<td>${x}</td>`, inr:x=>`INR ${Number(x).toFixed(2)}`,
  bot:{status:null}, crypto:{randomUUID:()=>`request-${calls.length}-unique`},
  window:{confirm:message=>{confirmation=message;return confirmResult;}},
  localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  api:async (url,options)=>{calls.push({url,options,body:JSON.parse(options.body)});return reply();},
  loadBotStatus:async ()=>context.renderPaperAccount(context.bot.status)
};
vm.createContext(context);
const begin = source.indexOf('const paperAccountUi =');
const end = source.indexOf('\nasync function loadBotStatus()',begin);
vm.runInContext(source.slice(begin,end)+'\nglobalThis.state = paperAccountUi;',context);
const deployStart = source.indexOf('function deployPayload(');
vm.runInContext(source.slice(deployStart,source.indexOf('\n}',deployStart)+2),context);
assert.equal(context.deployPayload({id:'saved-result'}).mode,'paper');
const a = {initialized:true,session_id:'legacy',revision:0,capital_inr:50000,daily_loss_limit_pct:5,
  daily_baseline_inr:50000,baseline_date_utc:'2026-11-03',baseline_basis:'Persisted UTC daily opening equity',
  equity_inr:46987.45,allowance_inr:2500,halted:true,halt_reason:'daily loss',trade_count:5,
  positions:0,unresolved_intents:0,funding_obligations:0};
context.bot.status = {mode:'paper',paper_account:a,paper_session_catalog:{...a,sessions:[]},runner:{running:false}};
const $ = context.$, state = context.state;
state.process = {state:'stopped'};
(async ()=>{
  context.initPaperAccount(); context.renderPaperAccount(context.bot.status);
  assert.match($('paperAccountApplied').textContent,/HALTED/);
  assert.match($('paperAccountApplied').textContent,/2500.00.*Persisted UTC daily opening equity/);
  assert.equal($('paperDailyLossInput').value,5);
  assert.equal($('paperLimitSave').disabled,true); // Unchanged limit (5% === 5%) keeps Update Daily Loss Limit disabled
  $('paperDailyLossInput').value='8';$('paperDailyLossInput').oninput();
  assert.equal($('paperLimitSave').disabled,false); // Changed valid limit (8% !== 5%) enables Update Daily Loss Limit
  context.renderPaperAccount(context.bot.status);
  assert.equal($('paperDailyLossInput').value,'8');
  assert.match($('paperAccountDraft').textContent,/4000.00/);
  assert.match($('paperAccountApplied').textContent,/Daily loss 5%/);
  await context.paperAccountAction('daily_limit');
  assert.equal(calls[0].options.method,'PUT');assert.equal(calls[0].body.daily_loss_limit_pct,8);
  assert.equal(calls[0].body.capital_inr,undefined);assert.equal(a.halted,true);
  assert.match($('paperAccountError').textContent,/halt preserved/);
  $('paperDailyLossInput').value='Infinity';await context.paperAccountAction('daily_limit');
  assert.equal(calls.length,1);assert.match($('paperAccountError').textContent,/finite/);
  $('paperDailyLossInput').value='8';$('paperCapitalInput').value='100000';
  confirmResult=false;await context.paperAccountAction('new_session');assert.equal(calls.length,1);
  confirmResult=true;await context.paperAccountAction('new_session');
  assert.match(confirmation,/46987.45/);assert.match(confirmation,/100000.00/);assert.match(confirmation,/STOPPED/);
  assert.equal(calls[1].options.method,'POST');assert.equal(calls[1].body.confirmed_equity_inr,46987.45);
  assert.equal(calls[1].body.capital_inr,100000);
  // An uncertain network response retains the exact id/body; refresh must not mask its error.
  reply=async ()=>{throw new Error('Connection lost: retry the same request');};
  await context.paperAccountAction('daily_limit');
  const uncertain=calls.at(-1).body;
  assert.match($('paperAccountError').textContent,/Connection lost/);assert.ok(storage.size);
  reply=async ()=>({message:'Recovered'});
  await context.paperAccountAction(null,true);
  assert.deepEqual(calls.at(-1).body,uncertain);assert.equal(storage.size,0);
  // A second click while the first request is pending cannot create another session.
  let complete;reply=()=>new Promise(resolve=>{complete=resolve;});
  const pending=context.paperAccountAction('new_session');const count=calls.length;
  await context.paperAccountAction('new_session');assert.equal(calls.length,count);
  complete({message:'Created stopped'});await pending;
  assert.ok(calls.every(c=>!c.url.includes('/start')));
  state.process={state:'running',pid:123};context.bot.status.runner.running=true;context.renderPaperAccount(context.bot.status);
  assert.equal($('paperLimitSave').disabled,true);assert.equal($('paperSessionNew').disabled,true);
  context.bot.status.runner.running=false;context.bot.status.historical_session=true;
  context.bot.status.evaluation_history=[{symbol:'BTCUSD',bar_close:'closed',evaluated_at:'evaluated',raw_signal:1,
    decision:'skip',skip_reason:'halted',config_id:'saved-hash',config_json:'full configuration',detail:'recorded detail'}];
  context.bot.status.funding_history=[{symbol:'BTCUSD',at_us:1,contracts:2,rate:0,mark_price:10000,cashflow_usd:0}];
  context.bot.status.more_evaluations=true;context.renderPaperAccount(context.bot.status);
  assert.equal($('botStart').disabled,true);assert.equal($('paperLimitSave').disabled,true);
  assert.match($('paperEvalHistory').innerHTML,/halted/);assert.match($('paperFundingHistory').innerHTML,/BOOKED/);
  assert.equal($('paperEvalNext').disabled,false);
  const archivedCount=calls.length;await context.paperAccountAction('daily_limit');assert.equal(calls.length,archivedCount);
  $('botMode').value='testnet';context.bot.status.mode='testnet';context.renderPaperAccount(context.bot.status);
  assert.ok($('paperAccountCard').classList.contains('hidden'));
  console.log('PASS: applied/draft separation, halt visibility, validation, confirmation, exact retry, double-click protection, stopped/archive guards and history rendering');
})().catch(e=>{console.error(e);process.exitCode=1;});
