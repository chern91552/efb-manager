// Exercise the actual page handlers without a browser or external dependencies.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../efb_manager/index.html'),'utf8');
assert(!html.includes('data-page="messages"'));
assert(!html.includes('id="openMessages"'));
const script='//'+html.split('// Hidden history entry:')[1].split("window.addEventListener('hashchange',route);")[0];
const handlers={},timers=new Map();let next=0;
const context={document:{querySelector:()=>({addEventListener:(type,fn)=>handlers[type]=fn})},
 window:{addEventListener:()=>{}},location:{hash:'wechat'},messagesUnlocked:false,route:()=>{},Math,
 setTimeout:fn=>{timers.set(++next,fn);return next},clearTimeout:id=>timers.delete(id)};
vm.createContext(context);vm.runInContext(script,context);
const fire=()=>{const callbacks=[...timers.values()];timers.clear();callbacks.forEach(fn=>fn())};
const down=()=>handlers.pointerdown({button:0,clientX:1,clientY:1});
down();handlers.pointerup();fire();assert.equal(context.location.hash,'wechat');
down();handlers.pointermove({clientX:20,clientY:1});fire();assert.equal(context.location.hash,'wechat');
down();handlers.pointercancel();fire();assert.equal(context.location.hash,'wechat');
down();fire();assert.equal(context.location.hash,'messages');assert.equal(context.messagesUnlocked,true);
handlers.pointerup();let prevented=false;handlers.click({preventDefault:()=>prevented=true});assert(prevented);
context.location.hash='wechat';handlers.keydown({key:'Enter',repeat:false,preventDefault:()=>{}});fire();
handlers.keyup({key:'Enter',preventDefault:()=>{}});assert.equal(context.location.hash,'messages');
console.log('Hidden history entry interactions passed');
