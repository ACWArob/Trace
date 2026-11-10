const MAX_BYTES=9_500_000; // Below the server's decimal 10 MB request limit, including envelope.
const copy=value=>structuredClone(value);
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const empty=()=>({entries:{},revisions:{},pending:{},conflicts:{},cursor:0,sequence:0});
const own=(object,key)=>Object.hasOwn(object,key);
const value=(object,key,fallback)=>own(object,key)?object[key]:fallback;
const assign=(object,key,value)=>Object.defineProperty(object,key,{value,writable:true,enumerable:true,configurable:true});

// Rebase the user's changed fields onto the latest durable snapshot. Two UI
// actions may start before either IndexedDB transaction has completed.
export function rebaseLibraryEntries(base,desired,current){
 const before=new Map(base.map(row=>[row.id,row])),after=new Map(desired.map(row=>[row.id,row]));
 const result=new Map(current.map(row=>[row.id,copy(row)]));
 for(const id of before.keys())if(!after.has(id))result.delete(id);
 for(const [id,row] of after){
  const original=before.get(id);if(same(original,row))continue;
  if(!original){result.set(id,copy(row));continue;}
  if(!result.has(id))throw new Error('This entry was removed while you were editing. Open the latest library before saving.');
  const merged=result.get(id);
  for(const key of new Set([...Object.keys(original),...Object.keys(row)]))if(!same(original[key],row[key])){
   if(own(row,key))assign(merged,key,copy(row[key]));else delete merged[key];
  }
 }
 return [...result.values()];
}

// Each account is stored in one transaction, including its unsent edits and conflicts.
export function createIndexedDBStorage(indexedDB=globalThis.indexedDB){
 let database;
 function open(){
  if(!database)database=new Promise((resolve,reject)=>{
   if(!indexedDB){reject(new Error('Account storage is unavailable in this browser.'));return;}
   const request=indexedDB.open('trace-account-library',1);
   request.onupgradeneeded=()=>request.result.createObjectStore('accounts');
   request.onsuccess=()=>resolve(request.result);
   request.onerror=()=>reject(request.error||new Error('Could not open account storage.'));
   request.onblocked=()=>reject(new Error('Account storage is blocked by another browser tab.'));
  });
  return database;
 }
 async function transact(mode,key,value){
  const db=await open();
  return new Promise((resolve,reject)=>{
   const transaction=db.transaction('accounts',mode),store=transaction.objectStore('accounts');
   const request=mode==='readonly'?store.get(key):store.put(value,key);
   transaction.oncomplete=()=>resolve(request.result);
   transaction.onabort=()=>reject(transaction.error||request.error||new Error('Could not save account storage.'));
   transaction.onerror=()=>{};
  });
 }
 async function update(key,change){
  const db=await open();return new Promise((resolve,reject)=>{
   const transaction=db.transaction('accounts','readwrite'),store=transaction.objectStore('accounts');let result,failure;
   const read=store.get(key);read.onsuccess=()=>{try{const next=change(read.result);result=next.data;if(next.write)store.put(next.data,key);}catch(error){failure=error;transaction.abort();}};
   transaction.oncomplete=()=>resolve(result);transaction.onabort=()=>reject(failure||transaction.error||read.error||new Error('Could not save account storage.'));transaction.onerror=()=>{};
  });
 }
 return {get:key=>transact('readonly',key),put:(key,value)=>transact('readwrite',key,value),update};
}

