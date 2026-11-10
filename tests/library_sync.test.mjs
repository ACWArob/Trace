import test from 'node:test';
import assert from 'node:assert/strict';
import {createLibrarySync,rebaseLibraryEntries} from '../library_sync.mjs';

const clone=value=>structuredClone(value);
const entry=(id,title=id)=>({id,title,type:'movie',notes:'Personal note',history:['2026-10-01'],attachments:[{id:'kept'}]});
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
function memory(){
 const values=new Map();return {values,fail:false,async get(id){return clone(values.get(id));},async put(id,value){if(this.fail)throw new Error('Disk full');values.set(id,clone(value));}};
}
function server(){
 const accounts=new Map(),calls=[];
 function account(user){if(!accounts.has(user))accounts.set(user,{revision:0,rows:new Map()});return accounts.get(user);}
 function remote(user,id,value){const data=account(user),row={id,entry:clone(value),revision:++data.revision};data.rows.set(id,row);return row;}
 function connect(user){
  const connection={offline:false,unauthorized:false,gate:null,async request(path,body){
   calls.push({user,path,body:clone(body)});
   if(connection.offline)throw new TypeError('Network unavailable');
   if(connection.unauthorized)throw Object.assign(new Error('Session expired'),{status:401});
   const data=account(user);
   if(!body){
    const after=Number(new URL(path,'https://trace.test').searchParams.get('after'));
    const all=[...data.rows.values()].filter(row=>row.revision>after).sort((a,b)=>a.revision-b.revision),changes=all.slice(0,200);
    const result={changes:clone(changes),cursor:changes.at(-1)?.revision??after,hasMore:all.length>200,total:data.rows.size};
    if(connection.gate?.kind==='get'){const gate=connection.gate;connection.gate=null;gate.started.resolve();await gate.release.promise;}
    return result;
   }
   const accepted=[],conflicts=[];
   for(const change of body.changes){const prior=data.rows.get(change.id);if((prior?.revision||0)!==change.baseRevision)conflicts.push(clone(prior));else accepted.push(remote(user,change.id,change.entry));}
   const result={accepted,conflicts};
   if(connection.gate?.kind==='post'){const gate=connection.gate;connection.gate=null;gate.started.resolve();await gate.release.promise;}
   return result;
  }};
  return connection;
 }
 return {connect,remote,calls,account};
}
async function client(connection,storage=memory(),user='user'){
 const changes=[],statuses=[];
 const sync=createLibrarySync({request:connection.request,storage,onChange:value=>changes.push(value),onStatus:value=>statuses.push(value)});
 await sync.setSession({user:{id:user}});await sync.syncNow();return {sync,storage,changes,statuses};
}
function gate(connection,kind){const value={kind,started:deferred(),release:deferred()};connection.gate=value;return value;}

test('two clients merge distinct entries and preserve every entry field',async()=>{
 const backend=server(),a=await client(backend.connect('user')),b=await client(backend.connect('user'));
 await a.sync.replaceEntries([entry('a')]);await a.sync.syncNow();
 await b.sync.replaceEntries([entry('b')]);await b.sync.syncNow();await a.sync.syncNow();
 assert.deepEqual(a.sync.getState().entries.sort((a,b)=>a.id.localeCompare(b.id)),[entry('a'),entry('b')]);
 assert.deepEqual(b.sync.getState().entries.sort((a,b)=>a.id.localeCompare(b.id)),[entry('a'),entry('b')]);
 assert.equal(a.sync.getState().pendingCount,0);
});

test('offline edits persist before callbacks and replay after reopening',async()=>{
 const backend=server(),connection=backend.connect('user'),storage=memory();connection.offline=true;
 const first=await client(connection,storage);
 await first.sync.replaceEntries([entry('offline')]);await first.sync.syncNow();
 assert.equal(storage.values.get('user').pending.offline.baseRevision,0);
 assert.equal(first.statuses.at(-1).state,'offline');first.sync.destroy();
 connection.offline=false;
 const reopened=await client(connection,storage);
 assert.deepEqual(reopened.sync.getState().entries,[entry('offline')]);
 assert.equal(reopened.sync.getState().pendingCount,0);
 assert.deepEqual(backend.account('user').rows.get('offline').entry,entry('offline'));
});

