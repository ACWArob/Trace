import {coverMatch,coverImageUrl} from './core.mjs';

// Background cover backfill for imported entries. Network, clock and saving are injected
// so the queue can be tested; it only ever adds `image` and a missing `imdbId`.
export const COVER_RETRY_MS=7*24*60*60*1000;

export function coverCandidates(entries,attempts={},now=Date.now()){
 return (Array.isArray(entries)?entries:[]).filter(row=>row&&row.type!=='memory'&&!row.image&&typeof row.id==='string'&&typeof row.title==='string'&&row.title.trim()
  &&!(Number.isFinite(attempts[row.id])&&now-attempts[row.id]<COVER_RETRY_MS));
}

export function pruneCoverAttempts(attempts,ids,now=Date.now()){
 const out={};
 for(const [id,at] of Object.entries(attempts||{}))if(ids.has(id)&&Number.isFinite(at)&&now-at<COVER_RETRY_MS)out[id]=at;
 return out;
}

export function applyCovers(entries,matches){
 const byId=new Map(matches.map(match=>[match.id,match]));
 return entries.map(row=>{
  const match=byId.get(row?.id);const image=coverImageUrl(match?.image);
  if(!match||!image||row.type==='memory'||row.image)return row;
  const next={...row,image};
  if(['movie','tv','anime'].includes(row.type)&&!row.imdbId&&/^tt\d{5,12}$/.test(match.imdbId||''))next.imdbId=match.imdbId;
  return next;
 });
}

export async function runCoverBackfill({entries,attempts={},search,save,now=()=>Date.now(),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),
 onProgress=()=>{},isCancelled=()=>false,batchSize=25,intervalMs=1000,backoffMs=2000,maxBackoffMs=5*60*1000,maxRetries=5}){
 const queue=coverCandidates(entries,attempts,now());
 const total=queue.length;let processed=0,matched=0,pending=[],lastStart=-Infinity;
 const flush=async()=>{if(!pending.length)return true;const batch=pending;pending=[];return !!await save(batch);};
 const finish=async status=>{
  if(status==='cancelled')return {status,processed,total,matched};
  if(!await flush())status='save-failed';
  return {status,processed,total,matched};
 };
 for(const entry of queue){
  let failures=0;
  for(;;){
   if(isCancelled())return finish('cancelled');
   const wait=lastStart+intervalMs-now();if(wait>0)await sleep(wait);
   if(isCancelled())return finish('cancelled');
   lastStart=now();
   let response;
   try{response=await search(entry);}catch{return finish(isCancelled()?'cancelled':'offline');}
   if(isCancelled())return finish('cancelled');
   const status=Number(response?.status)||0;
   if(status===401||status===403)return finish('auth');
   if(status===429||status>=500){
    failures++;
    if(failures>maxRetries)return finish('unavailable');
    const hinted=Math.max(0,Number(response.retryAfter)||0)*1000;
    await sleep(Math.min(maxBackoffMs,Math.max(hinted,backoffMs*2**(failures-1))));
    continue;
   }
   attempts[entry.id]=now();
   const match=status>=200&&status<300?coverMatch(entry,response.items):null;
   if(match){matched++;pending.push({id:entry.id,...match});}
   break;
  }
  processed++;
  onProgress({processed,total,matched});
  if(pending.length>=batchSize&&!await flush())return {status:'save-failed',processed,total,matched};
 }
 return finish('done');
}
