const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../efb_manager/index.html'),'utf8');
const source='function renderHealth(s)'+html.split('function renderHealth(s)')[1].split('let recordTarget=null;')[0];
class Element{
 constructor(text=''){this.textContent=text;this.children=[];this.value='';this.disabled=false;this.selectedOptions=[]}
 append(...nodes){this.children.push(...nodes)}
 replaceChildren(...nodes){this.children=[...nodes]}
 get firstChild(){return this.children[0]}
}
const nodes={},requests=[];
const context={$:id=>nodes[id]||(nodes[id]=new Element()),node:(_tag,_cls,text)=>new Element(text),
 when:v=>'time:'+v,request:async(path,body)=>{requests.push({path,body});return {items:[]}},refresh:async()=>{}};
vm.createContext(context);vm.runInContext(source,context);
context.health={health:{wechat:{status:'ok',last_success:1},telegram:{status:'retrying',retries:2,uncertain:1},archive:{status:'skipped'}}};
vm.runInContext('renderHealth(health)',context);
assert.equal(nodes.healthRows.children.length,3);
assert(nodes.healthRows.children[1].children[1].textContent.includes('正在安全重试'));
context.blacklist=[{key:'["blueset.wechat","uid"]',name:'测试群'}];
vm.runInContext('renderBlacklist(blacklist)',context);
(async()=>{
await nodes.blacklistItems.children[0].children[1].onclick();
assert.equal(requests[0].path,'/api/archive-blacklist');assert.equal(requests[0].body.blocked,false);
context.$('blacklistKey').value='["blueset.wechat","new-user"]';
await nodes.blacklistForm.onsubmit({preventDefault:()=>{}});
assert.equal(requests[1].body.blocked,true);assert.equal(requests[1].body.chat,'["blueset.wechat","new-user"]');
await nodes.testReminder.onclick();assert.equal(requests[2].path,'/api/reminder_test');
assert(!requests.some(r=>r.path==='/api/reminder-settings'));
console.log('Health display, blacklist controls and independent test reminder passed');
})().catch(e=>{console.error(e);process.exitCode=1});
