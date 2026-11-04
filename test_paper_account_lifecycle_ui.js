// Focused response-ordering and guard regressions complement real Chrome checks.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8'),nodes=new Map();
const account={initialized:true,session_id:'legacy',revision:0,capital_inr:50000,daily_loss_limit_pct:5,
  daily_baseline_inr:50000,equity_inr:46987.45,allowance_inr:2500,halted:true,trade_count:5,
  positions:0,unresolved_intents:0,funding_obligations:0,sessions:[]};
let handler;
const context={
  $(id){if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,
    classList:{toggle:(name,on)=>{nodes.get(id).hidden=on;}}});return nodes.get(id);},
  esc:String,inr:x=>`INR ${x}`,td:x=>`<td>${x}</td>`,bot:{status:null,busy:false},AbortSignal,URLSearchParams,
  api:(...args)=>handler(...args),localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  window:{confirm:()=>true},crypto:{randomUUID:()=> 'test-request'},Date,
  renderBot:s=>context.renderPaperAccount(s)
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const paperAccountUi ='),source.indexOf('\nfunction chip('))+
  '\nglobalThis.state=paperAccountUi;',context);
const state=context.state,$=context.$;
const catalog=a=>({...a,paper_account:{...a},historical_session:false});
const statusPayload=a=>({mode:'paper',journal_mode:'paper',paper_account:{...a},paper_session_catalog:catalog(a),
  selected_paper_session_id:a.session_id,runner:{running:false},settings:{live_allowed:false,testnet_keys:false}});
const deferred=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});return {promise,resolve,reject};};
(async()=>{
  $('botMode').value='paper';context.initPaperAccount();
  // Missing/failing status route keeps card visible with account evidence, but leaves writes blocked.
  handler=async path=>{
    if(path.includes('/paper/sessions'))return catalog(account);
    if(path==='/api/bot/process')return {state:'stopped'};
    assert.match(path,/mode=paper/);throw Error('Not Found');
  };
  await context.loadBotStatus();
  assert.equal($('paperAccountCard').hidden,false);
  assert.equal($('paperLimitSave').disabled,true);
  assert.equal($('paperSessionNew').disabled,true);
  assert.match($('paperAccountBlockers').textContent,/Status verification failed: Not Found/);
  assert.match($('botRefresh').textContent,/Not Found/);
  $('paperDailyLossInput').value='4';$('paperDailyLossInput').oninput();
  assert.equal($('paperLimitSave').disabled,true); // still blocked while status route is missing!

  // Once status route succeeds and matches selected mode, valid changed limit (4% !== 5%) enables save.
  handler=async path=>{
    if(path.includes('/paper/sessions'))return catalog(account);
    if(path==='/api/bot/process')return {state:'stopped'};
    return statusPayload(account);
  };
  await context.loadBotStatus();
  assert.equal($('paperAccountBlockers').textContent,'');
  assert.equal($('paperLimitSave').disabled,false);
  $('paperDailyLossInput').value='5';$('paperDailyLossInput').oninput();
  assert.equal($('paperLimitSave').disabled,true); // unchanged 5% disables Update Daily Loss Limit
  $('paperDailyLossInput').value='4';$('paperDailyLossInput').oninput();
  assert.equal($('paperLimitSave').disabled,false);

  // Stale poll response with older revision must not overwrite a completed save.
  const updatedAccount={...account,revision:1,daily_loss_limit_pct:4,allowance_inr:2000};
  handler=async (path,opts)=>{
    if(opts?.method==='PUT')return {message:'Updated',paper_account:updatedAccount};
    if(path.includes('/paper/sessions'))return catalog(updatedAccount);
    if(path==='/api/bot/process')return {state:'stopped'};
    return statusPayload(updatedAccount);
  };
  await context.paperAccountAction('daily_limit');
  assert.equal(state.account.daily_loss_limit_pct,4);
  assert.equal(state.account.revision,1);
  // Simulate an older poll response arriving with revision 0 (5%): must be ignored!
  handler=async path=>{
    if(path.includes('/paper/sessions'))return catalog(account);
    if(path==='/api/bot/process')return {state:'stopped'};
    return statusPayload(account);
  };
  await context.loadBotStatus();
  assert.equal(state.account.daily_loss_limit_pct,4);
  assert.equal(state.account.revision,1);
  assert.equal($('paperDailyLossInput').value,4);

  // Newer account and process responses win over earlier overlapping requests.
  const pending=[];handler=(path)=>{const d=deferred();pending.push({path,...d});return d.promise;};
  const old=context.loadPaperAccount(),fresh=context.loadPaperAccount();
  pending[2].resolve(catalog({...account,revision:2,daily_loss_limit_pct:8}));pending[3].resolve({state:'running',pid:77});
  await fresh;pending[0].resolve(catalog(account));pending[1].resolve({state:'stopped'});await old;
  assert.equal(state.account.revision,2);assert.equal(state.process.state,'running');assert.equal($('paperDailyLossInput').value,8);
  assert.match($('paperAccountBlockers').textContent,/Stop Bot Process/);
  // Drafts survive new applied revisions and must not silently acquire a new binding.
  $('paperDailyLossInput').value='9';$('paperDailyLossInput').oninput();
  handler=async p=>p.includes('/paper/sessions')?catalog({...account,revision:3,daily_loss_limit_pct:10}):{state:'stopped'};
  await context.loadPaperAccount();assert.equal($('paperDailyLossInput').value,'9');
  assert.match($('paperAccountBlockers').textContent,/Account changed since this draft/);
  assert.equal($('paperLimitSave').disabled,true);assert.equal(state.draftBinding.revision,2);
  $('paperDraftReload').onclick();assert.equal($('paperDailyLossInput').value,10);assert.equal(state.draftBinding.revision,3);
  // A process verification failure never hides account evidence or permits a save/start.
  handler=async p=>{if(p.includes('/paper/sessions'))return catalog({...account,revision:3});throw Error('permission denied');};
  await context.loadPaperAccount();assert.equal(state.process.state,'unknown');
  assert.equal($('paperAccountCard').hidden,false);assert.equal($('paperLimitSave').disabled,true);assert.equal($('botStart').disabled,true);
  assert.match($('paperAccountBlockers').textContent,/permission denied/);
  // Specific account blockers and stopping progress remain separate from HALTED.
  state.process={state:'stopped'};state.account={...account,revision:3,positions:1,unresolved_intents:2,funding_obligations:3};
  context.renderPaperAccount();
  for(const text of ['1 open position','2 unresolved order','3 outstanding funding'])assert.ok($('paperAccountBlockers').textContent.includes(text));
  state.account={...account,revision:3};state.stopping=true;context.renderPaperAccount();
  assert.match($('paperAccountBlockers').textContent,/waiting for confirmed process termination/);
  assert.equal($('paperLimitSave').disabled,true);assert.match($('paperAccountLifecycle').textContent,/HALTED/);
  state.stopping=false;state.process={state:'stopped'};
  // Responses from a session/mode the user has left must be ignored.
  const delayed=[];handler=()=>{const d=deferred();delayed.push(d);return d.promise;};
  const abandoned=context.loadPaperAccount();$('botMode').value='testnet';
  delayed[0].resolve(catalog({...account,revision:99}));delayed[1].resolve({state:'running'});await abandoned;
  assert.equal(state.account.revision,3);assert.equal(state.process.state,'stopped');
  console.log('PASS: missing status route blocks writes, changed limit enables save, stale poll revision fence, response ordering, stale drafts, unknown/stopping process and specific account guards');
})().catch(e=>{console.error(e);process.exitCode=1;});
