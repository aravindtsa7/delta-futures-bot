// Exercise actual LIVE rendering and start/stop handlers with fake DOM and API.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8'),nodes=new Map(),calls=[];
const context={
  $(id){
    if(!nodes.has(id))nodes.set(id,{value:'',disabled:false,textContent:'',innerHTML:'',dataset:{},
      classList:{toggle(){}},querySelector:selector=>context.$(`${id}:${selector}`)});
    return nodes.get(id);
  },
  document:{querySelectorAll:()=>[]},bot:{busy:false},paperAccountUi:{session:'',processSeq:0},
  mt:{product:null},modeBadge:()=>['LIVE','badge-live'],setBadge(){},renderPaperAccount(){},renderRunnerEvidence(){},
  botMsg(){},toast(){},confirm:()=>true,initPaperAccount(){},loadBotStatus:async()=>{},
  botSummary:()=>'',botSelectedMode:()=>context.$('botMode').value,
  esc:x=>String(x??''),td:x=>`<td>${x}</td>`,kpi:(name,value,detail)=>`${name}: ${value} (${detail})`,
  inr:x=>Number.isFinite(x)?`₹${x.toFixed(2)}`:'unavailable',pct:x=>`${x}%`,cls:()=>'',
  isNum:Number.isFinite,shortTime:x=>x||'',nf2:{format:x=>String(x)},
  api:async(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return {started:true,stopped:true};}
};
vm.createContext(context);
for(const name of ['decimals','fmtPx','renderBot','botAction','bindBot']){
  const start=new RegExp(`(?:async )?function ${name}\\(`).exec(source).index;
  vm.runInContext(source.slice(start,source.indexOf('\n}',start)+2),context);
}
const $=context.$;
const s={mode:'live',journal_mode:'live',settings:{live_allowed:true,testnet_keys:false},
  runner:{running:false},exchange:{connected:true},positions:[
    {symbol:'BTCUSD',side:'LONG',contracts:1,entry_price:68733.5,mark_price:68800,tick_size:.5,sl_price:67534,tp_price:71670},
    {symbol:'1000PEPEUSD',side:'LONG',contracts:17,entry_price:.0045005,mark_price:.00447,tick_size:1e-7}],
  wallet:{realized_equity_inr:4026,total_equity_inr:4000,available_margin_inr:2893,used_margin_inr:1133,
    today_pnl_inr:0,today_pnl_pct:0,daily_loss_limit_pct:5,daily_loss_limit_inr:201.3},
  signals:{},deployed_strategies:[],intents:[],trades:[]};
(async()=>{
  $('botMode').value='live';context.renderBot(s);
  assert.equal($('botStart').disabled,false);assert.equal($('botStop').disabled,true);
  assert.equal($('botStep').disabled,true);
  const option=$('botMode').querySelector('option[value="live"]');
  assert.equal(option.disabled,false);assert.equal(option.textContent,'Live (real money)');
  assert.match($('botKpis').innerHTML,/Delta wallet ₹4026.00/);
  assert.match($('botKpis').innerHTML,/Available margin: ₹2893.00/);
  assert.match($('botKpis').innerHTML,/Open positions: 2/);
  assert.match($('botKpis').innerHTML,/first UTC observation/);
  assert.doesNotMatch($('botKpis').innerHTML,/50000/);
  assert.match($('botPositions').innerHTML,/0.0045005/);
  assert.doesNotMatch($('botPositions').innerHTML,/paper/);
  context.bindBot();$('botStart').onclick();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls[0].path,'/api/bot/start');assert.equal(calls[0].body.mode,'live');
  s.runner.running=true;context.renderBot(s);
  assert.equal($('botStart').disabled,true);assert.equal($('botStop').disabled,false);
  $('botStop').onclick();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls[1].path,'/api/bot/stop');assert.equal(calls[1].body.mode,'live');
  s.runner.running=false;s.exchange={connected:false,error:'Wallet unavailable'};context.renderBot(s);
  assert.equal($('botStart').disabled,true);
  assert.match($('botKpis').innerHTML,/Open positions: unavailable/);
  s.exchange={connected:true};s.settings.live_allowed=false;context.renderBot(s);
  assert.equal($('botStart').disabled,true);assert.equal(option.disabled,true);
  console.log('PASS: real LIVE wallet/positions, precise small prices, gated controls, and LIVE start/stop payloads');
})().catch(error=>{console.error(error);process.exitCode=1;});