test('conflicts retain both versions across reopen and require explicit resolution',async()=>{
 const backend=server();backend.remote('user','a',entry('a','Original'));
 const connection=backend.connect('user'),a=await client(connection);connection.offline=true;
 await a.sync.replaceEntries([entry('a','Mine')]);await a.sync.syncNow();
 backend.remote('user','a',entry('a','Theirs'));connection.offline=false;await a.sync.syncNow();
 const state=a.sync.getState();assert.equal(state.conflicts[0].localEntry.title,'Mine');assert.equal(state.conflicts[0].remoteEntry.title,'Theirs');
 assert.equal(backend.account('user').rows.get('a').entry.title,'Theirs');
 a.sync.destroy();const reopened=await client(connection,a.storage);
 assert.equal(reopened.sync.getState().conflicts.length,1);
 await reopened.sync.resolveConflict('a','both');await reopened.sync.syncNow();
 const entries=reopened.sync.getState().entries;
 assert.equal(entries.find(x=>x.id==='a').title,'Theirs');
 assert.match(entries.find(x=>x.id!=='a').id,/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/);
 assert.equal(entries.find(x=>x.id!=='a').title,'Mine');assert.equal(reopened.sync.getState().pendingCount,0);
});

test('local and remote conflict choices keep the selected contents',async()=>{
 for(const choice of ['local','remote']){
  const backend=server();backend.remote('user','a',entry('a','Original'));
  const connection=backend.connect('user'),a=await client(connection);connection.offline=true;
  await a.sync.replaceEntries([entry('a','Mine')]);await a.sync.syncNow();backend.remote('user','a',entry('a','Theirs'));
  connection.offline=false;await a.sync.syncNow();await a.sync.resolveConflict('a',choice);await a.sync.syncNow();
  assert.equal(backend.account('user').rows.get('a').entry.title,choice==='local'?'Mine':'Theirs');assert.equal(a.sync.getState().conflicts.length,0);
 }
});

test('deletions sync as tombstones and cannot be resurrected by a stale client',async()=>{
 const backend=server();backend.remote('user','a',entry('a'));
 const a=await client(backend.connect('user')),connection=backend.connect('user'),b=await client(connection);
 connection.offline=true;await b.sync.replaceEntries([entry('a','Stale edit')]);await b.sync.syncNow();
 await a.sync.replaceEntries([]);await a.sync.syncNow();assert.equal(backend.account('user').rows.get('a').entry,null);
 connection.offline=false;await b.sync.syncNow();assert.equal(b.sync.getState().conflicts[0].remoteEntry,null);
 await b.sync.resolveConflict('a','remote');await b.sync.syncNow();assert.deepEqual(b.sync.getState().entries,[]);
});

test('edits during an in-flight push survive and advance their base revision',async()=>{
 const backend=server(),connection=backend.connect('user'),a=await client(connection),hold=gate(connection,'post');
 await a.sync.replaceEntries([entry('a','First')]);await hold.started.promise;
 await a.sync.replaceEntries([entry('a','Second')]);assert.equal(a.sync.getState().entries[0].title,'Second');
 hold.release.resolve();await a.sync.syncNow();
 assert.equal(a.sync.getState().entries[0].title,'Second');assert.equal(a.sync.getState().conflicts.length,0);
 const pushes=backend.calls.filter(x=>x.body);assert.equal(pushes[1].body.changes[0].baseRevision,1);
 assert.equal(backend.account('user').rows.get('a').entry.title,'Second');
});

test('late responses after an account switch cannot write or publish into the new account',async()=>{
 const backend=server(),connection=backend.connect('first'),a=await client(connection,memory(),'first'),hold=gate(connection,'post');
 await a.sync.replaceEntries([entry('private')]);await hold.started.promise;
 connection.offline=true;await a.sync.setSession({user:{id:'second'}});await a.sync.syncNow();
 const published=a.changes.length;hold.release.resolve();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(a.changes.length,published);assert.equal(a.sync.getState().userId,'second');assert.deepEqual(a.sync.getState().entries,[]);
 assert.equal(a.storage.values.has('second'),false);assert.equal(a.storage.values.get('first').pending.private.baseRevision,0);
});

test('storage failure leaves the prior durable state and emits no successful change',async()=>{
 const backend=server(),a=await client(backend.connect('user'));a.storage.fail=true;const count=a.changes.length;
 await assert.rejects(a.sync.replaceEntries([entry('unsaved')]),/Disk full/);
 assert.equal(a.changes.length,count);assert.deepEqual(a.sync.getState().entries,[]);assert.equal(a.statuses.at(-1).state,'error');
 assert.equal(backend.calls.filter(x=>x.body).length,0);
});

test('large entries respect UTF-8 byte and count limits without dropping fields',async()=>{
 const backend=server(),connection=backend.connect('user'),a=await client(connection);
 const rows=Array.from({length:102},(_,index)=>entry(String(index)));rows[0].notes='💿'.repeat(1400000);rows[1].notes=rows[0].notes;
 await a.sync.replaceEntries(rows);await a.sync.syncNow();
 const pushes=backend.calls.filter(x=>x.body);
 assert.ok(pushes.length>=3);assert.ok(pushes.every(x=>x.body.changes.length<=100));
 assert.ok(pushes.every(x=>new TextEncoder().encode(JSON.stringify(x.body)).length<=9_500_000));
 assert.equal(backend.account('user').rows.size,102);assert.equal(a.sync.getState().pendingCount,0);
 assert.equal(backend.account('user').rows.get('0').entry.notes,rows[0].notes);
});

