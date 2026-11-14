// Run the actual settings handlers with isolated DOM/API state; never touch an account.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8'),nodes=new Map(),storage=new Map(),calls=[];
let handler;
const context={
  $(id){if(!nodes.has(id)){const classes=new Set();nodes.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,
    classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)}});}return nodes.get(id);},
  esc:String,td:x=>`<td>${x}</td>`,inr:x=>`₹${Number(x).toFixed(2)}`,bot:{status:null,busy:false},
  localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  api:async(path,opts)=>{calls.push({path,opts});return handler(path,opts);},AbortSignal,URLSearchParams,
  crypto:{randomUUID:()=>`unique-request-${calls.length}`},window:{confirm:()=>{throw Error('Unexpected capital reset');}},
  renderBot:s=>context.renderPaperAccount(s)
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const paperAccountUi ='),source.indexOf('\nfunction chip('))+
  '\nglobalThis.state=paperAccountUi;',context);
const $=context.$,state=context.state;
(async()=>{
  for(const mode of ['paper','live','testnet']){
    $('botMode').value=mode;
    let a={mode,initialized:true,session_id:'legacy',revision:0,capital_inr:50000,daily_loss_limit_pct:5,
      daily_baseline_inr:4817.10,equity_inr:4800,baseline_basis:'UTC opening',positions:3,
      unresolved_intents:2,funding_obligations:1,halted:false,allowance_inr:240.855};
    Object.assign(state,{account:null,catalog:null,session:'',dirty:false,draftKey:null,draftBinding:null,
      request:null,minRevision:null,minRevisionSession:null,accountError:'',statusError:''});
    handler=async(path,opts)=>{
      if(opts?.method==='PUT'){
        const body=JSON.parse(opts.body);
        assert.equal(body.mode,mode);assert.equal(body.capital_inr,undefined);
        assert.equal(body.expected_revision,a.revision);
        a={...a,revision:a.revision+1,daily_loss_limit_pct:body.daily_loss_limit_pct,
          allowance_inr:a.daily_baseline_inr*body.daily_loss_limit_pct/100};
        return {message:'Saved',account_settings:a};
      }
      if(path==='/api/bot/process')return {state:'running',pid:123};
      if(path.includes('/settings')||path.includes('/paper/sessions'))return {...a,account_settings:a};
      assert.ok(!path.includes('paper_session_id=')||mode==='paper');
      return {mode,journal_mode:mode,runner:{running:true},paper_account:mode==='paper'?a:null};
    };
    context.initPaperAccount();await context.loadBotStatus();
    assert.equal($('paperAccountCard').classList.contains('hidden'),false);
    assert.equal($('paperCapitalField').classList.contains('hidden'),mode!=='paper');
    assert.equal($('paperSessionNew').classList.contains('hidden'),mode!=='paper');
    assert.equal($('paperSessionNew').disabled,true);
    assert.equal($('dailyLossAllowance').textContent,
      `5% of ₹4817.10 UTC opening balance = ${context.inr(4817.10*5/100)} max daily loss`);
    for(const preset of [4,5,10,25,50]){
      $('dailyPreset'+preset).onclick();
      assert.equal(Number($('paperDailyLossInput').value),preset);
      assert.equal($('dailyPreset'+preset).classList.contains('active'),true);
      if(preset===10)assert.match($('dailyLossAllowance').textContent,/₹481.71 max daily loss/);
      if(preset===50)assert.match($('dailyLossAllowance').textContent,/₹2408.55 max daily loss/);
    }
    for(const limit of [5,10,50,5,.5,95]){
      $('paperDailyLossInput').value=String(limit);$('paperDailyLossInput').oninput();
      assert.equal($('paperLimitSave').disabled,limit===a.daily_loss_limit_pct);
      if(limit!==a.daily_loss_limit_pct){
        await context.paperAccountAction('daily_limit');
        assert.equal(a.daily_loss_limit_pct,limit);
        assert.equal(calls.filter(c=>c.opts?.method==='PUT').at(-1).path,
          mode==='paper'?'/api/bot/paper/account/daily-loss':'/api/bot/settings');
      }
    }
    for(const limit of ['0.49','95.01','Infinity']){
      $('paperDailyLossInput').value=limit;$('paperDailyLossInput').oninput();
      assert.equal($('paperLimitSave').disabled,true);
    }
    // An interrupted request stays mode-bound and retries the exact body.
    const normalHandler=handler;
    handler=async(path,opts)=>{if(opts?.method==='PUT')throw Error('connection lost');return normalHandler(path,opts);};
    $('dailyPreset10').onclick();await context.paperAccountAction('daily_limit');
    const uncertain=calls.filter(c=>c.opts?.method==='PUT').at(-1).opts.body;
    assert.ok(storage.has(context.accountRequestKey(mode)));
    handler=normalHandler;await context.paperAccountAction(null,true);
    assert.equal(calls.filter(c=>c.opts?.method==='PUT').at(-1).opts.body,uncertain);
    assert.equal(storage.has(context.accountRequestKey(mode)),false);
  }
  assert.ok(calls.every(c=>!c.path.includes('/start')&&!c.path.includes('/orders')));
  console.log('PASS: PAPER/LIVE/testnet presets, UTC rupee preview, running/open-account edits, ranges, mode isolation and exact retries');
})().catch(e=>{console.error(e);process.exitCode=1;});
