import {coverMatch,coverImageUrl} from './core.mjs';

// Background cover backfill for imported entries. Network, clock and saving are injected
// so the queue can be tested; it only ever adds `image` and a missing `imdbId`.
export const COVER_RETRY_MS=7*24*60*60*1000;
const SCREEN_TYPES=['movie','tv','anime'];

// Letterboxd film links accepted by /api/letterboxd-poster (mirrors profile_feeds.letterboxd_film_url).
export function letterboxdFilmUrl(value){
 if(typeof value!=='string'||value.length>400||/[\x00-\x20\x7f\\?#@]/.test(value))return '';
 let code=value.match(/^https:\/\/boxd\.it\/([A-Za-z0-9]{1,16})$/);
 if(code)return `https://boxd.it/${code[1]}`;
 code=value.match(/^https:\/\/letterboxd\.com\/(?:[A-Za-z0-9_-]{1,40}\/)?film\/([A-Za-z0-9-]{1,200})\/?$/);
 return code?`https://letterboxd.com/film/${code[1].toLowerCase()}/`:'';
}

export function letterboxdPosterUrl(value){
 if(typeof value!=='string'||/[\x00-\x20\x7f\\]/.test(value))return '';
 try{
  const url=new URL(value);
  return url.protocol==='https:'&&url.hostname==='a.ltrbxd.com'&&!url.username&&!url.password&&!url.port&&!url.hash
   &&/^\/[A-Za-z0-9/._-]{1,300}$/.test(url.pathname)&&!/empty-poster/i.test(url.pathname)?url.href:'';
 }catch{return '';}
}

const wantsLetterboxdPoster=row=>SCREEN_TYPES.includes(row?.type)&&!!letterboxdFilmUrl(row.sourceUrl);

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
  const match=byId.get(row?.id);
  const image=coverImageUrl(match?.image)||(wantsLetterboxdPoster(row)?letterboxdPosterUrl(match?.image):'');
  if(!match||!image||row.type==='memory'||row.image)return row;
  const next={...row,image};
  if(SCREEN_TYPES.includes(row.type)&&!row.imdbId&&/^tt\d{5,12}$/.test(match.imdbId||''))next.imdbId=match.imdbId;
  return next;
 });
}

export async function runCoverBackfill({entries,attempts={},search,poster=null,save,now=()=>Date.now(),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),
 onProgress=()=>{},isCancelled=()=>false,batchSize=25,intervalMs=1000,backoffMs=2000,maxBackoffMs=5*60*1000,maxRetries=5}){
 const queue=coverCandidates(entries,attempts,now());
 const total=queue.length;let processed=0,matched=0,pending=[],lastStart=-Infinity;
 const flush=async()=>{if(!pending.length)return true;const batch=pending;pending=[];return !!await save(batch);};
 const finish=async status=>{
  if(status==='cancelled')return {status,processed,total,matched};
  if(!await flush())status='save-failed';
  return {status,processed,total,matched};
 };
 // One paced request with shared retry handling: {status,response} or {stop}.
 const request=async(load,entry)=>{
  let failures=0;
  for(;;){
   if(isCancelled())return {stop:'cancelled'};
   const wait=lastStart+intervalMs-now();if(wait>0)await sleep(wait);
   if(isCancelled())return {stop:'cancelled'};
   lastStart=now();
   let response;
   try{response=await load(entry);}catch{return {stop:isCancelled()?'cancelled':'offline'};}
   if(isCancelled())return {stop:'cancelled'};
   const status=Number(response?.status)||0;
   if(status===401||status===403)return {stop:'auth'};
   if(status===429||status>=500){
    failures++;
    if(failures>maxRetries)return {stop:'unavailable'};
    const hinted=Math.max(0,Number(response.retryAfter)||0)*1000;
    await sleep(Math.min(maxBackoffMs,Math.max(hinted,backoffMs*2**(failures-1))));
    continue;
   }
   return {status,response};
  }
 };
 for(const entry of queue){
  const found=await request(search,entry);
  if(found.stop)return finish(found.stop);
  let match=found.status>=200&&found.status<300?coverMatch(entry,found.response.items):null;
  if(!match&&poster&&wantsLetterboxdPoster(entry)){
   const fallback=await request(poster,entry);
   if(fallback.stop)return finish(fallback.stop);
   const image=fallback.status>=200&&fallback.status<300?letterboxdPosterUrl(fallback.response.image):'';
   if(image)match={image};
  }
  attempts[entry.id]=now();
  if(match){matched++;pending.push({id:entry.id,...match});}
  processed++;
  onProgress({processed,total,matched});
  if(pending.length>=batchSize&&!await flush())return {status:'save-failed',processed,total,matched};
 }
 return finish('done');
}