test('authentication failure pauses requests until a new session is supplied',async()=>{
 const backend=server(),connection=backend.connect('user'),a=await client(connection);connection.unauthorized=true;
 await a.sync.replaceEntries([entry('a')]);await a.sync.syncNow();assert.equal(a.statuses.at(-1).state,'auth-required');
 const count=backend.calls.length;connection.unauthorized=false;await a.sync.syncNow();assert.equal(backend.calls.length,count);
 await a.sync.setSession({user:{id:'user'}});await a.sync.syncNow();assert.equal(a.sync.getState().pendingCount,0);
});

test('sync storage errors are reported as errors and pending edits remain recoverable',async()=>{
 const backend=server(),connection=backend.connect('user'),a=await client(connection);
 connection.offline=true;await a.sync.replaceEntries([entry('a')]);await a.sync.syncNow();
 a.storage.fail=true;connection.offline=false;await a.sync.syncNow();
 assert.equal(a.statuses.at(-1).state,'error');assert.equal(a.sync.getState().pendingCount,1);
 a.storage.fail=false;await a.sync.syncNow();assert.equal(a.sync.getState().pendingCount,0);
});

test('account changes are published only after their storage transaction completes',async()=>{
 const backend=server(),connection=backend.connect('user'),storage=memory(),a=await client(connection,storage);
 connection.offline=true;const saved=deferred(),started=deferred(),original=storage.put.bind(storage);
 storage.put=async(id,data)=>{started.resolve();await saved.promise;return original(id,data);};
 const before=a.changes.length,edit=a.sync.replaceEntries([entry('a')]);await started.promise;
 assert.equal(a.changes.length,before);assert.deepEqual(a.sync.getState().entries,[]);
 saved.resolve();await edit;await a.sync.syncNow();assert.deepEqual(a.changes.at(-1).entries,[entry('a')]);
});

test('entry IDs that resemble object properties retain their IDs and start at revision zero',async()=>{
 const backend=server(),a=await client(backend.connect('user'));
 const rows=['__proto__','constructor','toString'].map(id=>entry(id));
 await a.sync.replaceEntries(rows);await a.sync.syncNow();
 assert.deepEqual(a.sync.getState().entries,rows);assert.equal(a.sync.getState().pendingCount,0);
 assert.ok(backend.calls.filter(call=>call.body).flatMap(call=>call.body.changes).every(change=>change.baseRevision===0));
});

test('UI save rebasing preserves two rapid independent changes from one old snapshot',()=>{
 const before=[entry('a'),entry('b')];
 const first=[{...before[0],rating:4.5},before[1]];
 const second=[before[0],{...before[1],notes:'Second edit'}];
 const saved=rebaseLibraryEntries(before,second,rebaseLibraryEntries(before,first,before));
 assert.equal(saved[0].rating,4.5);assert.equal(saved[1].notes,'Second edit');
 const noteOnly=[{...before[0],notes:'New note'},before[1]];
 const sameItem=rebaseLibraryEntries(before,noteOnly,first);
 assert.equal(sameItem[0].rating,4.5);assert.equal(sameItem[0].notes,'New note');
 assert.throws(()=>rebaseLibraryEntries(before,noteOnly,[before[1]]),/removed/);
});

test('two offline tabs sharing atomic account storage preserve both queues',async()=>{
 const store=memory();let tail=Promise.resolve();
 store.update=(id,change)=>{const job=tail.then(()=>{if(store.fail)throw Error('Disk full');const result=change(clone(store.values.get(id)));if(result.write)store.values.set(id,clone(result.data));return clone(result.data);});tail=job.catch(()=>{});return job;};
 const remote=server(),a=remote.connect('shared'),b=remote.connect('shared');
 a.offline=true;b.offline=true;
 const first=createLibrarySync({request:a.request,storage:store}),second=createLibrarySync({request:b.request,storage:store});
 await first.setSession({user:{id:'shared'}});await first.syncNow();await second.setSession({user:{id:'shared'}});await second.syncNow();
 await Promise.all([first.replaceEntries([entry('a')]),second.replaceEntries([entry('b')])]);
 const reopened=createLibrarySync({request:a.request,storage:store});await reopened.setSession({user:{id:'shared'}});
 assert.deepEqual(reopened.getState().entries.map(x=>x.id).sort(),['a','b']);assert.equal(reopened.getState().pendingCount,2);await reopened.syncNow();
 a.offline=false;await reopened.syncNow();assert.equal(reopened.getState().pendingCount,0);
 first.destroy();second.destroy();reopened.destroy();
});
