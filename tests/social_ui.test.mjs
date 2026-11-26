import test from 'node:test';
import assert from 'node:assert/strict';
import {createSocial} from '../social.js';

// Small event/element fixtures exercise the controller without browser dependencies.
class Surface {
 constructor(){this.listeners={};this.open=false;this.forms=[];this.html='';}
 addEventListener(type,listener){(this.listeners[type]??=[]).push(listener);}
 async dispatch(type,target){for(const listener of this.listeners[type]||[])await listener({target,preventDefault(){}});}
 set innerHTML(html){
  this.forms.forEach(form=>form.isConnected=false);this.html=html;this.forms=[];
  for(const match of html.matchAll(/<form\b([^>]*)>/g)){
   const attrs=match[1],attributes=new Set([...attrs.matchAll(/\b(data-[\w-]+)/g)].map(row=>row[1]));
   const auth=/data-auth="([^"]+)"/.exec(attrs)?.[1];
   const submit={disabled:false},hint={textContent:''};
   this.forms.push({attributes,dataset:auth?{auth}:{},isConnected:true,values:{},elements:{inviteCode:{required:false}},
    matches(selectors){return selectors.split(',').some(selector=>attributes.has(selector.slice(1,-1)));},
    hasAttribute(name){return attributes.has(name);},reset(){this.values={};},
    querySelector(selector){return selector==='[data-invite-hint]'?hint:submit;},submit,hint});
  }
 }
 get innerHTML(){return this.html;}
 querySelector(selector){
  if(selector==='#social-error')return this.error??={textContent:''};
  const auth=/^\[data-auth="([^"]+)"\]$/.exec(selector);
  if(auth)return this.forms.find(form=>form.dataset.auth===auth[1])||null;
  return null;
 }
 showModal(){this.open=true;}
 close(){this.open=false;for(const listener of this.listeners.close||[])listener();}
}

function fixture(t,respond){
 const old={document:globalThis.document,location:globalThis.location,fetch:globalThis.fetch,FormData:globalThis.FormData};
 const main=new Surface(),dialog=new Surface(),sessions=[],calls=[];
 globalThis.document={createElement:()=>dialog,body:{append(){}}};
 globalThis.location={hash:'#shelf'};
 globalThis.FormData=class extends Map{constructor(form){super(Object.entries(form.values));}};
 globalThis.fetch=async(path,options)=>{
  const body=options?.body?JSON.parse(options.body):undefined;calls.push({path,body});
  return new Response(JSON.stringify(await respond(path,body)),{status:200,headers:{'Content-Type':'application/json'}});
 };
 t.after(()=>Object.assign(globalThis,old));
 const social=createSocial({main,entries:()=>[],notify(){},openDraft(){},onSession:async(session,context)=>sessions.push({session:structuredClone(session),context})});
 const click=action=>dialog.dispatch('click',{dataset:{social:action},closest(){return this;}});
 const submit=async(attribute,values)=>{const form=dialog.forms.find(form=>form.attributes.has(attribute));assert.ok(form,attribute);form.values=values;await dialog.dispatch('submit',form);return form;};
 return {social,dialog,sessions,calls,click,submit};
}

test('signup keeps codes out of session callbacks and clears them after acknowledgement',async t=>{
 const code='synthetic-recovery-code',user={id:'synthetic-user',username:'alice'};
 const f=fixture(t,path=>path==='/api/auth/config'?{signupMode:'invite'}:{user,csrf:'synthetic-csrf',recoveryCodes:[code]});
 f.social.showAuth();await f.click('signup');
 await f.submit('data-auth',{username:'alice',password:'synthetic-password',inviteCode:'synthetic-invite'});
 assert.deepEqual(f.sessions[0].session,{user,csrf:'synthetic-csrf'});
 assert.ok(f.dialog.innerHTML.includes(code));
 await f.submit('data-recovery-confirm',{saved:'on'});
 assert.equal(f.dialog.open,false);
 assert.ok(!f.dialog.innerHTML.includes(code));
});

test('a pending review survives the recovery-code step after signup',async t=>{
 const user={id:'synthetic-user',username:'alice'};
 const f=fixture(t,path=>path==='/api/auth/session'?{user:null,csrf:''}:path==='/api/auth/config'?{signupMode:'open'}:{user,csrf:'synthetic-csrf',recoveryCodes:['synthetic-code']});
 await f.social.share({id:'book',type:'book',title:'A Synthetic Book',rating:4});
 await f.click('signup');await f.submit('data-auth',{username:'alice',password:'synthetic-password'});
 assert.ok(f.dialog.forms.some(form=>form.attributes.has('data-recovery-confirm')));
 await f.submit('data-recovery-confirm',{saved:'on'});
 assert.ok(f.dialog.forms.some(form=>form.attributes.has('data-publish')));
 assert.ok(f.dialog.innerHTML.includes('A Synthetic Book'));
});

test('late signup configuration cannot change a replaced login form',async t=>{
 let finish;
 const config=new Promise(resolve=>finish=resolve);
 const f=fixture(t,path=>path==='/api/auth/config'?config:{});
 f.social.showAuth();await f.click('signup');const signup=f.dialog.forms[0];
 await f.click('login');const loginMarkup=f.dialog.innerHTML;
 finish({signupMode:'closed'});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(signup.isConnected,false);
 assert.equal(f.dialog.innerHTML,loginMarkup);
 assert.equal(f.dialog.forms[0].submit.disabled,false);
});

test('password recovery uses its own endpoint and shows the replacement codes',async t=>{
 const user={id:'synthetic-user',username:'alice'};
 const f=fixture(t,()=>({user,csrf:'synthetic-csrf',recoveryCodes:['synthetic-new-code']}));
 f.social.showAuth();await f.click('recover');
 await f.submit('data-auth',{username:'alice',recoveryCode:'synthetic-old-code',newPassword:'synthetic-new-password'});
 assert.deepEqual(f.calls[0],{path:'/api/auth/recover',body:{username:'alice',recoveryCode:'synthetic-old-code',newPassword:'synthetic-new-password'}});
 assert.ok(f.dialog.innerHTML.includes('synthetic-new-code'));
 assert.ok(!f.dialog.innerHTML.includes('synthetic-old-code'));
 assert.ok(!Object.hasOwn(f.sessions[0].session,'recoveryCodes'));
});

test('account deletion needs typed confirmation and emits the deleted account identity',async t=>{
 const user={id:'synthetic-user',username:'alice'};
 const f=fixture(t,path=>path==='/api/auth/session'?{user,csrf:'synthetic-csrf'}:{deleted:true});
 await f.social.showAccountSettings();
 await f.submit('data-account-delete',{password:'synthetic-password',confirmation:'wrong',understood:'on'});
 assert.ok(!f.calls.some(call=>call.path==='/api/auth/delete'));
 await f.submit('data-account-delete',{password:'synthetic-password',confirmation:'alice',understood:'on'});
 assert.deepEqual(f.calls.find(call=>call.path==='/api/auth/delete').body,{password:'synthetic-password',confirmation:'alice'});
 assert.deepEqual(f.sessions.at(-1),{session:{user:null,csrf:''},context:{reason:'delete',deletedUserId:'synthetic-user'}});
 assert.equal(f.dialog.open,false);
});
