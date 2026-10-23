const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../efb_manager/index.html'),'utf8');
const source='let recordTarget=null;'+html.split('let recordTarget=null;')[1].split('function debounce(fn)')[0];
const nodes={},requests=[];let accepted=false;
const context={$:id=>nodes[id]||(nodes[id]={hidden:true,style:{},offsetWidth:200,offsetHeight:50,contains:()=>true}),
 innerWidth:400,innerHeight:300,document:{addEventListener:()=>{}},window:{addEventListener:()=>{}},
 confirm:()=>accepted,request:async(path,body)=>requests.push({path,body}),refresh:async()=>{},refreshChats:async()=>{},notice:()=>{},
 messageEpoch:0,conversationEpoch:0,chatKey:'different-chat',Math};
vm.createContext(context);vm.runInContext(source,context);
let handler;context.element={addEventListener:(_,fn)=>handler=fn};
context.target={scope:'message',chat:'["blueset.wechat","one"]',id:3,captured_at:123};
vm.runInContext('bindRecordMenu(element,target)',context);
(async()=>{
handler({preventDefault:()=>{},clientX:399,clientY:299});
assert.equal(nodes.recordMenu.style.left,'192px');
await nodes.deleteRecord.onclick();assert.equal(requests.length,0);
accepted=true;handler({preventDefault:()=>{},clientX:1,clientY:1});
await nodes.deleteRecord.onclick();assert.equal(requests[0].path,'/api/delete-records');assert.equal(requests[0].body.id,3);
context.target={scope:'conversation',chat:'["blueset.wechat","one"]',name:'测试会话'};
vm.runInContext('bindRecordMenu(element,target)',context);handler({preventDefault:()=>{},clientX:1,clientY:1});
assert.equal(nodes.deleteRecord.textContent,'删除此会话全部记录');
await nodes.deleteRecord.onclick();assert.equal(requests[1].body.scope,'conversation');
console.log('Record deletion menu and confirmation passed');
})().catch(e=>{console.error(e);process.exitCode=1});
