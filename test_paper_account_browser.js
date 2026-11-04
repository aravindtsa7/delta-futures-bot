// Actual headless Chrome: served HTML/CSS/JS + isolated production API/journal.
// node test_paper_account_browser.js [--empty | --real-readonly]
// The real-readonly variant never submits an operation or starts/stops a runner.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process'),net=require('node:net');
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
async function main(){
  const real=process.argv.includes('--real-readonly'),empty=process.argv.includes('--empty');
  const evidence=fs.mkdtempSync(path.join(os.tmpdir(),'paper-browser-evidence-'));
  const profile=path.join(evidence,'chrome-profile');
  const port=real?8001:await freePort(),base='http://127.0.0.1:'+port;
  let fixture,chrome,ws,send,evaluate;const errors=[],snapshots=[];let requests=0;
  try {
    if(!real){
      fixture=spawn(path.join(__dirname,'.venv/Scripts/python.exe'),['-B','test_paper_account_browser_server.py',String(port),...(empty?['--empty']:[])],{cwd:__dirname,windowsHide:true,stdio:['ignore','ignore','pipe']});
      fixture.stderr.on('data',chunk=>process.stderr.write(chunk));
      for(let i=0;i<150;i++){try{if((await fetch(base+'/api/bot/process')).ok)break;}catch{}if(i===149)throw Error('Fixture API did not start');await wait(100);}
    }
    const browser=['C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(p=>fs.existsSync(p));
    if(!browser)throw Error('No installed Chromium browser; rendered verification NOT VERIFIED');
    chrome=spawn(browser,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
    let debugPort;
    for(let i=0;i<100;i++){try{debugPort=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];break;}catch{}await wait(100);}
    if(!debugPort)throw Error('Browser debugger unavailable');
    const pages=await(await fetch('http://127.0.0.1:'+debugPort+'/json/list')).json();
    ws=new WebSocket(pages.find(x=>x.type==='page').webSocketDebuggerUrl);
    await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j;});
    let id=0;const pending=new Map();
    send=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
    ws.onmessage=event=>{
      const x=JSON.parse(event.data);
      if(x.id){const p=pending.get(x.id);pending.delete(x.id);if(x.error)p.reject(Error(JSON.stringify(x.error)));else p.resolve(x.result);}
      else if(x.method==='Runtime.exceptionThrown')errors.push(x.params.exceptionDetails.exception?.description||x.params.exceptionDetails.text);
      else if(x.method==='Page.javascriptDialogOpening'){
        if(real)send('Page.handleJavaScriptDialog',{accept:false});
        else send('Page.handleJavaScriptDialog',{accept:true});
      } else if(x.method==='Network.responseReceived'&&x.params.response.url.includes('/api/bot/paper/sessions'))requests++;
    };
    evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
    const until=async(expression,label)=>{for(let i=0;i<150;i++){
      try{if(await evaluate(`typeof paperAccountUi !== 'undefined' && (${expression})`))return;}
      catch(e){if(!/Execution context was destroyed|Cannot find context/.test(e.message))throw e;}
      await wait(100);
    }throw Error('Timed out: '+label);};
    const click=async id=>evaluate(`document.getElementById(${JSON.stringify(id)}).click()`);
    const input=async(id,value)=>evaluate(`(()=>{const x=document.getElementById(${JSON.stringify(id)});x.focus();x.value=${JSON.stringify(String(value))};x.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    const snapshot=async label=>{
      const value=await evaluate(`({selected:document.getElementById('botMode').value,visible:!!document.getElementById('paperAccountCard').getClientRects().length,lifecycle:document.getElementById('paperAccountLifecycle').textContent,applied:document.getElementById('paperAccountApplied').textContent,blockers:document.getElementById('paperAccountBlockers').textContent,error:document.getElementById('paperAccountError').textContent,saveDisabled:document.getElementById('paperLimitSave').disabled,newDisabled:document.getElementById('paperSessionNew').disabled,capital:document.getElementById('paperCapitalInput').value,limit:document.getElementById('paperDailyLossInput').value,refresh:document.getElementById('botRefresh').textContent})`);
      snapshots.push({label,...value});console.log(label,JSON.stringify(value));
      const image=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(evidence,label+'.png'),Buffer.from(image.data,'base64'));return value;
    };
    await send('Page.enable');await send('Runtime.enable');await send('Network.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    await send('Network.setBlockedURLs',{urls:['*unpkg.com*']});
    await send('Page.navigate',{url:base+'/#bot'});
    await until(`document.getElementById('paperAccountLifecycle')?.textContent.includes('Web/API: available')`,'independent account load');
    await until(`paperAccountUi.process?.state !== undefined`,'process verification');
    if(real){
      await until(`paperAccountUi.process?.state==='stopped'`,'real process stopped');
      let s=await snapshot('real-stopped');assert.equal(s.visible,true);assert.match(s.applied,/46,987.45/);assert.match(s.applied,/HALTED/);assert.equal(s.saveDisabled,false);
      await input('paperCapitalInput',75000);await input('paperDailyLossInput',6);const count=requests;
      await wait(11000);s=await snapshot('real-drafts-after-polls');assert.equal(s.capital,'75000');assert.equal(s.limit,'6');assert.ok(requests>=count+2);
      await send('Page.reload',{ignoreCache:true});await until(`paperAccountUi.process?.state==='stopped' && paperAccountUi.account?.initialized`,'hard refresh');
      s=await snapshot('real-hard-refresh');assert.equal(s.visible,true);assert.equal(s.limit,'5');assert.equal(s.capital,'50000');
    }else if(empty){
      let s=await snapshot('first-launch');assert.equal(s.visible,true);assert.match(s.applied,/No persisted paper wallet/);assert.equal(s.saveDisabled,true);
      await input('paperCapitalInput',75000);await input('paperDailyLossInput',7.5);await until(`!document.getElementById('paperSessionNew').disabled`,'initialization eligibility');
      await click('paperSessionNew');await until(`paperAccountUi.account?.initialized && paperAccountUi.account?.capital_inr===75000`,'first account initialized stopped');
      s=await snapshot('initialized-stopped');assert.match(s.lifecycle,/STOPPED/);assert.equal(s.saveDisabled,false);
    }else{
      await until(`paperAccountUi.process?.state==='running'`,'fixture running');
      let s=await snapshot('halted-process-running');assert.equal(s.visible,true);assert.match(s.applied,/HALTED/);assert.equal(s.saveDisabled,true);assert.match(s.blockers,/Stop Bot Process/);
      await click('botStop');await until(`paperAccountUi.process?.state==='stopped' && !document.getElementById('paperLimitSave').disabled`,'normal stop flow');
      s=await snapshot('halted-process-stopped');assert.equal(s.visible,true);assert.match(s.applied,/HALTED/);
      await input('paperCapitalInput',90000);await input('paperDailyLossInput',8);const count=requests;await wait(11000);
      s=await snapshot('drafts-after-polls');assert.equal(s.capital,'90000');assert.equal(s.limit,'8');assert.ok(requests>=count+2);
      await click('paperLimitSave');await until(`paperAccountUi.account?.daily_loss_limit_pct===8`,'daily limit saved');
      assert.equal(await evaluate(`paperAccountUi.account.halted`),true);
      await send('Page.reload',{ignoreCache:true});await until(`paperAccountUi.account?.daily_loss_limit_pct===8 && paperAccountUi.process?.state==='stopped'`,'persistence after hard refresh');
      s=await snapshot('saved-limit-reloaded');assert.match(s.applied,/HALTED/);assert.equal(s.limit,'8');assert.equal(s.capital,'50000');
      await send('Network.setBlockedURLs',{urls:['*unpkg.com*','*/api/bot/status*']});await send('Page.reload',{ignoreCache:true});
      await until(`paperAccountUi.process?.state==='stopped' && paperAccountUi.account?.initialized`,'settings without runtime status');
      s=await snapshot('runtime-status-unavailable');assert.equal(s.visible,true);assert.equal(s.saveDisabled,false);
      await send('Network.setBlockedURLs',{urls:['*unpkg.com*','*/api/bot/process*','*/api/bot/status*']});
      await until(`paperAccountUi.process?.state==='unknown'`,'unknown process');
      s=await snapshot('process-unknown');assert.equal(s.visible,true);assert.equal(s.saveDisabled,true);assert.match(s.blockers,/Process verification failed/);
      await send('Network.setBlockedURLs',{urls:['*unpkg.com*']});await until(`paperAccountUi.process?.state==='stopped'`,'process recovery');
      await input('paperCapitalInput',100000);await input('paperDailyLossInput',10);await click('paperSessionNew');
      await until(`paperAccountUi.account?.capital_inr===100000 && !paperAccountUi.busy && paperAccountUi.process?.state==='stopped'`,'new session');
      s=await snapshot('new-session-stopped');assert.match(s.lifecycle,/STOPPED/);assert.doesNotMatch(s.applied,/HALTED/);
      await evaluate(`document.getElementById('paperSessionSelect').value='legacy';document.getElementById('paperSessionSelect').dispatchEvent(new Event('change',{bubbles:true}));`);
      await until(`paperAccountUi.catalog?.historical_session && bot.status?.historical_session`,'archive load');
      s=await snapshot('archived-session');assert.equal(s.visible,true);assert.equal(s.saveDisabled,true);assert.match(s.applied,/ARCHIVED.*HALTED/);
      assert.equal(await evaluate(`document.querySelectorAll('#botTrades tbody tr').length`),5);
      assert.match(await evaluate(`document.getElementById('paperEvalHistory').textContent`),/fixture/);
      await evaluate(`document.getElementById('paperSessionSelect').value='';document.getElementById('paperSessionSelect').dispatchEvent(new Event('change',{bubbles:true}));`);
      await until(`!paperAccountUi.catalog?.historical_session && !bot.status?.historical_session`,'active account reload');
      await input('paperDailyLossInput',11);
      const current=await(await fetch(base+'/api/bot/paper/sessions')).json();
      const update=await fetch(base+'/api/bot/paper/account/daily-loss',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({daily_loss_limit_pct:12,expected_session_id:current.session_id,expected_revision:current.revision,request_id:'browser-external-update'})});assert.equal(update.status,200);
      await until(`paperAccountUi.account?.daily_loss_limit_pct===12`,'new account revision');
      s=await snapshot('stale-draft-blocked');assert.equal(s.limit,'11');assert.equal(s.saveDisabled,true);assert.match(s.blockers,/Account changed since this draft/);
      await click('paperDraftReload');s=await snapshot('applied-values-reloaded');assert.equal(s.limit,'12');assert.equal(s.saveDisabled,false);
    }
    assert.deepEqual(errors,[]);console.log('PASS: actual Chromium rendered lifecycle checks; evidence:',evidence);
  }finally{
    fs.writeFileSync(path.join(evidence,'results.json'),JSON.stringify({real,empty,snapshots,errors},null,2));
    if(send)try{await send('Browser.close');}catch{}ws?.close();
    if(fixture){try{await fetch(base+'/__test/shutdown',{method:'POST'});}catch{}await new Promise(r=>{if(fixture.exitCode!==null)return r();fixture.once('exit',r);});}
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