export function createLibrarySync({request,onChange=()=>{},onStatus=()=>{},storage=createIndexedDBStorage()}){
 let current=null,epoch=0,storageTail=Promise.resolve(),destroyed=false;
 const active=context=>!destroyed&&current===context&&context.epoch===epoch;
 function snapshot(context=current){
  return copy({userId:context?.userId??null,entries:Object.values(context?.data.entries||{}),pendingCount:Object.keys(context?.data.pending||{}).length,
   conflicts:Object.entries(context?.data.conflicts||{}).map(([id,remote])=>({id,localEntry:value(context.data.entries,id,null),remoteEntry:remote.entry,remoteRevision:remote.revision})),cursor:context?.data.cursor||0});
 }
 function status(context,state,error){
  if(!active(context))return;
  const data=snapshot(context);
  onStatus({userId:context.userId,state,pendingCount:data.pendingCount,conflictCount:data.conflicts.length,...(error?{error:error.message||String(error)}:{})});
 }
 function publish(context){if(active(context))onChange({userId:context.userId,entries:snapshot(context).entries});}
 function stored(action){
  const job=storageTail.then(action).catch(error=>{
   const failure=new Error(error.message||String(error),{cause:error});failure.storageFailure=true;throw failure;
  });
  storageTail=job.catch(()=>{});return job;
 }
 function mutate(context,change){
  const job=context.tail.then(async()=>{
   if(!active(context))return false;
   if(storage.update){
    let changed=false;
    const draft=await stored(()=>storage.update(context.userId,latest=>{
     const data=copy(latest||context.data);if(!active(context))return {data,write:false};
     changed=change(data)!==false;return {data,write:changed};
    }));
    if(!active(context))return false;
    const different=!same(context.data,draft);context.data=draft;if(different)publish(context);return changed;
   }
   const draft=copy(context.data);
   if(change(draft)===false)return false;
   const saved=await stored(async()=>{if(!active(context))return false;await storage.put(context.userId,draft);return true;});
   if(!saved||!active(context))return false;
   context.data=draft;publish(context);return true;
  });
  context.tail=job.catch(()=>{});return job;
 }
 function settle(context){status(context,Object.keys(context.data.conflicts).length?'conflict':Object.keys(context.data.pending).length?'offline':'synced');}
 function queue(context,data,id,entry,baseRevision=value(data.revisions,id,0)){
  if(entry===null)delete data.entries[id];else assign(data.entries,id,copy(entry));
  assign(data.pending,id,{baseRevision,version:++data.sequence});
 }
 function applyRemote(data,remote){
  if(remote.entry===null)delete data.entries[remote.id];else assign(data.entries,remote.id,copy(remote.entry));
  assign(data.revisions,remote.id,remote.revision);
 }
 function rememberRemote(data,remote){
  const known=value(data.conflicts,remote.id,null);
  if(!known||remote.revision>known.revision)assign(data.conflicts,remote.id,{entry:copy(remote.entry),revision:remote.revision});
 }
 async function pull(context){
  let more=true;
  while(active(context)&&more){
   const after=context.data.cursor;
   const result=await request(`/api/library?after=${after}&limit=200`);
   if(!active(context))return;
   await mutate(context,data=>{
    for(const remote of result.changes){
     if(remote.revision<=value(data.revisions,remote.id,0))continue;
     if(own(data.pending,remote.id)){
      if(own(data.conflicts,remote.id))rememberRemote(data,remote);
      else if(same(value(data.entries,remote.id,null),remote.entry)){
       applyRemote(data,remote);delete data.pending[remote.id];
      }else if(remote.revision>data.pending[remote.id].baseRevision)rememberRemote(data,remote);
     }else applyRemote(data,remote);
    }
    data.cursor=Math.max(data.cursor,result.cursor);
   });
   more=result.hasMore;
   if(more&&result.cursor<=after)throw new Error('The library server returned an invalid cursor.');
  }
 }
 function batch(context){
  const changes=[],versions={};let bytes=new TextEncoder().encode('{"changes":[]}').length;
  for(const [id,pending] of Object.entries(context.data.pending)){
   if(own(context.data.conflicts,id))continue;
   const change={id,entry:value(context.data.entries,id,null),baseRevision:pending.baseRevision};
   const size=new TextEncoder().encode(JSON.stringify(change)).length+(changes.length?1:0);
   if(bytes+size>MAX_BYTES){if(!changes.length)throw new Error('An entry is too large to sync (10 MB maximum).');break;}
   changes.push(copy(change));assign(versions,id,pending.version);bytes+=size;
   if(changes.length===100)break;
  }
  return {changes,versions};
 }
 async function push(context){
  while(active(context)){
   await context.tail;
   if(!active(context))return;
   const {changes,versions}=batch(context);
   if(!changes.length)return;
   const result=await request('/api/library/sync',{changes});
   if(!active(context))return;
   await mutate(context,data=>{
    for(const remote of result.accepted){
     if(remote.revision<value(data.revisions,remote.id,0))continue;
     const pending=value(data.pending,remote.id,null);
     assign(data.revisions,remote.id,remote.revision);
     if(pending?.version===versions[remote.id]){
      applyRemote(data,remote);delete data.pending[remote.id];delete data.conflicts[remote.id];
     }else if(pending)pending.baseRevision=remote.revision;
    }
    for(const remote of result.conflicts)rememberRemote(data,remote);
   });
   if(!result.accepted.length&&!result.conflicts.length)throw new Error('The library server did not acknowledge the changes.');
  }
 }
 function syncNow(){
  const context=current;
  if(!context||!context.ready||context.paused||destroyed)return Promise.resolve(snapshot());
  if(context.sync)return context.sync;
  status(context,'syncing');
  context.sync=(async()=>{
   try{
    await context.tail;
    await pull(context);
    if(active(context))await push(context);
    if(active(context))await pull(context);
    // Edits queued during the final pull must also reach the server.
    while(active(context)&&Object.keys(context.data.pending).some(id=>!own(context.data.conflicts,id)))await push(context);
    if(active(context))settle(context);
   }catch(error){
    if(active(context)){
     if(error.status===401||error.status===403){context.paused=true;status(context,'auth-required',error);}
     else status(context,error.status||error.storageFailure?'error':'offline',error);
    }
   }finally{context.sync=null;}
   return snapshot();
  })();
  return context.sync;
 }
 async function setSession(session){
  const userId=session?.user?.id??null;
  ++epoch;
  if(userId===null){current=null;onChange({userId:null,entries:[]});onStatus({userId:null,state:'synced',pendingCount:0,conflictCount:0});return snapshot();}
  const context={userId,epoch,data:empty(),tail:Promise.resolve(),ready:false,paused:false,sync:null};
  current=context;status(context,'loading');
  try{
   const saved=await stored(()=>storage.get(userId));
   if(!active(context))return snapshot();
   context.data=saved?{...empty(),...copy(saved)}:empty();context.ready=true;
   publish(context);void syncNow();
  }catch(error){if(active(context))status(context,'error',error);throw error;}
  return snapshot();
 }
 async function replaceEntries(entries){
  const context=current;
  if(!context?.ready||destroyed)throw new Error('Sign in and load your library before editing it.');
  const incoming={};
  const base=Object.values(copy(context.data.entries));
  for(const entry of entries){
   if(!entry||typeof entry.id!=='string'||!entry.id||own(incoming,entry.id))throw new Error('Library entries need unique nonempty IDs.');
   assign(incoming,entry.id,copy(entry));
  }
  status(context,'saving');
  try{
   await mutate(context,data=>{
    const rebased={};for(const row of rebaseLibraryEntries(base,Object.values(incoming),Object.values(data.entries)))assign(rebased,row.id,row);
    let changed=false;
    for(const id of new Set([...Object.keys(data.entries),...Object.keys(rebased)])){
     const before=value(data.entries,id,null),after=value(rebased,id,null);
     if(!same(before,after)){queue(context,data,id,after);changed=true;}
    }
    return changed;
   });
   if(active(context)){if(context.paused)status(context,'auth-required');else void syncNow();}
  }catch(error){status(context,'error',error);throw error;}
  return snapshot();
 }
 async function resolveConflict(id,choice){
  const context=current;
  if(!context?.ready)throw new Error('Your account library is not loaded.');
  if(!['local','remote','both'].includes(choice))throw new Error('Choose local, remote, or both.');
  try{
   await mutate(context,data=>{
    const remote=value(data.conflicts,id,null);if(!remote)return false;
    const local=value(data.entries,id,null);
    assign(data.revisions,id,remote.revision);delete data.conflicts[id];
    if(choice==='local')queue(context,data,id,local,remote.revision);
    else{
     applyRemote(data,{id,...remote});delete data.pending[id];
     if(choice==='both'&&local){
      let duplicateId;do{duplicateId=globalThis.crypto.randomUUID();}while(own(data.entries,duplicateId)||own(data.revisions,duplicateId));
      queue(context,data,duplicateId,{...local,id:duplicateId},0);
     }
    }
   });
   if(active(context)){if(context.paused)status(context,'auth-required');else void syncNow();}
  }catch(error){status(context,'error',error);throw error;}
  return snapshot();
 }
 const online=()=>void syncNow();
 globalThis.addEventListener?.('online',online);
 return {setSession,replaceEntries,syncNow,resolveConflict,getState:()=>snapshot(),destroy(){destroyed=true;++epoch;current=null;globalThis.removeEventListener?.('online',online);}};
}
