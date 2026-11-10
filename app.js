import {artworkCandidates,artworkFit} from './artwork.mjs';
import {apiFetch} from './api_client.mjs';
import {TYPES,singular,uid,today,escapeHTML as e,safeUrl,dateOnly,normalizeImport,mergeEntries,recapEntries,selectTracewall,tracewallLayout,tracewallArtifact,TRACEWALL_THEMES,TRACEWALL_FORMATS,tracewallShareCard,migrateEntry,normalizeAttachments,safeImageUrl as imageUrl,sameItem,normalizeGenres,reviewLinks,normalizePublicScores,blendRatings,filmConsensus,FILM_SCORE_SOURCES} from './core.mjs';
import {DEMO} from './data.js';
import {createSocial} from './social.js';
import {createLibrarySync,rebaseLibraryEntries} from './library_sync.mjs';
import {calendarDays,shiftMonth,monthLabel,chapters,wheelItems,bookRows} from './views.mjs';

const $ = (s,root=document)=>root.querySelector(s);
const paths={
 people:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m18 0v-2a4 4 0 0 0-3-3.87M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8m8-7.87a4 4 0 0 1 0 7.75',
 shelf:'M3 3v18m18-18v18M3 12h18M3 21h18M7 5v7m4-9v9m5-6v6M7 15v6m4-7v7m5-5v5',
 book:'M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15',
 wall:'M3 3h7v8H3zM14 3h7v5h-7zM3 15h7v6H3zM14 12h7v9h-7z',
 import:'M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5',download:'M12 3v12m-4-4 4 4 4-4M4 17v4h16v-4',
 plus:'M12 5v14M5 12h14', search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
 film:'M3 3h18v18H3zM7 3v18M17 3v18M3 8h4m10 0h4M3 16h4m10 0h4',
 music:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18m0 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6m-6 1a6 6 0 0 1 4-4',
 camera:'M4 6h4l2-3h4l2 3h4v15H4zM12 9a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
 pin:'M12 22s8-8 8-14a8 8 0 0 0-16 0c0 6 8 14 8 14Zm0-17a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
 heart:'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
 grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',list:'M8 6h13M8 12h13M8 18h13M3 6h.1M3 12h.1M3 18h.1',
 sparkle:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z',arrow:'M4 12h16m-6-6 6 6-6 6',
 upload:'M12 16V3m-4 4 4-4 4 4M4 15v6h16v-6',close:'m6 6 12 12M18 6 6 18'
};
const icon=(name)=>`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.sparkle}"/></svg>`;
function hydrateIcons(root=document){root.querySelectorAll('[data-icon]').forEach(x=>x.innerHTML=icon(x.dataset.icon));}
hydrateIcons();
const KEY='trace.collection.v1';
const connectionErrors={};
let connections={};try{const saved=JSON.parse(localStorage.getItem('trace.connections.v1')||'{}');for(const service of ['letterboxd','goodreads'])if(saved[service]&&typeof saved[service].identifier==='string')connections[service]=saved[service];}catch{}
let storageProblem='';
let collection={started:false,entries:[]};
try { const saved=localStorage.getItem(KEY); if(saved){const data=JSON.parse(saved);if(!Array.isArray(data.entries)) throw Error();collection={started:!!data.started,entries:validateEntries(data.entries)};} } catch {storageProblem='Your saved collection could not be read. Back up browser storage before replacing it. New changes will not be saved until this is resolved.';}
let wallConfig={title:'The things that stayed.',period:today().slice(0,7),theme:'paper',selected:null};
try{const config=JSON.parse(localStorage.getItem('trace.wall.v1'));if(config && typeof config.title==='string') wallConfig={...wallConfig,...config,selected:Array.isArray(config.selected)?config.selected:null};}catch{}
const state={page:'shelf',category:'all',status:'all',sort:'collected',search:'',favorites:false,view:'shelf',pending:null,importError:'',importSource:'auto',importNotice:'',previewId:null,calendarMonth:null,calendarDay:null,wheelIndex:0,chapterMonth:null,genre:'all',movieMode:'dvd',movieLimit:60};
try{const saved=localStorage.getItem('trace.movie-view');if(['dvd','poster'].includes(saved))state.movieMode=saved;}catch{}
const main=$('#main');const logDialog=$('#log-dialog');const detailDialog=$('#detail-dialog');
let activeSession={user:null,csrf:''},libraryReady=true,sessionEpoch=0;
let librarySaveTail=Promise.resolve();
const guestStorageProblem=storageProblem;let guestCollection=collection;
let syncStatus={state:'device',pendingCount:0,conflictCount:0};
const libraryDialog=document.createElement('dialog');libraryDialog.id='library-dialog';libraryDialog.className='modal';libraryDialog.setAttribute('aria-label','Private library');document.body.append(libraryDialog);let pendingGuestCopy=null;
const librarySync=createLibrarySync({request:libraryRequest,onChange:receiveLibrary,onStatus:status=>{syncStatus=status;renderLibraryStatus();}});
const social=createSocial({main,onSession:handleSession,entries:()=>entries(),notify:message=>notify(message),openDraft:item=>{entryDraft={...item,id:uid(),status:'want',date:'',rating:0,notes:'',history:[],addedAt:new Date().toISOString()};renderEntryForm();logDialog.showModal();}});
let logType='movie',searchResults=[],searchController=null,searchSequence=0,entryDraft=null,editingId=null,toastTimer;
const entries=()=>collection.started?collection.entries:DEMO;
const isDemo=()=>!collection.started;
function notify(message){const t=$('#toast');t.textContent=message;t.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('visible'),4500);}
async function persist(next){
 if(storageProblem){notify(storageProblem);return false;}if(next.entries.length>30000){notify('A library can contain up to 30,000 items.');return false;}const userId=activeSession.user?.id||null;
 try{if(userId){if(!libraryReady)throw Error('Your account library is still loading.');const base=structuredClone(collection.entries),desired=structuredClone(next.entries);const save=librarySaveTail.then(async()=>{if(activeSession.user?.id!==userId)return false;await librarySync.replaceEntries(rebaseLibraryEntries(base,desired,librarySync.getState().entries));return activeSession.user?.id===userId;});librarySaveTail=save.catch(()=>{});return await save;}else{localStorage.setItem(KEY,JSON.stringify(next));guestCollection=next;collection=next;}return true;}
 catch(error){notify(userId?'Could not save this change on your device. '+error.message:'Browser storage is full or unavailable. Download a backup, then free space and try again.');return false;}
}
function saveWall(){try{localStorage.setItem(preferenceKey('wall'),JSON.stringify(wallConfig));}catch{notify('Recap settings could not be saved in this browser.');}}
function validateEntries(rows){
 if(rows.length>30000)throw Error('This backup exceeds the 30,000-item limit.');
 return rows.map(original=>{
  const row=migrateEntry(original);
  if(!row || typeof row.title!=='string' || !row.title.trim() || !Object.hasOwn(TYPES,row.type))throw Error('This backup has an invalid entry.');
  const out={id:typeof row.id==='string'&&/^[a-zA-Z0-9_-]{1,100}$/.test(row.id)?row.id:uid(),title:row.title.slice(0,500),type:row.type,status:['done','want','progress'].includes(row.status)?row.status:'done',favorite:!!row.favorite,forLater:!!row.forLater,rating:Math.max(0,Math.min(5,Number(row.rating)||0)),date:dateOnly(row.date),history:Array.isArray(row.history)?row.history.map(dateOnly).filter(Boolean):[]};
  for(const key of ['creator','year','notes','description','source','externalId','addedAt','imdbId'])out[key]=typeof row[key]==='string'?row[key].slice(0,['notes','description'].includes(key)?20000:500):'';
  out.publicScores=normalizePublicScores(row.publicScores);out.genres=normalizeGenres(row.genres||DEMO.find(x=>sameItem(x,row))?.genres);out.image=imageUrl(row.image);out.sourceUrl=safeUrl(row.sourceUrl||'');out.metadataSourceUrl=safeUrl(row.metadataSourceUrl||'');out.metadataSource=String(row.metadataSource||'').slice(0,100);out.attachments=normalizeAttachments(row.attachments);
  if(['restaurant','place'].includes(row.legacyType)){out.legacyType=row.legacyType;out.legacyRating=Number(row.legacyRating)||0;}
  return {...row,...out};
 });
}
function stars(r){return r?`${'★'.repeat(Math.floor(r))}${r%1?'½':''}`:'';}
const memoryDate=x=>x.date?new Date(x.date+'T12:00:00').toLocaleDateString('en-US',{month:'short',day:'numeric'}):'A little memory';
const statusLabel=x=>x.type==='memory'?memoryDate(x):x.status==='want'?'Want to try':x.status==='progress'?'In progress':x.rating?stars(x.rating):'Logged';
const categoryFor=x=>['tv','anime'].includes(x.type)?'tv':x.type;
const artClass=x=>x.type==='book'?'book':x.type==='album'?'vinyl':x.type==='memory'?'polaroid':'dvd';
const colorFor=x=>['#a84d32','#3f7472','#79414b','#8e6e29','#59683b'][Array.from(x.title).reduce((s,c)=>s+c.charCodeAt(0),0)%5];
function fallback(x){return `<span class="cover-fallback" style="--cover-color:${colorFor(x)}"><span class="fallback-symbol">${x.type==='album'?'◉':x.type==='book'?'✴︎':'✧'}</span><span class="fallback-title" dir="auto">${e(x.title)}</span><span class="fallback-creator" dir="auto">${e(x.creator||singular[x.type])}</span><span class="cover-missing">${x.type==='memory'?'No photo added':'Cover unavailable'}</span></span>`;}
function cover(x){const [url,...alternatives]=artworkCandidates(x);return url?`<img class="cover-art" src="${e(url)}" data-cover-alternatives="${e(JSON.stringify(alternatives))}" alt="${e(x.title)} cover" loading="lazy" data-cover-title="${e(x.title)}" data-cover-creator="${e(x.creator||'')}" data-cover-type="${e(x.type)}">`:fallback(x);}
function failedCover(img){let alternatives=[];try{alternatives=JSON.parse(img.dataset.coverAlternatives||'[]');}catch{}const next=alternatives.shift();if(next){img.dataset.coverAlternatives=JSON.stringify(alternatives);img.src=next;return;}img.outerHTML=fallback({title:img.dataset.coverTitle,creator:img.dataset.coverCreator,type:img.dataset.coverType});}
document.addEventListener('error',ev=>{if(ev.target.matches?.('img[data-cover-title]'))failedCover(ev.target);},true);
document.addEventListener('load',ev=>{if(ev.target.matches?.('img[data-cover-title]')&&ev.target.naturalWidth<=2&&ev.target.naturalHeight<=2)failedCover(ev.target);},true);
function artifact(x,orientation='face') {
 const kind=artClass(x);const spine=orientation==='spine'&&['book','dvd'].includes(kind);
 return `<span class="artifact ${kind} ${spine?'is-spine':''}" style="--cover-color:${colorFor(x)}">${kind==='vinyl'?`<span class="record"></span><span class="sleeve">${cover(x)}</span><span class="vinyl-stand" aria-hidden="true"></span>`:kind==='polaroid'?`${cover(x)}<span class="photo-caption">${e(x.title)}</span>`:`<span class="binding" aria-hidden="true"></span><span class="jacket">${kind==='dvd'?'<span class="case-label">DVD · VIDEO</span>':''}${cover(x)}</span><span class="page-block" aria-hidden="true"></span>${spine?`<span class="spine-type">${kind==='dvd'?'DVD':'✳︎'}</span><span class="spine-title" dir="auto">${e(x.title)}</span><span class="spine-author">${e(x.creator)}</span>`:''}`}</span>`;
}
function sampleStrip(){return isDemo()?`<div class="sample-strip"><span>${icon('sparkle')} A sample shelf, for a little inspiration. Your collection starts with your first log.</span><button data-action="start">Make it yours ${icon('arrow')}</button></div>`:'';}
function heading(title,sub,note=''){return `<div class="page-heading"><div><h1>${title}</h1><p class="subtitle">${sub}</p></div></div>`;}
const categoryOptions=[['all','All','shelf'],['movie','Movies','film'],['tv','TV shows','film'],['book','Books','book'],['album','Albums','music'],['memory','Memories','camera']];
function shelfPage(){
 const roomTitles={all:['Your shelf','Films, books, records, and memories.'],book:['Library','Books, with room for the next chapter.'],album:['Listening room','Your album collection, one turn at a time.'],movie:['Cinema','A place for the films that stay with you.'],tv:['TV room','Series, seasons, and familiar worlds.'],memory:['Memories','The days you decided to keep.']};
 main.innerHTML=heading(...roomTitles[state.category])+
 `<div class="shelf-meta"><span class="collection-count"><i></i>${entries().length} ${entries().length===1?'item':'items'} ${isDemo()?'<span class="sample-badge">Sample collection</span>':''}<span>·</span> ${entries().filter(x=>x.favorite).length} favorites</span><div class="view-switch" role="group" aria-label="Collection view"><button data-action="view" data-value="shelf" class="${state.view==='shelf'?'active':''}" aria-pressed="${state.view==='shelf'}">${icon('shelf')} Shelf</button><button data-action="view" data-value="list" class="${state.view==='list'?'active':''}" aria-pressed="${state.view==='list'}">${icon('list')} List</button></div></div>
 <div class="category-tabs" role="group" aria-label="Filter category">${categoryOptions.map(([id,label,ic])=>`<button data-action="category" data-value="${id}" class="${state.category===id?'active':''}" aria-pressed="${state.category===id}">${label}<span class="tab-count">${entries().filter(x=>id==='all'||categoryFor(x)===id).length}</span></button>`).join('')}</div>
 <div class="toolbar"><label class="search-box">${icon('search')}<span class="sr-only">Search your shelf</span><input id="shelf-search" type="search" placeholder="Search your shelf" value="${e(state.search)}"></label><select id="status-filter" class="select-control" aria-label="Filter status"><option value="all">All items</option><option value="forLater">For later list</option><option value="done">Logged</option><option value="progress">In progress</option><option value="want">Want to try</option></select><select id="sort-filter" class="select-control" aria-label="Sort collection"><option value="collected">Shelf order</option><option value="recent">Latest logged</option><option value="rating">Highest rated</option><option value="title">A to Z</option></select><button data-action="favorites" class="favorite-filter ${state.favorites?'active':''}" aria-pressed="${state.favorites}" aria-label="Show favorites">${icon('heart')}<span>Favorites</span></button></div>
 <div class="shelf-invitation"><button class="text-button" data-action="keep-memory">${icon('camera')} Add memory</button></div><label class="genre-filter"><span>Genre</span><select id="genre-filter" aria-label="Filter genre"><option value="all">All genres</option><option value="missing">Missing genres</option>${[...new Set(entries().filter(x=>state.category==='all'||categoryFor(x)===state.category).flatMap(x=>x.genres||[]))].sort().map(g=>`<option value="${e(g)}">${e(g)}</option>`).join('')}</select></label><div id="shelf-content"></div>${sampleStrip()}`;
 $('#genre-filter').value=state.genre;$('#status-filter').value=state.status;$('#sort-filter').value=state.sort;renderShelfContents();
}
function filtered(){let list=entries().filter(x=>(state.category==='all'||categoryFor(x)===state.category)&&(state.status==='all'||(state.status==='forLater'?x.forLater:x.status===state.status))&&(state.genre==='all'||(state.genre==='missing'?!(x.genres||[]).length:(x.genres||[]).includes(state.genre)))&&(!state.favorites||x.favorite)&&`${x.title} ${x.creator}`.toLowerCase().includes(state.search.toLowerCase()));
 if(state.sort==='title')list.sort((a,b)=>a.title.localeCompare(b.title));if(state.sort==='rating')list.sort((a,b)=>b.rating-a.rating);if(state.sort==='recent')list.sort((a,b)=>(b.date||'').localeCompare(a.date||''));return list;}
function empty(title,description,action='log'){return `<div class="empty-state"><span>✳</span><h2>${title}</h2><p>${description}</p><button class="button primary" data-action="${action}">${action==='reset'?'Clear filters':'Log something'} ${icon('plus')}</button></div>`;}
function renderShelfContents(){const host=$('#shelf-content');if(!host)return;host.className='';const scrollPositions=new Map([...host.querySelectorAll('.shelf-rail')].map(rail=>[rail.dataset.category,rail.scrollLeft]));const list=filtered();if(!list.length){host.innerHTML=entries().length?empty('A little room to look around.','No items match these filters. Try a different search.','reset'):empty('Every shelf starts somewhere.','A favorite film. An ordinary day worth keeping. What will your first trace be?');return;}
 if(state.view==='list'){host.innerHTML=`<div class="entry-list">${list.map(listItem).join('')}</div>`;return;}
 if(state.category==='book'){renderBookRoom(host,list);return;}if(state.category==='album'){renderAlbumRoom(host,list);return;}if(state.category==='memory'){renderMemoryCalendar(host,list);return;}if(state.category==='movie'){renderMovieRoom(host,list);return;}if(state.category==='tv'){renderScreenRoom(host,list,'tv');return;}
 const groups=[['book','Books',''],['movie','Movies',''],['tv','TV shows',''],['album','Albums',''],['memory','Memories','']];
 let html=`<div class="cabinet ${state.category==='all'?'mixed-collection':'single-category'} ${state.category==='all'&&host.clientWidth>=900?'wide-collection':''}">`;
 for(const [category,title,sub] of groups){const group=list.filter(x=>categoryFor(x)===category);if(!group.length)continue;
 const shelfWidth=state.category==='all'&&host.clientWidth>=900&&['book','movie','tv'].includes(category)?(host.clientWidth-36)/2:host.clientWidth;
 html+=`<section class="shelf-row lane-${category}" aria-label="${e(title)}"><div class="shelf-inscription"><h2>${e(title)}</h2><div class="shelf-scroll-controls"><button type="button" data-action="shelf-scroll" data-direction="-1" aria-label="Scroll ${e(title)} left">${icon('arrow')}</button><button type="button" data-action="shelf-scroll" data-direction="1" aria-label="Scroll ${e(title)} right">${icon('arrow')}</button></div></div><div class="shelf-rail" data-category="${category}" role="group" aria-label="${e(title)} shelf" tabindex="0"><div class="cabinet-stage ${category==='album'?'record-rack':''}">${group.map((x,i)=>{
 const spine=['book','movie','tv'].includes(category);const selected=x.id===state.previewId;const tilt=[-2,0,1,-3,1,0,2][i%7];
 return roomCell(x,i,spine);}).join('')}${category==='album'?'<div class="record-trough" aria-hidden="true"><span>33⅓ &nbsp; / &nbsp; LONG PLAYS</span></div>':''}</div></div><div class="shelf-plank"></div></section>`;
 }html+='</div>';host.innerHTML=html;
 for(const rail of host.querySelectorAll('.shelf-rail')){rail.scrollLeft=scrollPositions.get(rail.dataset.category)||0;updateShelfScroll(rail);rail.addEventListener('scroll',()=>updateShelfScroll(rail),{passive:true});}
}
function roomCell(x,i=0,spine=['book','movie','tv','anime'].includes(x.type)){const selected=x.id===state.previewId;const category=categoryFor(x);const tilt=[-2,0,1,-3,1,0,2][i%7];
 return `<div class="cabinet-cell ${spine?'spine-cell':'face-cell'} type-${x.type} ${selected?'is-preview':''}" data-orientation="${spine?'spine':'face'}" style="--layer:${i+1};--lean:${tilt}deg;--object-height:${category==='book'?220+(i%3)*12:['movie','tv'].includes(category)?211+(i%2)*9:190}px"><button class="object-button" data-action="detail" data-id="${e(x.id)}" aria-label="${selected?'Open details for':'Preview'} ${e(x.title)}" aria-expanded="${selected}">${artifact(x,selected?'face':spine?'spine':'face')}</button>${x.favorite?`<span class="object-heart" aria-label="Favorite">${icon('heart')}</span>`:''}<div class="cabinet-caption"><button class="title" data-action="detail" data-id="${e(x.id)}">${e(x.title)}</button><span class="caption-meta">${x.type==='memory'?e(memoryDate(x)):e(x.creator||singular[x.type])}</span><span class="rating">${x.type==='memory'?(x.attachments?.length?`${x.attachments.length} attached`:'Memory'):e(statusLabel(x))}</span></div></div>`;
}
function updateShelfScroll(rail){const section=rail.closest('.shelf-row');if(!section?.querySelector('[data-direction]'))return;const overflow=rail.scrollWidth>rail.clientWidth+1;section.classList.toggle('has-overflow',overflow);rail.tabIndex=overflow?0:-1;section.querySelector('[data-direction="-1"]').disabled=rail.scrollLeft<=1;section.querySelector('[data-direction="1"]').disabled=rail.scrollLeft+rail.clientWidth>=rail.scrollWidth-1;}

function listItem(x,diary=false){return `<button class="list-entry" data-action="detail" data-id="${e(x.id)}">${diary?`<div class="diary-date">${x.date?`<b>${Number(x.date.slice(8))}</b>${new Date(x.date+'T12:00:00').toLocaleDateString('en-US',{weekday:'short'})}`:'—'}</div>`:''}<span class="list-thumb">${cover(x)}</span><div><h3>${e(x.title)}</h3><p>${e(x.creator)}${x.year?` · ${e(x.year)}`:''}</p>${diary&&x.notes?`<p class="diary-note">“${e(x.notes.slice(0,150))}”</p>`:''}</div><span class="type-badge">${singular[x.type]}</span><span class="list-rating">${e(statusLabel(x))}</span></button>`;}
function renderBookRoom(host,list){host.className='room-books';const rows=bookRows(list,host.clientWidth,matchMedia('(max-width:760px)').matches?84:107);host.innerHTML=`<div class="room-caption"><span>${list.length} books</span><button class="text-button" data-action="log">Add a book +</button></div><div class="library-case cabinet">${rows.map((row,r)=>`<section class="book-row"><span class="library-row-label">${String(r+1).padStart(2,'0')}</span><div class="cabinet-stage">${row.map((x,i)=>roomCell(x,i,true)).join('')}</div><div class="library-plank"></div></section>`).join('')}</div>`;}
function scoreSummary(scores,kind){
 if(kind==='movie'){const blend=filmConsensus(scores),single=blend.count===1?blend.sources[0]:null;return {...blend,label:single?single.source:'Trace score',display:single?single.value/single.max*10:blend.value,coverage:`${blend.count} of 4 sources · ${blend.count<2?'not blended':blend.count===4?'full blend':'partial blend'}`};}
 const blend=blendRatings(scores);return {...blend,label:blend.count===1?blend.sources[0].source:'Trace score',display:blend.value===null?null:blend.value*2,coverage:blend.count?blend.count+' source'+(blend.count===1?' · not blended':'s · blended'):'Not scored yet'};
}
function traceBadge(scores,kind){const blend=scoreSummary(scores,kind);return `<span class="trace-score-badge ${blend.count===1?'single-source-badge':''}"><img class="trace-score-symbol" src="/assets/trace-icon.svg" alt="" width="22" height="22"><span class="trace-score-label">${e(blend.label)}</span><b>${blend.display===null?'—':blend.display.toFixed(1)}${blend.display===null?'':'<span class="trace-score-scale">/10</span>'}</b><small>${e(blend.coverage)}</small></span>`;}
function scoreRow(row,metadata={}){const number=Number(row.value.toFixed(2)),native=row.source.startsWith('Rotten Tomatoes')?number+'%':number+' / '+row.max;const saved=metadata[row.via?.toLowerCase()];const savedText=saved?.fetchedAt?` · saved ${new Date(saved.fetchedAt*1000).toLocaleDateString('en-US',{month:'short',day:'numeric'})}${saved.stale?' · update pending':''}`:'';const content=`<span>${e(row.source)}${row.via?' · '+e(row.via):''}</span><b>${native}</b><small>${row.count===null?'Rating count unavailable':row.count.toLocaleString()+' ratings'}${e(savedText)}${/\/search(?:[/?]|$)/.test(row.url)?' · source search':''}</small>`;return row.url?`<a href="${e(row.url)}" target="_blank" rel="noopener noreferrer">${content}</a>`:`<div class="score-unlinked">${content}</div>`;}
function traceRatingPanel(scores,kind,status={},metadata={}){
 const blend=scoreSummary(scores,kind),film=kind==='movie';
 const rows=film?FILM_SCORE_SOURCES.map(name=>{const row=blend.sources.find(x=>x.source===name);return row?scoreRow(row,metadata):`<div class="score-missing"><span>${e(name)}</span><b>—</b><small>Not available</small></div>`}).join(''):blend.sources.map(row=>scoreRow(row,metadata)).join('');
 const formula=film?'The four sources each contribute 25% when all are available. IMDb stays on /10, Letterboxd is multiplied by 2, and each Rotten Tomatoes percentage is divided by 10. Missing sources are left out; available sources share the weight equally. At least two sources are needed for a Trace score. RT percentages measure approval, so this is a composite consensus score.': 'Available ratings are converted to /10. Known rating counts use log₁₀(count + 1) ÷ 4, capped between 0.2 and 1; sources without counts get weight 0.5. Each source counts once.';
 const unavailable=Object.values(status).some(x=>['unavailable','rate_limited','invalid_key'].includes(x));
 const notice=unavailable?'Some rating providers are temporarily unavailable. Available scores are shown.':film&&blend.count<4&&status.mdblist==='not_configured'?'Additional rating sources are not connected to this Trace server yet.':'';
 return `<details class="trace-rating-panel"><summary>${traceBadge(scores,kind)}</summary>${rows?`<div class="score-sources">${rows}</div>`:''}${film&&blend.other.length?`<div class="other-public-scores"><h4>Other scores · outside this blend</h4><div class="score-sources">${blend.other.map(row=>scoreRow(row,metadata)).join('')}</div></div>`:''}${notice?`<p class="score-notice">${notice}</p>`:''}<p>${formula} Your personal rating stays separate.</p>${!blend.count?'<p>No matching public scores yet. Check the correct release in Discover, or visit the other communities.</p>':''}</details>`;
}
function updateScorePanel(host,data,kind){const open=host.querySelector('details')?.open;host.innerHTML=traceRatingPanel(data.sources,kind,data.providerStatus,data.providerMeta);if(open)host.querySelector('details').open=true;}
async function refreshCatalogScores(host,items){
 const cards=[...host.querySelectorAll('.catalog-trace-score')],jobs=items.map((item,index)=>({item,card:cards[index]})).filter(({item})=>['movie','tv','anime'].includes(item.type)&&/^tt\d{5,12}$/.test(item.imdbId||''));let next=0;
 await Promise.all(Array.from({length:Math.min(2,jobs.length)},async()=>{while(next<jobs.length){const {item,card}=jobs[next++];if(!card?.isConnected)return;try{const params=new URLSearchParams({type:item.type,title:item.title,year:item.year||'',imdbId:item.imdbId});const response=await apiFetch('/api/ratings?'+params);if(!response.ok)throw Error();const data=await response.json();if(!card.isConnected)continue;item.publicScores=normalizePublicScores(data.sources);updateScorePanel(card,data,item.type);}catch{if(card.isConnected)card.insertAdjacentHTML('beforeend','<p class="source-hint">Additional scores could not be refreshed.</p>');}}}));
}

function elsewhere(item){const links=reviewLinks(item);return links.length?`<section class="elsewhere"><h4>Elsewhere</h4><p>Explore other communities’ reviews and details.</p><div>${links.map(x=>`<a href="${e(x.url)}" target="_blank" rel="noopener noreferrer">${e(x.label)} ↗</a>`).join('')}</div></section>`:'';}
function genreChips(item){return (item.genres||[]).length?`<div class="genre-chips">${item.genres.slice(0,3).map(g=>`<span>${e(g)}</span>`).join('')}</div>`:'';}
function renderMovieRoom(host,list){host.className='room-movie-columns';const visible=list.slice(0,state.movieLimit);const selected=list.find(x=>x.id===state.previewId);host.innerHTML=`<section class="screening-room cinema-room cinema-feature"><div class="screen-room-label"><span>TRACE CINEMA</span><span>${list.length} films</span></div><div class="screening-screen ${selected?'has-selection':''}">${selected?`<div class="screen-poster">${cover(selected)}</div><div class="screen-program"><span>${e(selected.year)} · ${e(selected.creator||'Film')}</span><h2>${e(selected.title)}</h2><p>${e((selected.description||'').slice(0,280))}</p><button class="button" data-action="open-entry" data-id="${e(selected.id)}">Open details ${icon('arrow')}</button></div>`:`<div class="screen-idle"><span>✦</span><h2>Choose tonight’s feature.</h2><p>Select a case or poster below to bring it onto the screen.</p></div>`}</div></section><section class="movie-library"><header class="movie-library-heading"><div><span class="calendar-kicker">THE FILM COLLECTION</span><h2>${list.length} films on your shelf</h2></div><div class="view-switch movie-mode" role="group" aria-label="Movie appearance"><button data-action="movie-mode" data-value="dvd" aria-pressed="${state.movieMode==='dvd'}" class="${state.movieMode==='dvd'?'active':''}">DVD cases</button><button data-action="movie-mode" data-value="poster" aria-pressed="${state.movieMode==='poster'}" class="${state.movieMode==='poster'?'active':''}">Posters</button></div></header><div class="movie-grid mode-${state.movieMode}">${visible.map(x=>{const selected=state.previewId===x.id;return `<article class="movie-card cabinet-cell face-cell ${selected?'is-preview':''}" data-orientation="face"><button class="movie-cover object-button" data-action="detail" data-id="${e(x.id)}" aria-label="${selected?'Open details for':'Preview'} ${e(x.title)}" aria-expanded="${selected}">${state.movieMode==='dvd'?artifact(x):cover(x)}</button><div class="movie-card-caption"><button data-action="detail" data-id="${e(x.id)}" class="title">${e(x.title)}</button><span>${e(x.year)}${x.creator?' · '+e(x.creator):''}</span><span class="rating">${e(statusLabel(x))}</span>${genreChips(x)}</div></article>`;}).join('')}</div>${visible.length<list.length?`<button class="button secondary movie-load-more" data-action="more-movies">Show 60 more · ${list.length-visible.length} remaining</button>`:''}</section>`;}
function renderScreenRoom(host,list,kind){host.className='room-screen '+(kind==='tv'?'television-room':'cinema-room');const selected=list.find(x=>x.id===state.previewId);host.innerHTML=`<section class="screening-room cabinet"><div class="screen-room-label"><span>${kind==='tv'?'THE TV ROOM':'TRACE CINEMA'}</span><span>${list.length} ${kind==='tv'?'shows':'films'}</span></div><div class="screening-screen ${selected?'has-selection':''}">${selected?`<div class="screen-poster">${cover(selected)}</div><div class="screen-program"><span>${e(selected.year)} · ${e(selected.creator||singular[selected.type])}</span><h2>${e(selected.title)}</h2><p>${e((selected.description||selected.notes||'').slice(0,280))}</p><button class="button" data-action="open-entry" data-id="${e(selected.id)}">Open details ${icon('arrow')}</button></div>`:`<div class="screen-idle"><span>${kind==='tv'?'◯':'✦'}</span><h2>${kind==='tv'?'Pick a show. Settle in.':'Choose tonight’s feature.'}</h2><p>Select a case below to pull up its cover.</p></div>`}</div><div class="program-rail shelf-rail" role="group" aria-label="${kind==='tv'?'TV':'Movie'} cases" tabindex="0"><div class="cabinet-stage">${list.map((x,i)=>roomCell(x,i,true)).join('')}</div></div><div class="cinema-ledge"></div></section>`;}
function renderAlbumRoom(host,list){host.className='room-albums';const index=((state.wheelIndex%list.length)+list.length)%list.length;state.wheelIndex=index;host.innerHTML=`<section class="jukebox cabinet"><div class="jukebox-label"><span>THE LISTENING ROOM</span><span>${list.length} albums · 33⅓</span></div><div class="jukebox-arch"><span></span><span></span><span></span></div><div class="jukebox-wheel">${wheelItems(list,index).map(({item,offset})=>`<div class="wheel-position" style="--offset:${offset};--distance:${Math.abs(offset)}">${roomCell(item,0,false)}</div>`).join('')}</div><div class="jukebox-controls"><button class="wheel-turn" data-action="wheel" data-value="-1" aria-label="Previous album">←</button><label class="album-dial"><span>TURN THE DIAL</span><input id="album-dial" type="range" min="0" max="${list.length-1}" step="1" value="${index}" aria-label="Choose album"></label><button class="wheel-turn" data-action="wheel" data-value="1" aria-label="Next album">→</button></div><p class="jukebox-hint">${state.previewId?'Select the cover again for details and ratings.':'Select a sleeve to reveal the album.'}</p></section>`;}
function renderMemoryCalendar(host,list){host.className='room-memories';const dates=list.filter(x=>x.date).map(x=>x.date).sort().reverse();if(!state.calendarMonth)state.calendarMonth=dates[0]?.slice(0,7)||today().slice(0,7);const month=state.calendarMonth;const monthDates=dates.filter(x=>x.startsWith(month));if(!state.calendarDay?.startsWith(month))state.calendarDay=monthDates[0]||(month===today().slice(0,7)?today():month+'-01');const selected=list.filter(x=>x.date===state.calendarDay),undated=list.filter(x=>!x.date);host.innerHTML=`<section class="memory-calendar"><header><div><span class="calendar-kicker">MEMORIES</span><h2>${e(monthLabel(month))}</h2></div><div class="calendar-controls"><button data-action="calendar-month" data-value="-1" aria-label="Previous month">←</button><button data-action="calendar-today">Today</button><button data-action="calendar-month" data-value="1" aria-label="Next month">→</button></div></header><div class="calendar-weekdays">${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(d=>`<span>${d}</span>`).join('')}</div><div class="calendar-grid">${calendarDays(month).map(day=>{const memories=list.filter(x=>x.date===day.date);return `<button class="calendar-day ${day.inMonth?'':'outside-month'} ${day.date===state.calendarDay?'selected':''} ${day.date===today()?'today':''}" data-action="calendar-day" data-value="${day.date}" aria-label="${day.date}, ${memories.length} ${memories.length===1?'memory':'memories'}" aria-pressed="${day.date===state.calendarDay}"><span>${day.day}</span>${memories.length?`<div class="calendar-photo">${memories[0].image?`<img src="${e(imageUrl(memories[0].image))}" alt="" loading="lazy">`:'<span>✧</span>'}</div><small>${memories.length===1?'1 memory':memories.length+' memories'}</small>`:''}</button>`;}).join('')}</div></section><section class="calendar-day-story"><header><h3>${e(new Date(state.calendarDay+'T12:00:00').toLocaleDateString('en-US',{month:'long',day:'numeric'}))}</h3><button class="button secondary" data-action="memory-on-day" ${state.calendarDay>today()?'disabled':''}>Add memory +</button></header><div class="day-memory-cards">${selected.map(x=>`<button data-action="open-entry" data-id="${e(x.id)}" class="day-memory-card">${x.image?`<img src="${e(imageUrl(x.image))}" alt="">`:''}<strong dir="auto">${e(x.title)}</strong><p>${e(x.notes||'')}</p><span>${x.attachments?.length?x.attachments.length+' attached':''}</span></button>`).join('')||'<p class="calendar-empty">No memories on this day yet.</p>'}</div></section>${undated.length?`<section class="undated-memories"><h3>Undated</h3>${undated.map(x=>listItem(x)).join('')}</section>`:''}`;}

function diaryPage(){const all=chapters(entries());if(!all.length){main.innerHTML=heading('Diary','A chapter for the things you keep.')+empty('Your first chapter.','Log a title or add a memory to begin.');return;}const index=Math.max(0,all.findIndex(x=>x.month===state.chapterMonth));const chapter=all[index];state.chapterMonth=chapter.month;
 main.innerHTML=heading('Diary','Your collection, in chapters.')+`<div class="chapter-navigation"><button class="button secondary" data-action="chapter" data-value="${all[Math.max(0,index-1)].month}" ${index===0?'disabled':''}>← Newer</button><span>Chapter ${chapter.number} of ${all.length}</span><button class="button secondary" data-action="chapter" data-value="${all[Math.min(all.length-1,index+1)].month}" ${index===all.length-1?'disabled':''}>Older →</button></div><article class="journal-chapter"><div class="journal-spine"></div><header><span class="chapter-kicker">CHAPTER ${String(chapter.number).padStart(2,'0')}</span><h2>${e(chapter.title)}</h2><p>${chapter.entries.length} entries</p></header><div class="journal-entries">${chapter.entries.map(x=>listItem(x,true)).join('')}</div><footer>trace. <span>${chapter.number}</span></footer></article>${sampleStrip()}`;
}
const catalogState={kind:'movie',query:'',genre:'',page:0,mode:'browse',items:[],response:{},sequence:0};
function discoverPage(){main.innerHTML=heading('Discover','Follow a title. Find your next favorite.')+`<form id="discover-search" class="discover-search"><label class="field"><span class="sr-only">Catalog category</span><select id="discover-kind" aria-label="Catalog category">${['movie','tv','anime','book','album'].map(k=>`<option value="${k}" ${catalogState.kind===k?'selected':''}>${TYPES[k]}</option>`).join('')}</select></label><label class="field"><span class="sr-only">Search catalogs</span><input name="query" dir="auto" type="search" value="${e(catalogState.query)}" maxlength="200" placeholder="A title, an author, an artist…"></label><button class="button primary" type="submit">Search</button></form><div class="catalog-toolbar"><button class="text-button" data-action="browse-catalog">Browse saved catalog ↗</button><label>Genre <select id="catalog-genre" aria-label="Catalog genre"><option value="">All genres</option></select></label></div><p class="catalog-explanation">Search real catalogs for films, TV, books, and albums. Found titles are saved here with their sources; your private ratings and notes stay on your shelf.</p><div id="catalog-results-view" aria-live="polite"></div>`;loadCatalog();}
async function loadCatalog(){const sequence=++catalogState.sequence,host=$('#catalog-results-view');if(!host)return;host.innerHTML='<p class="source-hint">Looking through the catalog…</p>';const button=$('#discover-search [type="submit"]');button.disabled=true;try{const params=new URLSearchParams(catalogState.mode==='search'?{type:catalogState.kind,q:catalogState.query}:{type:catalogState.kind,q:catalogState.query,genre:catalogState.genre,page:String(catalogState.page)});const response=await apiFetch((catalogState.mode==='search'?'/api/search?':'/api/catalog?')+params);const data=await response.json();if(!response.ok)throw Error(data.error||'Catalog unavailable.');if(sequence!==catalogState.sequence||!host.isConnected)return;catalogState.items=data.items||[];const genres=[...new Set(data.genres||catalogState.items.flatMap(x=>x.genres||[]))].sort();const filter=$('#catalog-genre');filter.innerHTML='<option value="">All genres</option>'+genres.map(g=>`<option value="${e(g)}">${e(g)}</option>`).join('');filter.value=catalogState.genre;catalogState.response=data;renderCatalogItems(data);}catch(err){if(sequence===catalogState.sequence&&host.isConnected)host.innerHTML=`<p class="error-message" role="alert">${e(err instanceof TypeError?(window.TraceNative?"Cannot reach Trace’s server. Check your connection and try again.":"Cannot reach Trace’s local server. Start it with python3 server.py 5173, then try your search again."):err.message)}</p><button class="text-button" data-action="catalog-manual">Add a title yourself +</button>`;}finally{if(button.isConnected)button.disabled=false;}}
function catalogSourceLinks(item){const rows=[{source:item.source,sourceUrl:item.sourceUrl},...(Array.isArray(item.catalogSources)?item.catalogSources:[])],seen=new Set();return rows.filter(row=>{const url=safeUrl(row?.sourceUrl);if(!/^https?:\/\//.test(url)||seen.has(url))return false;seen.add(url);return true;}).slice(0,3).map(row=>`<a class="catalog-source" href="${e(safeUrl(row.sourceUrl))}" target="_blank" rel="noopener noreferrer">${e(row.source||'Original source')} ↗</a>`).join(' ');}
function renderCatalogItems(data=catalogState.response){const host=$('#catalog-results-view');if(!host)return;const items=catalogState.mode==='search'&&catalogState.genre?catalogState.items.filter(x=>(x.genres||[]).includes(catalogState.genre)):catalogState.items;const count=catalogState.mode==='browse'?(data.total??items.length):items.length;host.innerHTML=`<div class="catalog-count">${count} ${catalogState.mode==='browse'?'saved title':'result'}${count===1?'':'s'}${data.provider?' · '+e(data.provider):''}${data.cached?' · saved catalog result':''}${data.stale?' · provider is temporarily unavailable':''}</div>${(data.warnings||[]).map(message=>`<p class="source-hint">${e(message)}</p>`).join('')}${items.length?`<div class="discover-grid">${items.map(item=>{const index=catalogState.items.indexOf(item);return `<article class="catalog-title type-${e(item.type)}"><button class="catalog-cover" data-action="catalog-pick" data-value="${index}" aria-label="Add ${e(item.title)} to your shelf">${cover(item)}</button><h2 dir="auto">${e(item.title)}</h2><p dir="auto">${e(item.creator)}${item.year?' · '+e(item.year):''}</p>${genreChips(item)}<div class="catalog-trace-score">${traceRatingPanel(item.publicScores,item.type)}</div><button class="text-button" data-action="catalog-pick" data-value="${index}">Add to shelf +</button>${catalogSourceLinks(item)}</article>`;}).join('')}</div>`:'<div class="circle-empty"><h2>A little room for discovery.</h2><p>Search above to bring real titles and their details into the catalog.</p></div>'}<div class="feed-pagination">${catalogState.page?'<button class="button secondary" data-action="catalog-page" data-value="-1">← Previous</button>':''}${data.hasMore?'<button class="button secondary" data-action="catalog-page" data-value="1">Next titles →</button>':''}</div>`;refreshCatalogScores(host,items);}
main.addEventListener('submit',ev=>{if(ev.target.id==='discover-search'){ev.preventDefault();catalogState.query=ev.target.elements.query.value.trim();catalogState.mode=catalogState.query?'search':'browse';catalogState.genre='';catalogState.page=0;loadCatalog();}});
main.addEventListener('change',ev=>{if(ev.target.id==='discover-kind'){catalogState.kind=ev.target.value;catalogState.genre='';catalogState.page=0;loadCatalog();}if(ev.target.id==='catalog-genre'){catalogState.genre=ev.target.value;catalogState.page=0;if(catalogState.mode==='browse')loadCatalog();else renderCatalogItems();}});
main.addEventListener('click',ev=>{const button=ev.target.closest('[data-action]');if(!button)return;const {action,value}=button.dataset;if(action==='browse-catalog'){catalogState.mode='browse';catalogState.query='';catalogState.genre='';catalogState.page=0;$('#discover-search').elements.query.value='';loadCatalog();}if(action==='catalog-page'){catalogState.page=Math.max(0,catalogState.page+Number(value));loadCatalog();}if(action==='catalog-pick'){const item=catalogState.items[Number(value)];if(item){entryDraft={...item,id:uid(),rating:0,date:'',status:'want',notes:'',history:[],addedAt:new Date().toISOString()};renderEntryForm();logDialog.showModal();}}if(action==='catalog-manual')openLog(catalogState.kind);});
function importsPage(){main.innerHTML=heading('Connections','Bring your public profiles into Trace.','No need to<br>start from scratch.<br><span>↙</span>')+
 libraryAccountPanel()+connectionsPanel()+`<details class="advanced-import"><summary>Import a full history or restore a backup</summary><p class="import-intro">Bring your ratings, notes, dates, and saved lists into one place. Your files are read in this browser. Preview everything before adding it to your shelf.</p>
 <div class="import-options"><article class="import-provider"><div class="provider-logo letterboxd-dots" aria-hidden="true"><i></i><i></i><i></i></div><h2>Letterboxd</h2><p>Open Settings → Data → Export your data. Unzip the download and choose diary.csv, ratings.csv, reviews.csv, watched.csv, or watchlist.csv. You can select several CSVs together.</p><a href="https://letterboxd.com/settings/data/" target="_blank" rel="noopener noreferrer">Open Letterboxd export ↗</a></article>
 <article class="import-provider"><div class="provider-logo" aria-hidden="true">goodreads</div><h2>Goodreads</h2><p>On the Goodreads website, go to My Books → Import and export → Export Library. Upload the CSV to bring your books, shelves, ratings, and reviews.</p><a href="https://www.goodreads.com/review/import" target="_blank" rel="noopener noreferrer">Open Goodreads export ↗</a></article></div>
 <div class="import-toolbar"><label for="import-source">Import from</label><select id="import-source"><option value="auto">Detect automatically</option><option value="letterboxd">Letterboxd</option><option value="goodreads">Goodreads</option><option value="imdb">IMDb ratings</option><option value="generic">Another service / Trace CSV</option></select><button class="text-button" data-action="csv-template">Download CSV template</button><button class="text-button" data-action="backup">Back up my collection</button></div>
 <label class="upload-area">${icon('upload')}<strong>Give your collection a new home</strong><span>Choose CSV files or a Trace JSON backup · up to 10 MB per file</span><input id="import-file" type="file" accept=".csv,.json,text/csv,application/json" multiple aria-label="Choose collection files"></label>
 <div id="import-feedback">${importFeedback()}</div><p class="import-help">Duplicates are matched by category, title, and year, with creator details when available. Existing ratings and notes are kept; missing details and diary dates are filled in. Undated imports stay undated. ZIP files must be extracted first.<br>Albums, memories, or exports from another service? Match the columns in the CSV template. Public feeds can omit older or private entries; exports remain available for a complete history.</p></details>`;$('#import-source').value=state.importSource;}
function connectionsPanel(){return `<p class="connection-intro">Link a public profile to import available ratings, notes, and dates. Trace keeps your existing ratings and notes.</p><div class="connection-grid">${[['letterboxd','Letterboxd','Username or profile URL','letterboxd.com/yourname'],['goodreads','Goodreads','Profile URL or user ID','goodreads.com/user/show/12345']].map(([service,name,label,placeholder])=>{const saved=connections[service];return `<section class="connection-card"><header><h2>${name}</h2><span class="connection-state">${saved?'Public profile linked':'Not linked'}</span></header><form data-connect-service="${service}"><label class="field"><span>${label}</span><input name="profile" required maxlength="200" value="${e(saved?.profileUrl||'')}" placeholder="${placeholder}"></label><button class="button primary" type="submit">${saved?'Refresh profile':'Link profile'}</button>${saved?`<button class="text-button" type="button" data-action="unlink-profile" data-value="${service}">Unlink</button>`:''}</form>${saved?`<p class="connection-last">${saved.count} feed entries · Updated ${e(new Date(saved.syncedAt).toLocaleDateString())}<br><a href="${e(safeUrl(saved.profileUrl))}" target="_blank" rel="noopener noreferrer">Open public profile ↗</a></p>`:''}<div data-connection-feedback="${service}">${connectionErrors[service]?`<p class="error-message" role="alert">${e(connectionErrors[service])}</p>`:''}</div></section>`;}).join('')}</div><p class="connection-limit">Public feeds import recent entries and reviews. For every Letterboxd rating, import ratings.csv from your full export. Posting edits back to Letterboxd needs approved API access and OAuth; it is not enabled by a profile link.</p>`;}
async function connectProfile(service,profile,button=null,quiet=false){delete connectionErrors[service];if(button){button.disabled=true;button.textContent='Connecting…';}const feedback=document.querySelector(`[data-connection-feedback="${service}"]`);if(feedback)feedback.textContent='';try{
 const response=await apiFetch('/api/profile-connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service,profile})});const data=await response.json();if(!response.ok)throw Error(data.error||'Could not read this public profile.');const incoming=validateEntries(data.items);const merged=mergeEntries(collection.started?collection.entries:[],incoming);if(!await persist({started:true,entries:merged.entries}))return;connections[service]={identifier:data.identifier,profileUrl:data.profileUrl,count:data.items.length,syncedAt:new Date().toISOString()};try{localStorage.setItem(preferenceKey('connections'),JSON.stringify(connections));}catch{notify('Entries imported, but the profile connection could not be saved.');return;}if(!quiet)notify(`Profile linked. ${merged.added} entries added, ${merged.merged} enriched.`);render();
 }catch(err){if(!quiet){connectionErrors[service]=err.message;const currentFeedback=document.querySelector(`[data-connection-feedback="${service}"]`);if(currentFeedback)currentFeedback.innerHTML=`<p class="error-message" role="alert">${e(err.message)}</p>`;else notify(err.message);}}finally{if(button){button.disabled=false;button.textContent=connections[service]?'Refresh profile':'Link profile';}}}
main.addEventListener('submit',ev=>{const form=ev.target.closest('[data-connect-service]');if(!form)return;ev.preventDefault();connectProfile(form.dataset.connectService,form.elements.profile.value.trim(),form.querySelector('[type="submit"]'));});
function importFeedback(){if(state.importError)return `<div class="error-message" role="alert">${e(state.importError)}</div>`;if(state.importNotice)return `<div class="success-message">${e(state.importNotice)}</div>`;if(!state.pending)return '';const p=state.pending;if(!p.preview||p.base!==collection.entries||p.started!==collection.started){p.preview=mergeEntries(collection.started?collection.entries:[],p.entries);p.base=collection.entries;p.started=collection.started;}const merged=p.preview;return `<div class="import-preview"><p class="eyebrow">READY WHEN YOU ARE</p><h2>A few familiar favorites.</h2><p>${p.entries.length} rows · ${merged.added} new items · ${merged.merged} entries enriched · ${merged.duplicates} unchanged duplicates${p.skipped?` · ${p.skipped} empty rows skipped`:''}</p><div class="preview-items">${p.entries.slice(0,60).map(x=>`<div class="preview-item"><span>${e(x.title)} <small>${x.year?`(${e(x.year)})`:''}</small></span><span>${singular[x.type]} · ${e(statusLabel(x))}${x.date?` · ${x.date}`:''}</span></div>`).join('')}${p.entries.length>60?`<p class="import-help">Showing the first 60 of ${p.entries.length} rows.</p>`:''}</div><button class="button primary" data-action="confirm-import">Add to my shelf ${icon('arrow')}</button> <button class="text-button" data-action="cancel-import">Cancel</button></div>`;}
let importSequence=0;
async function readImport(files){const seq=++importSequence;state.pending=null;state.importError='';state.importNotice='';const feedback=$('#import-feedback');feedback.innerHTML='<p class="loading-pulse">Finding your familiar favorites…</p>';try{let all=[],skipped=0;for(const file of files){if(file.size>10*1024*1024)throw Error(`${file.name} is over 10 MB. Choose a smaller export.`);if(/\.zip$/i.test(file.name))throw Error('Unzip your Letterboxd export first, then select the CSV files inside.');const text=await file.text();if(/\.json$/i.test(file.name)){const data=JSON.parse(text);if(data.format!=='trace-backup-v1'||!Array.isArray(data.entries))throw Error('Choose a Trace JSON backup or a supported CSV export.');all.push(...validateEntries(data.entries));}else{const parsed=normalizeImport(text,file.name,state.importSource);all.push(...parsed.entries);skipped+=parsed.skipped;}if(all.length>30000)throw Error('Import up to 30,000 entries at a time.');}if(!all.length)throw Error('No entries found in these files.');if(seq!==importSequence)return;state.pending={entries:all,skipped};}catch(err){if(seq!==importSequence)return;state.importError=err instanceof SyntaxError?'The backup is not valid JSON. Choose the original Trace backup.':err.message;}if(state.page==='imports')$('#import-feedback').innerHTML=importFeedback();}
function periodOptions(){const dates=entries().filter(x=>x.date).map(x=>x.date);const months=[...new Set([today().slice(0,7),...dates.map(x=>x.slice(0,7))])].sort().reverse();const years=[...new Set([today().slice(0,4),...dates.map(x=>x.slice(0,4))])].sort().reverse();return `<option value="all">All time</option>${years.map(x=>`<option value="${x}">${x} · Year in review</option>`).join('')}${months.map(x=>`<option value="${x}">${new Date(x+'-01T12:00:00').toLocaleDateString('en-US',{month:'long',year:'numeric'})}</option>`).join('')}`;}
function wallCandidates(){return recapEntries(entries(),wallConfig.period);}
function selectedWall(){return selectTracewall(wallCandidates(),wallConfig.selected);}
const wallColumns=()=>matchMedia('(max-width:640px)').matches?2:3;
let wallColumnsRendered=0;
let wallPageIndex=0;
let socialWallPreview=null;
const wallPicker={category:'all',query:'',limit:24};
const wallFormat=()=>Object.hasOwn(TRACEWALL_FORMATS,wallConfig.format)?wallConfig.format:'story';
const wallTitle=()=>wallConfig.title||'The things that stayed.';
const wallPalettes={
 paper:{name:'Parchment',paper:TRACEWALL_THEMES.paper,edge:'#e8ccaa',ink:'#482d1c',muted:'#69452d',mat:'#e9cda5',rule:'#956c4c'},
 sage:{name:'Sage',paper:TRACEWALL_THEMES.sage,edge:'#c5cdae',ink:'#293527',muted:'#40513a',mat:'#c8d3af',rule:'#727e56'},
 rose:{name:'Clay',paper:TRACEWALL_THEMES.rose,edge:'#e2b9a8',ink:'#4b2629',muted:'#6c3a39',mat:'#e8bdab',rule:'#a36761'},
 blue:{name:'Faded blue',paper:'#dfe7e7',edge:'#b8c9d3',ink:'#24394a',muted:'#344f63',mat:'#baceda',rule:'#5d7789'},
 plum:{name:'Plum',paper:'#694451',edge:'#482d39',ink:'#fff2e3',muted:'#f2d7c9',mat:'#926978',rule:'#d2a18b',dark:true},
 night:{name:'After dark',paper:'#2d3942',edge:'#19252e',ink:'#fff3de',muted:'#e9ccb0',mat:'#4b5e66',rule:'#be9872',dark:true},
};
const customWallColor=()=>typeof wallConfig.customColor==='string'&&/^#[\da-f]{6}$/i.test(wallConfig.customColor)?wallConfig.customColor:'#c4a18a';
function wallPalette(){
 if(wallConfig.theme!=='custom'){
  return Object.hasOwn(wallPalettes,wallConfig.theme)?wallPalettes[wallConfig.theme]:wallPalettes.paper;
 }
 const paper=customWallColor();
 const rgb=paper.slice(1).match(/../g).map(value=>parseInt(value,16));
 const [r,g,b]=rgb.map(value=>{
  const channel=value/255;
  return channel<=.04045?channel/12.92:((channel+.055)/1.055)**2.4;
 });
 const dark=.2126*r+.7152*g+.0722*b<.179;
 const ink=dark?'#ffffff':'#000000';
 const tint=(target,amount)=>'#'+rgb.map(channel=>Math.round(channel+(target-channel)*amount).toString(16).padStart(2,'0')).join('');
 return {name:'Custom',paper,edge:tint(dark?0:255,.16),ink,muted:ink,mat:tint(dark?255:0,.17),rule:ink,dark};
}
function wallPaletteControls(){
 const choices=Object.entries(wallPalettes);
 const active=Object.hasOwn(wallPalettes,wallConfig.theme)?wallConfig.theme:'paper';
 return `<div class="color-swatches" role="group" aria-label="Recap paper color" style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px">
 ${choices.map(([key,palette])=>{
  const pressed=wallConfig.theme!=='custom'&&active===key;
  return `<button class="text-button" data-action="wall-theme" data-value="${key}" aria-pressed="${pressed}" style="display:grid;justify-items:center;gap:6px;padding:5px 0;text-decoration:none;font-weight:${pressed?700:400}"><span aria-hidden="true" class="swatch ${pressed?'active':''}" style="background:${palette.paper}"></span>${palette.name}</button>`;
 }).join('')}</div>
 <label class="field" style="display:flex;align-items:center;gap:8px;margin:12px 0 0"><span>${wallConfig.theme==='custom'?'Custom paper ✓':'Custom paper'}</span><input id="wall-custom-color" type="color" value="${customWallColor()}" style="width:42px;height:32px;padding:3px"></label>`;
}
function wallPeriod(){return wallConfig.period==='all'?'A life, collected':wallConfig.period.length===4?wallConfig.period:new Date(wallConfig.period+'-01T12:00:00').toLocaleDateString('en-US',{month:'long',year:'numeric'});}
function wallFooter(list){return `${list.length} keepsakes · ${new Set(list.map(categoryFor)).size} corners of life`;}
function wallPosition(box,width,height){return `left:${box.x/width*100}%;top:${box.y/height*100}%;width:${box.width/width*100}%;height:${box.height/height*100}%`;}
function wallTile(item,slot,layout,palette){const art=tracewallArtifact(item.type);return `<figure class="keepsake-item kind-${art.kind}" style="${wallPosition(slot,layout.width,layout.height)};--tilt:${slot.tilt}deg;--tile-scale:${slot.scale}"><span class="keepsake-object kind-${art.kind}"><span class="keepsake-art" style="${wallPosition(art.image,320,374)}">${cover(item)}</span></span><figcaption dir="auto">${e(item.title)}</figcaption><span class="keepsake-kind" style="color:${palette.muted}">${art.label}</span></figure>`;}
function socialWallMarkup(card){
 const name=TRACEWALL_FORMATS[card.format];
 const description=card.picks.map(item=>item.title).join(', ');
 return `<div class="wall-share-preview" data-format="${card.format}">
 <div class="wall-share-label"><strong>${name}</strong><span>${card.width} × ${card.height}</span></div>
 ${card.pages>1?`<div class="wall-share-pages" role="group" aria-label="Recap cards"><button class="button secondary small" data-action="wall-card" data-value="-1" ${card.index===0?'disabled':''}>← Previous</button><span aria-live="polite">Card ${card.index+1} of ${card.pages}</span><button class="button secondary small" data-action="wall-card" data-value="1" ${card.index===card.pages-1?'disabled':''}>Next →</button></div>`:''}
 ${card.picks.length?`<canvas id="wall-share-canvas" class="wall-share-sheet" width="${card.width}" height="${card.height}" role="img" aria-label="${e(name+': '+description)}"></canvas><p class="wall-share-status" role="status">Putting your keepsakes together…</p>`:'<p class="wall-share-empty">Nothing kept here yet. Choose another period, or log something you loved.</p>'}
 </div>`;
}
function wallPickerMarkup(){
 return `<section id="wall-picker" class="wall-selection" aria-labelledby="wall-picker-title" style="padding:18px clamp(12px,2.5vw,24px);background:#f4e5cb;border:1px solid #c9a77e;border-radius:10px">
 <h3 id="wall-picker-title">Pick your keepsakes <span id="wall-picker-count" class="type-badge" aria-live="polite"></span></h3>
 <p class="source-hint">Tap a cover to add or remove it. Your picks appear in order below.</p>
 <div id="wall-picked" role="group" aria-label="Selected keepsakes in order" style="display:flex;gap:8px;overflow-x:auto;padding:9px 2px 12px"></div>
 <div class="category-tabs" role="group" aria-label="Keepsake categories">${categoryOptions.map(([id,label])=>`<button type="button" data-action="wall-category" data-value="${id}">${label}<span class="tab-count"></span></button>`).join('')}</div>
 <label class="search-box" style="margin:12px 0">${icon('search')}<span class="sr-only">Search your logged items</span><input id="wall-picker-search" type="search" dir="auto" placeholder="Find a title or creator…" value="${e(wallPicker.query)}"></label>
 <div id="wall-picker-results"></div>
 <div style="display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:12px"><button class="text-button" data-action="wall-find">Find more in Discover</button><a class="text-button" href="#imports">Import from Goodreads or Letterboxd</a></div>
 </section>`;
}
function wallPickerCard(item,order){
 const picked=order>0;
 const kind=categoryFor(item);
 return `<button type="button" class="catalog-title type-${e(kind)}" data-action="wall-select" data-id="${e(item.id)}" aria-label="${e(item.title)}" aria-pressed="${picked}" style="display:flex;flex-direction:column;gap:7px;position:relative;text-align:start;padding:7px;border:1px solid ${picked?'#95633e':'#c9a77e'};border-radius:7px;background:${picked?'#efdbb4':'#fff6e5'}">
 <span class="catalog-cover" style="aspect-ratio:4/5">${cover(item)}</span>
 <span aria-hidden="true" class="type-badge" style="position:absolute;top:12px;right:12px;background:${picked?'#66482c':'#fff6e5'};color:${picked?'#fff6e5':'#66482c'};box-shadow:0 1px 3px #442b2140">${picked?'✓ '+order:'+'}</span>
 <strong dir="auto" style="overflow-wrap:anywhere;font-size:12px;line-height:1.35">${e(item.title)}</strong>
 <small dir="auto" style="color:#775238;overflow-wrap:anywhere">${e(item.creator||tracewallArtifact(item.type).label)}</small>
 </button>`;
}
function renderWallPicker(selected=selectedWall(),candidates=wallCandidates()){
 const results=$('#wall-picker-results');
 if(!results)return;
 const picked=$('#wall-picked');
 const scrollLeft=picked.scrollLeft;
 const order=new Map(selected.map((item,index)=>[item.id,index+1]));
 $('#wall-picker-count').textContent=`${selected.length} / 12 selected`;
 picked.innerHTML=selected.length?selected.map((item,index)=>`<button type="button" class="button secondary small" data-action="wall-select" data-wall-tray data-id="${e(item.id)}" aria-label="Remove ${e(item.title)}" title="${e(item.title)}" style="display:grid;grid-template-columns:32px 1fr;gap:7px;align-items:center;flex:0 0 112px;min-width:0;padding:6px;text-align:start"><span class="list-thumb" style="width:32px;height:42px">${cover(item)}</span><span style="min-width:0"><span style="display:block;color:#775238">${index+1} · ×</span><span dir="auto" style="display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e(item.title)}</span></span></button>`).join(''):'<p class="source-hint">Your tray is empty. Pick a cover below.</p>';
 picked.scrollLeft=scrollLeft;
 const counts={all:candidates.length};
 for(const item of candidates){const kind=categoryFor(item);counts[kind]=(counts[kind]||0)+1;}
 for(const tab of main.querySelectorAll('[data-action="wall-category"]')){
  const active=tab.dataset.value===wallPicker.category;
  tab.classList.toggle('active',active);
  tab.setAttribute('aria-pressed',active);
  tab.querySelector('.tab-count').textContent=counts[tab.dataset.value]||0;
 }
 const query=wallPicker.query.trim().toLocaleLowerCase();
 const items=candidates.filter(item=>(wallPicker.category==='all'||categoryFor(item)===wallPicker.category)&&`${item.title} ${item.creator||''}`.toLocaleLowerCase().includes(query));
 const visible=items.slice(0,wallPicker.limit);
 const message=query?'No logged items match that search.':!candidates.length?'No completed items in this period. Try All time for undated imports.':'No logged items in this category for this period.';
 results.innerHTML=visible.length?`<p class="source-hint" role="status" style="margin:0 0 8px">${visible.length} of ${items.length} logged items</p><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,108px),1fr));gap:10px">${visible.map(item=>wallPickerCard(item,order.get(item.id)||0)).join('')}</div>${items.length>visible.length?'<button type="button" class="button secondary small" data-action="wall-more" style="margin-top:12px">Show more</button>':''}`:`<p class="source-hint" role="status">${message}</p>`;
 const find=main.querySelector('[data-action="wall-find"]');
 find.textContent=wallPicker.category==='memory'?'Add a memory':wallPicker.category==='book'?'Find more books':'Find more in Discover';
}
function wallPage(){const opts=periodOptions();if(!opts.includes(`value="${wallConfig.period}"`))wallConfig.period='all';main.innerHTML=heading('Tracewall','Create a recap from your media and memories.','Your life.<br>A little collage.<br><span>✧</span>')+
 `<div class="wall-controls"><label class="field"><span>Give this chapter a name</span><input id="wall-title" maxlength="70" value="${e(wallConfig.title)}" placeholder="The things that stayed."></label><label class="field"><span>Look back on</span><select id="wall-period">${opts}</select></label><label class="field"><span>Image format</span><select id="wall-format">${Object.entries(TRACEWALL_FORMATS).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')}</select></label><button class="button primary" data-action="export-wall" ${!selectedWall().length?'disabled':''}>${icon('download')} Save as image</button></div><div id="wall-body"></div>${wallPickerMarkup()}${sampleStrip()}`;$('#wall-period').value=wallConfig.period;$('#wall-format').value=wallFormat();renderWall();}
function renderWall(){
 const selected=selectedWall(),candidates=wallCandidates();
 const card=tracewallShareCard(selected,wallFormat(),wallPageIndex);
 const layout=tracewallLayout(card?0:selected.length,wallColumns());wallColumnsRendered=layout.columns;
 if(card)wallPageIndex=card.index;
 socialWallPreview=null;
 const counts={};for(const item of candidates)counts[categoryFor(item)]=(counts[categoryFor(item)]||0)+1;
 const palette=wallPalette();
 $('#wall-body').innerHTML=`<div class="wall-layout">${card?socialWallMarkup(card):`<section class="tracewall keepsake-paper" id="tracewall" aria-label="Your recap collage" style="aspect-ratio:${layout.width}/${layout.height};background:${palette.paper};color:${palette.ink};border-color:${palette.rule};--wall-small:${layout.smallText/12}cqw">
 <canvas id="wall-paper-texture" width="${layout.width/2}" height="${Math.round(layout.height/2)}" aria-hidden="true" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none"></canvas>
 <div class="keepsake-top" style="color:${palette.muted};top:${42/layout.height*100}%"><img src="/assets/trace-wordmark.png" alt="Trace" class="keepsake-logo"><span>${e(wallPeriod())}${isDemo()?' · SAMPLE':''}</span></div>
 <h2 id="wall-display-title" style="top:${132/layout.height*100}%">${e(wallConfig.title||'The things that stayed.')}</h2><p class="keepsake-subtitle" style="color:${palette.muted};top:${264/layout.height*100}%">read, watched, played. lived.</p>
 ${selected.length?`<div class="keepsake-collage">${selected.map((item,i)=>wallTile(item,layout.slots[i],layout,palette)).join('')}</div>`:`<p class="keepsake-empty" style="color:${palette.muted}">Nothing kept here yet.<br>Choose another period, or log something you loved.</p>`}
 <div class="keepsake-footer" style="color:${palette.muted};border-color:${palette.rule};bottom:${42/layout.height*100}%"><span>${wallFooter(selected)}</span><em>good things leave a trace.</em></div></section>`}
 <aside class="wall-aside"><div><h3>A little of everything.</h3><p>${wallConfig.selected===null?'A mix from the things you logged.':'Your picks, in the order you chose them.'} Choose up to 12 keepsakes.</p><button class="text-button" data-action="wall-mix" ${wallConfig.selected===null?'disabled':''}>Use an automatic mix</button>${wallPaletteControls()}</div>
 <div>${Object.entries(counts).map(([kind,count])=>`<div class="wall-stat"><b>${count}</b><span>${{movie:'movies watched',tv:'shows watched',book:'books read',album:'albums heard',memory:'moments kept'}[kind]}</span></div>`).join('')}</div></aside></div>
 <p class="wall-notice">The image stays on your device until you share it. Unavailable covers become title cards.</p>`;
 renderWallPicker(selected,candidates);
 const button=main.querySelector('[data-action="export-wall"]');
 button.disabled=Boolean(card)||!selected.length;
 if(card?.picks.length)prepareSocialWall(card,palette,button);
 const texture=$('#wall-paper-texture');
 const context=texture?.getContext('2d');
 if(context){
  context.scale(texture.width/layout.width,texture.height/layout.height);
  paintWallPaper(context,layout.width,layout.height,palette);
 }
}
function render(){document.querySelectorAll('[data-nav]').forEach(a=>{const active=a.dataset.nav===state.page;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});({shelf:shelfPage,diary:diaryPage,wall:wallPage,imports:importsPage,discover:discoverPage,social:()=>social.render(state.socialProfile||'')}[state.page]||shelfPage)();}
function route(){state.previewId=null;const [route,profile]=location.hash.slice(1).split('/');state.page=['shelf','diary','wall','imports','discover','social'].includes(route)?route:'shelf';state.socialProfile=state.page==='social'&&/^[a-zA-Z0-9_]{3,24}$/.test(profile||'')?profile:'';render();}
window.addEventListener('hashchange',route);
async function closeModal(id){const dialog=$(id);if(!dialog.open||dialog.dataset.closing)return;dialog.dataset.closing='true';try{if(!matchMedia('(prefers-reduced-motion: reduce)').matches)await dialog.animate([{opacity:1,transform:'translateY(0)'},{opacity:0,transform:'translateY(7px)'}],{duration:110,easing:'ease-in'}).finished;dialog.close();}finally{delete dialog.dataset.closing;}if(id==='#log-dialog'){searchController?.abort();searchSequence++;}}
document.addEventListener('click',ev=>{const closer=ev.target.closest('[data-close]');if(closer)closeModal(closer.dataset.close);});
for(const d of [logDialog,detailDialog]){d.addEventListener('cancel',ev=>{ev.preventDefault();closeModal('#'+d.id);});d.addEventListener('click',ev=>{if(ev.target===d){const r=d.getBoundingClientRect();if(ev.clientX<r.left||ev.clientX>r.right||ev.clientY<r.top||ev.clientY>r.bottom)closeModal('#'+d.id);}});d.addEventListener('close',()=>{if(d===logDialog){searchController?.abort();searchSequence++;}});}
function openLog(type='movie'){logType=type;entryDraft=null;searchResults=[];if(type==='memory'){entryDraft={id:uid(),type:'memory',title:'',attachments:[],source:'Personal memory'};renderEntryForm();}else renderSearchModal();logDialog.showModal();}
function modalHead(title,sub,dialog='#log-dialog'){return `<div class="modal-head"><div><h2 dir="auto">${title}</h2><p>${sub}</p></div><button class="close-button" data-close="${dialog}" aria-label="Close">×</button></div>`;}
const providerName={movie:'Wikidata / Wikipedia',tv:'TVmaze + Wikidata',anime:'Kitsu',book:'Open Library',album:'Apple + MusicBrainz · albums only',};
function renderSearchModal(){if(logType==='memory')logType='movie';searchController?.abort();searchSequence++;$('#log-content').innerHTML=modalHead('Add to shelf','Search for a title, or add an item yourself.')+`<div class="modal-body"><div class="type-picker" role="group" aria-label="What are you logging?">${Object.keys(TYPES).map(t=>`<button data-log-type="${t}" class="${logType===t?'active':''}" aria-pressed="${logType===t}">${TYPES[t]}</button>`).join('')}</div><form id="catalog-search" class="modal-search">${icon('search')}<label class="sr-only" for="catalog-query">Search ${TYPES[logType].toLowerCase()}</label><input id="catalog-query" dir="auto" type="search" minlength="2" maxlength="200" required placeholder="${{movie:'A film that stayed with you…',tv:'Find a show…',anime:'Find an anime…',book:'A title or an author…',album:'An album or an artist…',restaurant:'Restaurant name and city…',place:'A place, landmark, or city…'}[logType]}"><button class="button primary small" type="submit">Search</button></form><p class="source-hint">Searches ${providerName[logType]}. Select a result to keep its cover and details.</p><div id="catalog-results" class="search-results"></div><div class="manual-entry"><span>Some things aren’t in a catalog.</span><button class="text-button" data-action="manual">Add it yourself ${icon('arrow')}</button></div></div>`;}
logDialog.addEventListener('click',ev=>{const type=ev.target.closest('[data-log-type]');if(type){logType=type.dataset.logType;if(logType==='memory'){entryDraft={id:uid(),type:'memory',title:'',attachments:[],source:'Personal memory'};renderEntryForm();}else{renderSearchModal();$('#catalog-query').focus();}}const result=ev.target.closest('[data-search-index]');if(result){entryDraft={...searchResults[Number(result.dataset.searchIndex)],id:uid()};renderEntryForm();}const action=ev.target.closest('[data-action]')?.dataset.action;if(action==='manual'){entryDraft={id:uid(),title:$('#catalog-query')?.value.trim()||'',type:logType,creator:'',year:'',image:'',source:'Manual entry'};renderEntryForm();}if(action==='back-search')renderSearchModal();});
logDialog.addEventListener('submit',async ev=>{if(ev.target.id==='catalog-search'){ev.preventDefault();await catalogSearch($('#catalog-query').value.trim());}if(ev.target.id==='entry-form'){ev.preventDefault();saveEntry(ev.target,false);}});
async function catalogSearch(query){searchController?.abort();const controller=new AbortController();searchController=controller;const sequence=++searchSequence;const kind=logType;const host=$('#catalog-results');host.innerHTML='<div class="loading-pulse">Looking through the collection…</div>';const submit=$('#catalog-search button');submit.disabled=true;try{const response=await apiFetch(`/api/search?type=${encodeURIComponent(kind)}&q=${encodeURIComponent(query)}`,{signal:controller.signal});const result=await response.json();if(!response.ok)throw Error(result.error||'Search is unavailable. Try again or add it manually.');if(sequence!==searchSequence)return;searchResults=result.items.map(x=>({...x,id:uid()}));host.innerHTML=searchResults.length?searchResults.map((x,i)=>`<button class="search-result" data-search-index="${i}">${x.image?`<img src="${e(imageUrl(x.image))}" alt="" data-fallback="${e(x.id)}">`:`<span class="result-placeholder">${icon(logType==='album'?'music':logType==='book'?'book':'film')}</span>`}<div><strong dir="auto">${e(x.title)}</strong><small dir="auto">${e(x.creator)}${x.year?` · ${e(x.year)}`:''}</small></div><span>+</span></button>`).join(''):'<p class="source-hint">No matches this time. Try a different title or add it yourself.</p>';}catch(err){if(sequence===searchSequence && err.name!=='AbortError')host.innerHTML=`<div class="error-message" role="alert">${e(err.message)}</div>`;}finally{if(sequence===searchSequence)submit.disabled=false;}}
function fields(x,isEdit=false){const memory=x.type==='memory';return `
 <div class="form-row"><label class="field"><span>${'Title'}</span><input dir="auto" name="title" required maxlength="300" value="${e(x.title||'')}" placeholder="${memory?'That rainy Sunday…':'What would you like to remember?'}"></label><label class="field"><span>Category</span><select name="type">${Object.entries(TYPES).map(([k,v])=>`<option value="${k}" ${x.type===k?'selected':''}>${v}</option>`).join('')}</select></label></div>
 <div class="form-row" data-media-only><label class="field"><span>Creator</span><input dir="auto" name="creator" maxlength="300" value="${e(x.creator||'')}" placeholder="An artist, author, or director"></label><label class="field"><span>Release year <small>(optional)</small></span><input name="year" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" value="${e(x.year||'')}" placeholder="2026"></label></div>
 <div class="form-row"><label class="field" data-media-only><span>On your shelf as</span><select name="status"><option value="done" ${x.status==='done'||!x.status?'selected':''}>Logged / experienced</option><option value="progress" ${x.status==='progress'?'selected':''}>In progress</option><option value="want" ${x.status==='want'?'selected':''}>Want to try</option></select></label><label class="field" data-memory-only><span>A little context <small>(optional)</small></span><input name="memoryContext" maxlength="300" value="${e(memory?x.creator||'':'')}" placeholder="At home, with a friend, somewhere new…"></label><label class="field"><span>Date <small>(optional)</small></span><input type="date" name="date" value="${e(isEdit?x.date||'':x.status==='want'?'':today())}" max="${today()}"></label></div>
 <div class="field" data-media-only><span id="rating-label">Your rating <small>(optional)</small></span><div class="rating-control" role="group" aria-labelledby="rating-label">${[1,2,3,4,5].map(n=>`<button type="button" data-rating="${n}" class="${(x.rating||0)>=n?'filled':''}" aria-label="${n} out of 5 stars" aria-pressed="${(x.rating||0)===n}">★</button>`).join('')}<button type="button" class="clear-rating" data-rating="0">Clear</button><span id="rating-value" class="source-hint">${x.rating?x.rating+' / 5':''}</span></div><input type="hidden" name="rating" value="${x.rating||0}"></div>
 <label class="field"><span>${memory?'Tags':'Genres'}</span><input name="genres" maxlength="400" value="${e((x.genres||[]).join(', '))}" placeholder="${memory?'A quiet day, With friends':'Drama, Science Fiction…'}"><small class="source-hint">Separate with commas.</small></label>
 <label class="field"><span>Notes</span><textarea dir="auto" name="notes" maxlength="20000" placeholder="${memory?'What do you want to remember?':'What will you remember about this one?'}">${e(x.notes||'')}</textarea></label>
 <details class="field photo-field" ${memory?'open':''}><summary class="source-hint">${memory?'A photo from the day':'Cover or photo'} <small>(optional)</small></summary><label class="field"><span>Choose a photo · up to 25 MB</span><input type="file" name="photo" accept="image/jpeg,image/png,image/webp"></label><label class="field" data-memory-only><span>Take a photo</span><input type="file" name="cameraPhoto" accept="image/*" capture="environment"></label><label class="field"><span>Or use an image URL</span><input type="url" name="image" value="${/^https?:/.test(x.image||'')?e(x.image):''}" placeholder="https://…"></label></details>
 <section class="memory-attachments" data-memory-only><h3>Attached media</h3><p>Choose up to 12 books, albums, films, or shows from your shelf.</p><label class="field"><span class="sr-only">Find something to attach</span><input type="search" data-attachment-search placeholder="Find a book, album, film, or show…"></label><div data-attachment-options></div><span class="source-hint" data-attachment-count></span></section>
 ${x.sourceUrl?`<p class="entry-source">${memory?'Original entry':'Found via'} <a href="${e(safeUrl(x.sourceUrl))}" target="_blank" rel="noopener noreferrer">${e(x.source||'Original source')} ↗</a></p>`:''}<div id="entry-error"></div><div class="form-actions"><label class="check-field"><input type="checkbox" name="favorite" ${x.favorite?'checked':''}> ${icon('heart')} ${'Favorite'}</label><div>${!isEdit?'<button type="button" class="text-button" data-action="back-search">Back</button>':''}<button class="button primary" type="submit">${isEdit?'Save changes':memory?'Keep this memory':'Add to my shelf'}</button></div></div>`;}
function prepareForm(form,x){form.traceLinks=normalizeAttachments(x.attachments);updateFormKind(form);}
function updateFormKind(form){const memory=form.elements.type.value==='memory';form.elements.title.closest('label').querySelector('span').textContent='Title';form.elements.title.placeholder=memory?'That rainy Sunday…':'What would you like to remember?';if(memory)form.querySelector('.photo-field').open=true;form.querySelector('[type="submit"]').textContent=form.id==='edit-form'?'Save changes':memory?'Keep this memory':'Add to my shelf';form.querySelectorAll('[data-media-only]').forEach(el=>{el.hidden=memory;el.querySelectorAll('input,select,button').forEach(input=>input.disabled=memory);});form.querySelectorAll('[data-memory-only]').forEach(el=>{el.hidden=!memory;el.querySelectorAll('input').forEach(input=>input.disabled=!memory);});if(memory)renderAttachments(form);}
function renderAttachments(form){const selected=form.traceLinks||[];const query=form.querySelector('[data-attachment-search]').value.toLowerCase();const shelf=entries().filter(x=>x.type!=='memory');const seen=new Set();const options=[...selected,...shelf].filter(x=>{const key=x.id||x.type+':'+x.title;if(seen.has(key))return false;seen.add(key);return true;});const filtered=options.filter(x=>`${x.title} ${x.creator}`.toLowerCase().includes(query));
 form.traceOptions=options;
 form.querySelector('[data-attachment-options]').innerHTML=filtered.slice(0,30).map(x=>`<label class="attachment-option"><input type="checkbox" data-attachment-id="${e(x.id||x.type+':'+x.title)}" ${selected.some(a=>a.id&&a.id===x.id||sameItem(a,x))?'checked':''}>${x.image?`<img src="${e(imageUrl(x.image))}" alt="" loading="lazy">`:`<span class="attachment-placeholder">${icon(x.type==='album'?'music':'book')}</span>`}<span><strong dir="auto">${e(x.title)}</strong><small>${singular[x.type]}${x.creator?` · ${e(x.creator)}`:''}</small></span></label>`).join('')||`<p class="attachment-empty">${shelf.length?'Nothing matched. Try a title or creator.':'Add a book, album, film, or show to your shelf first. You can attach it later.'}</p>`;
 form.querySelector('[data-attachment-count]').textContent=`${selected.length} / 12 attached${filtered.length>30?' · Showing 30 matches. Keep typing to narrow them down.':''}`;
}
for(const dialog of [logDialog,detailDialog]){
 dialog.addEventListener('change',ev=>{const form=ev.target.closest('form');if(!form)return;if(ev.target.name==='type'){updateFormKind(form);return;}
 if(ev.target.matches('input[name="photo"],input[name="cameraPhoto"]')&&ev.target.files.length){
  const other=ev.target.name==='photo'?'cameraPhoto':'photo';
  form.elements[other].value='';
 }
 if(ev.target.matches('[data-attachment-id]')){const item=(form.traceOptions||[]).find(x=>(x.id||x.type+':'+x.title)===ev.target.dataset.attachmentId);if(!item)return;if(ev.target.checked){if(form.traceLinks.length>=12){ev.target.checked=false;notify('Choose up to 12 things for one memory.');return;}form.traceLinks=normalizeAttachments([...form.traceLinks,item]);}else form.traceLinks=form.traceLinks.filter(x=>!(x.id&&x.id===item.id||sameItem(x,item)));renderAttachments(form);}});
 dialog.addEventListener('input',ev=>{if(ev.target.matches('[data-attachment-search]'))renderAttachments(ev.target.closest('form'));});
}
function renderEntryForm(){searchController?.abort();searchSequence++;const memory=entryDraft.type==='memory';$('#log-content').innerHTML=modalHead(memory?'Add a memory':'Log this title',memory?'Add a photo, a note, and media from your shelf.':'Add your rating, date, and notes.')+`<form id="entry-form" class="modal-body">${fields(entryDraft)}</form>`;prepareForm($('#entry-form'),entryDraft);}
function ratingClick(ev){const b=ev.target.closest('[data-rating]');if(!b)return;const form=b.closest('form');form.elements.rating.value=b.dataset.rating;form.querySelectorAll('[data-rating]').forEach(x=>{x.classList.toggle('filled',Number(x.dataset.rating)>0&&Number(x.dataset.rating)<=Number(b.dataset.rating));x.setAttribute('aria-pressed',String(x.dataset.rating===b.dataset.rating));});form.querySelector('#rating-value').textContent=Number(b.dataset.rating)?`${b.dataset.rating} / 5`:'';}
logDialog.addEventListener('click',ratingClick);detailDialog.addEventListener('click',ratingClick);
async function photoData(file){
 if(!['image/jpeg','image/png','image/webp'].includes(file.type))throw Error('Choose a JPEG, PNG, or WebP photo.');
 if(file.size>25*1024*1024)throw Error('Choose a photo smaller than 25 MB.');
 let bitmap;
 try{bitmap=await createImageBitmap(file);}catch{throw Error('Could not open this photo. Try a JPEG, PNG, or WebP image.');}
 try{
  const canvas=document.createElement('canvas');
  const scale=Math.min(1,1280/Math.max(bitmap.width,bitmap.height));
  canvas.width=Math.max(1,Math.round(bitmap.width*scale));
  canvas.height=Math.max(1,Math.round(bitmap.height*scale));
  const context=canvas.getContext('2d');
  context.fillStyle='#fff4df';
  context.fillRect(0,0,canvas.width,canvas.height);
  context.drawImage(bitmap,0,0,canvas.width,canvas.height);
  return canvas.toDataURL('image/jpeg',.82);
 }finally{bitmap.close();}
}
async function saveEntry(form,edit){const submit=form.querySelector('[type="submit"]');submit.disabled=true;try{
 const data=new FormData(form),type=data.get('type'),memory=type==='memory';
 const original=edit?collection.entries.find(x=>x.id===editingId):entryDraft;
 const owner=activeSession.user?.id;
 const cameraPhoto=memory?data.get('cameraPhoto'):null;
 const photo=cameraPhoto?.size?cameraPhoto:data.get('photo');
 const uploaded=photo?.size?await photoData(photo):'';
 if(!form.isConnected||!form.closest('dialog')?.open||owner!==activeSession.user?.id)return;
 const item={...original,id:edit?editingId:uid(),title:String(data.get('title')).trim(),creator:String(data.get(memory?'memoryContext':'creator')||'').trim(),type,year:memory?'':data.get('year'),status:memory?'done':data.get('status'),date:dateOnly(data.get('date')),rating:memory?0:Number(data.get('rating')),genres:normalizeGenres(String(data.get('genres')||'')),notes:String(data.get('notes')).trim(),favorite:data.get('favorite')==='on',image:uploaded||safeUrl(String(data.get('image')||''))||original?.image||'',attachments:memory?normalizeAttachments(form.traceLinks):[],addedAt:original?.addedAt||new Date().toISOString()};
 if(!item.title)throw Error('Give this memory a title.');if(item.date>today())throw Error('Choose today or an earlier date.');item.history=[...new Set([...(original?.history||[]),...(item.status==='done'&&item.date?[item.date]:[])])];const current=collection.started?collection.entries:[];
 const result=edit?current.map(x=>x.id===editingId?item:x):memory?[...current,item]:mergeEntries(current,[item]).entries;
 if(!await persist({started:true,entries:result}))return;await closeModal(edit?'#detail-dialog':'#log-dialog');render();notify(edit?'Changes saved.':memory?'Memory saved.':'Added to your shelf.');
 }catch(err){form.querySelector('#entry-error').innerHTML=`<p class="error-message" role="alert">${e(err.message)}</p>`;}finally{submit.disabled=false;}}
function attachedMedia(x){if(!x.attachments?.length)return '';return `<div class="attached-media"><span>Attached media</span>${x.attachments.map(a=>{const target=entries().find(item=>item.id===a.id||sameItem(item,a));return target?`<button type="button" data-linked-id="${e(target.id)}">${icon(a.type==='album'?'music':'book')}${e(a.title)}</button>`:`<span class="attached-snapshot">${e(a.title)}</span>`;}).join('')}</div>`;}
function selectPreviewCell(cell,item,active){
 cell.classList.toggle('is-preview',active);const button=cell.querySelector('.object-button');button.innerHTML=artifact(item,active?'face':cell.dataset.orientation);button.setAttribute('aria-expanded',String(active));button.setAttribute('aria-label',`${active?'Open details for':'Preview'} ${item.title}`);
}
async function previewOrOpen(id,button){
 const item=entries().find(x=>x.id===id),cell=button.closest('.cabinet-cell');if(!item||!cell)return;
 if(state.previewId===id){await pickUp(id,cell.querySelector('.object-button'));return;}
 if(state.category==='album'){state.previewId=id;const list=filtered();state.wheelIndex=list.findIndex(x=>x.id===id);renderShelfContents();return;}if(['movie','tv'].includes(state.category)){state.previewId=id;renderShelfContents();return;}
 for(const old of main.querySelectorAll('.cabinet-cell.is-preview')){const previous=entries().find(x=>x.id===old.querySelector('.object-button').dataset.id);if(previous)selectPreviewCell(old,previous,false);}
 state.previewId=id;selectPreviewCell(cell,item,true);
 const rail=cell.closest('.shelf-rail');if(!rail)return;const reveal=()=>{if(!cell.isConnected)return;const left=cell.offsetLeft;const right=left+cell.offsetWidth;if(left<rail.scrollLeft)rail.scrollTo({left:Math.max(0,left-12),behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});else if(right>rail.scrollLeft+rail.clientWidth)rail.scrollTo({left:right-rail.clientWidth+18,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});updateShelfScroll(rail);};
 reveal();cell.addEventListener('transitionend',reveal,{once:true});
}
let picking=false;
async function pickUp(id,button){if(picking)return;picking=true;const object=button?.querySelector('.artifact');let animation;try{if(object&&!matchMedia('(prefers-reduced-motion: reduce)').matches){const initial=getComputedStyle(object).transform;animation=object.animate([{transform:initial},{transform:'translateY(-14px) rotate(0deg) scale(1.035)'}],{duration:170,easing:'cubic-bezier(.2,.8,.2,1)',fill:'forwards'});await animation.finished;}openDetail(id);}catch{openDetail(id);}finally{animation?.cancel();picking=false;}}
function quickRating(x,demo){if(x.type==='memory')return '';const value=demo?0:x.rating||0;return `<section class="quick-rating"><span>Your rating</span><div class="quick-stars" role="group" aria-label="Rate this title">${[1,2,3,4,5].map(n=>`<button type="button" data-quick-rating="${n}" data-id="${e(x.id)}" class="${value>=n?'filled':''}" aria-label="Rate ${n} out of 5" aria-pressed="${value===n}">★</button>`).join('')}</div><label><span class="sr-only">Exact rating, including half stars</span><select data-exact-rating data-id="${e(x.id)}">${Array.from({length:11},(_,i)=>i/2).map(n=>`<option value="${n}" ${n===value?'selected':''}>${n?n+' / 5':'Not rated'}</option>`).join('')}</select></label></section>`;}
async function setQuickRating(id,rating){rating=Math.round(Math.max(0,Math.min(5,Number(rating)||0))*2)/2;const source=entries().find(x=>x.id===id);if(!source||source.type==='memory')return;const existing=collection.entries.find(x=>sameItem(x,source));const item=existing?{...existing}:{...source,id:uid(),notes:'',favorite:false,forLater:false,attachments:[],date:'',history:[],addedAt:new Date().toISOString()};if(!existing&&!rating)return;item.rating=rating;if(rating){item.status='done';item.date=item.date||today();item.history=[...new Set([...(item.history||[]),item.date])];}const next=existing?collection.entries.map(x=>x.id===item.id?item:x):[...collection.entries,item];if(!await persist({started:true,entries:next}))return;state.previewId=null;render();openDetail(item.id);notify(rating?'Rating saved.':'Rating cleared.');}
let detailRequest=0;
function openDetail(id){const x=entries().find(x=>x.id===id);if(!x)return;editingId=id;const demo=isDemo(),memory=x.type==='memory',request=++detailRequest;
 $('#detail-content').innerHTML=modalHead(memory?'Memory':x.title,memory?'Your photo, notes, and attached media.':`${x.creator||singular[x.type]}${x.year?' · '+x.year:''}`,'#detail-dialog')+`<div class="modal-body"><div class="detail-summary physical-detail"><div class="detail-object">${artifact(x)}</div><div><h3>${e(x.title)}</h3><p>${e(x.creator)}${x.year?` · ${e(x.year)}`:''}</p><span class="type-badge">${singular[x.type]}</span><p class="list-rating">${e(statusLabel(x))}</p></div></div>
 ${genreChips(x)}${quickRating(x,demo)}${!memory?`<section id="public-rating">${traceRatingPanel(x.publicScores,x.type)}</section>`:''}${elsewhere(x)}${!memory&&!demo?`<section class="metadata-lookup"><button class="text-button" data-find-metadata="${e(id)}">Find cover & genres ↗</button><div id="metadata-results"></div></section>`:''}<div class="entry-description" id="entry-description">${x.description?`<h4>About</h4><p>${e(x.description)}</p>`:!memory?'<p class="source-hint">No description available yet.</p>':''}</div>${x.notes?`<div class="entry-description"><h4>${demo?'From the sample diary':'Your note'}</h4><p>${e(x.notes)}</p></div>`:''}
 ${x.sourceUrl?`<p class="entry-source"><a href="${e(safeUrl(x.sourceUrl))}" target="_blank" rel="noopener noreferrer">${e(x.source||'Original source')} ↗</a></p>`:''}${attachedMedia(x)}
 ${x.metadataSourceUrl?`<p class="entry-source">Details from <a href="${e(safeUrl(x.metadataSourceUrl))}" target="_blank" rel="noopener noreferrer">${e(x.metadataSource||'Catalog')} ↗</a></p>`:''}<div class="entry-actions">${!demo?`<button class="button secondary" data-share-entry="${e(id)}">Share a review ↗</button>`:''}${demo?`<button class="button primary" data-action="keep-sample" data-id="${e(id)}">${memory?'Add a memory':'Log this title'} ${icon('plus')}</button>`:''}<details class="list-menu" ${memory?'hidden':''}><summary class="button secondary">Add to list</summary><div><button type="button" data-save-list="later" data-id="${e(id)}">${x.forLater?'✓ ':''}For later</button><button type="button" data-save-list="favorite" data-id="${e(id)}">${x.favorite?'✓ ':''}Favorites</button></div></details></div>
 ${demo?'':`<details class="edit-entry"><summary>Edit entry</summary><form id="edit-form">${fields(x,true)}</form></details>`}</div>`;
 if(!demo)prepareForm($('#edit-form'),x);if(!detailDialog.open)detailDialog.showModal();
 if(!memory){const params=new URLSearchParams({type:x.type,title:x.title,year:x.year||'',imdbId:x.imdbId||''});apiFetch('/api/ratings?'+params).then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{if(request===detailRequest&&editingId===id)updateScorePanel($('#public-rating'),data,x.type);}).catch(()=>{if(request===detailRequest&&editingId===id)$('#public-rating').insertAdjacentHTML('beforeend','<p class="source-hint">Public scores could not be refreshed. Saved scores, when available, are shown above.</p>');});}
 if(x.type==='book'&&!x.description&&/^\/works\/OL\d+W$/.test(x.externalId||'')){
  $('#entry-description').innerHTML='<p class="source-hint">Loading description…</p>';
  apiFetch('/api/book-description?id='+encodeURIComponent(x.externalId)).then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{if(request!==detailRequest||editingId!==id)return;$('#entry-description').innerHTML=data.description?`<h4>About</h4><p>${e(data.description)}</p>`:'<p class="source-hint">No description available. You can open the original source above.</p>';}).catch(()=>{if(request===detailRequest&&editingId===id)$('#entry-description').innerHTML='<p class="source-hint">Description unavailable right now. You can still log or save this title.</p>';});
 }
}
detailDialog.addEventListener('click',ev=>{const share=ev.target.closest('[data-share-entry]');if(share){const item=entries().find(x=>x.id===share.dataset.shareEntry);detailDialog.close();social.share(item);}});
let metadataMatches=[];
async function findMetadata(id,button){const item=entries().find(x=>x.id===id);if(!item)return;button.disabled=true;const host=$('#metadata-results');host.innerHTML='<p class="source-hint">Searching the catalog…</p>';try{const response=await apiFetch('/api/search?type='+encodeURIComponent(item.type)+'&q='+encodeURIComponent(item.title));const data=await response.json();if(!response.ok)throw Error(data.error||'Catalog unavailable.');if(editingId!==id||!host.isConnected)return;metadataMatches=data.items;host.innerHTML=metadataMatches.length?`<p class="source-hint">Choose the matching release. Your rating and notes are kept.</p>${metadataMatches.slice(0,8).map((x,i)=>`<button class="metadata-match" data-use-metadata="${i}" data-entry="${e(id)}">${x.image?`<img src="${e(imageUrl(x.image))}" alt="">`:''}<span><strong dir="auto">${e(x.title)}</strong><small>${e(x.year)} · ${e(x.creator||x.source)}</small><small>${e((x.genres||[]).join(' · '))}</small></span><b>Use cover & genres +</b></button>`).join('')}`:'<p class="source-hint">No matches. You can add genres in Edit entry.</p>';}catch(err){if(host.isConnected)host.innerHTML=`<p class="error-message" role="alert">${e(err.message)}</p>`;}finally{button.disabled=false;}}
async function useMetadata(id,index){const candidate=metadataMatches[index],item=collection.entries.find(x=>x.id===id);if(!candidate||!item)return;const updated={...item,genres:normalizeGenres([...(item.genres||[]),...(candidate.genres||[])]),metadataSource:candidate.source,metadataSourceUrl:safeUrl(candidate.sourceUrl)};updated.publicScores=normalizePublicScores([...normalizePublicScores(candidate.publicScores),...normalizePublicScores(item.publicScores)]);if(candidate.image)updated.image=imageUrl(candidate.image);for(const key of ['description','creator','year','imdbId'])if(!updated[key]&&candidate[key])updated[key]=candidate[key];if(!await persist({...collection,entries:collection.entries.map(x=>x.id===id?updated:x)}))return;render();openDetail(id);notify('Cover and genres updated.');}
detailDialog.addEventListener('click',ev=>{const find=ev.target.closest('[data-find-metadata]');if(find)findMetadata(find.dataset.findMetadata,find);const use=ev.target.closest('[data-use-metadata]');if(use)useMetadata(use.dataset.entry,Number(use.dataset.useMetadata));});
async function saveToList(id,list){const source=entries().find(x=>x.id===id);if(!source)return;const existing=collection.entries.find(x=>sameItem(x,source));const item=existing?{...existing}:{...source,id:uid(),notes:'',rating:0,date:'',status:'want',history:[],attachments:[],favorite:false,forLater:false,addedAt:new Date().toISOString()};if(list==='later')item.forLater=true;else item.favorite=true;
 const next=existing?collection.entries.map(x=>x.id===item.id?item:x):[...collection.entries,item];if(!await persist({started:true,entries:next}))return;state.previewId=null;render();openDetail(item.id);notify(list==='later'?'Saved to For later. Find it in the shelf filter.':'Added to Favorites.');}
detailDialog.addEventListener('submit',ev=>{if(ev.target.id==='edit-form'){ev.preventDefault();saveEntry(ev.target,true);}});
detailDialog.addEventListener('change',ev=>{if(ev.target.matches('[data-exact-rating]'))setQuickRating(ev.target.dataset.id,Number(ev.target.value));});
detailDialog.addEventListener('click',ev=>{const rateButton=ev.target.closest('[data-quick-rating]');if(rateButton){setQuickRating(rateButton.dataset.id,Number(rateButton.dataset.quickRating));return;}const listButton=ev.target.closest('[data-save-list]');if(listButton){saveToList(listButton.dataset.id,listButton.dataset.saveList);return;}const linked=ev.target.closest('[data-linked-id]');if(linked){openDetail(linked.dataset.linkedId);return;}const b=ev.target.closest('[data-action="keep-sample"]');if(b){entryDraft={...DEMO.find(x=>x.id===b.dataset.id),id:uid(),source:'Manual entry',date:today(),history:[],attachments:[]};detailDialog.close();renderEntryForm();logDialog.showModal();}});
main.addEventListener('input',ev=>{
 if(ev.target.id==='wall-picker-search'){
  wallPicker.query=ev.target.value;
  wallPicker.limit=24;
  renderWallPicker();
 }
if(ev.target.id==='shelf-search'){state.previewId=null;state.search=ev.target.value;renderShelfContents();}if(ev.target.id==='wall-title'){wallConfig.title=ev.target.value;const fullTitle=$('#wall-display-title');if(fullTitle)fullTitle.textContent=wallTitle();else socialWallPreview?.paint?.();saveWall();}});
main.addEventListener('change',ev=>{if(ev.target.id==='genre-filter'){state.genre=ev.target.value;state.previewId=null;state.movieLimit=60;renderShelfContents();}if(ev.target.id==='album-dial'){state.wheelIndex=Number(ev.target.value);state.previewId=null;renderShelfContents();}if(ev.target.id==='status-filter'){state.previewId=null;state.status=ev.target.value;renderShelfContents();}if(ev.target.id==='sort-filter'){state.previewId=null;state.sort=ev.target.value;renderShelfContents();}if(ev.target.id==='import-source'){importSequence++;state.importSource=ev.target.value;state.pending=null;state.importError='';state.importNotice='';$('#import-feedback').innerHTML='';$('#import-file').value='';}if(ev.target.id==='import-file'){readImport([...ev.target.files]);}if(ev.target.id==='wall-period'){wallConfig.period=ev.target.value;wallConfig.selected=null;wallPageIndex=0;wallPicker.limit=24;saveWall();renderWall();}if(ev.target.id==='wall-format'){wallConfig.format=ev.target.value;wallPageIndex=0;saveWall();renderWall();}if(ev.target.id==='wall-custom-color'){wallConfig.customColor=ev.target.value;wallConfig.theme='custom';saveWall();renderWall();$('#wall-custom-color')?.focus();}});
main.addEventListener('click',async ev=>{const b=ev.target.closest('[data-action]');if(!b)return;const {action,value,id}=b.dataset;
 if(action==='movie-mode'){state.movieMode=value;try{localStorage.setItem('trace.movie-view',value);}catch{}renderShelfContents();}
 if(action==='more-movies'){state.movieLimit+=60;renderShelfContents();}
 if(action==='shelf-scroll'){const rail=b.closest('.shelf-row').querySelector('.shelf-rail');rail.scrollBy({left:Number(b.dataset.direction)*rail.clientWidth*.8,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});}
 if(action==='unlink-profile'){delete connections[value];try{localStorage.setItem(preferenceKey('connections'),JSON.stringify(connections));}catch{notify('Could not save this change.');return;}importsPage();notify('Profile unlinked. Imported entries stay on your shelf.');}
 if(action==='wheel'){const list=filtered();if(list.length){state.wheelIndex=(state.wheelIndex+Number(value)+list.length)%list.length;state.previewId=null;renderShelfContents();}}
 if(action==='open-entry')openDetail(id);
 if(action==='calendar-today'){state.calendarMonth=today().slice(0,7);state.calendarDay=today();renderShelfContents();}
 if(action==='calendar-month'){state.calendarMonth=shiftMonth(state.calendarMonth,Number(value));state.calendarDay=null;renderShelfContents();}
 if(action==='calendar-day'){state.calendarDay=value;state.calendarMonth=value.slice(0,7);renderShelfContents();}
 if(action==='memory-on-day'){openLog('memory');const date=state.calendarDay||today();if(date<=today())$('#entry-form').elements.date.value=date;}
 if(action==='chapter'){state.chapterMonth=value;diaryPage();}
 if(action==='log')openLog(state.category==='book'?'book':state.category==='album'?'album':state.category==='tv'?'tv':'movie');
 if(action==='keep-memory')openLog('memory');
 if(action==='backup')backupCollection();
 if(action==='detail'){if(b.closest('.cabinet-cell'))await previewOrOpen(id,b);else await pickUp(id,b);}
 if(action==='view'){state.previewId=null;state.view=value;shelfPage();}
 if(action==='category'){state.previewId=null;state.genre='all';state.movieLimit=60;state.category=value;shelfPage();}
 if(action==='favorites'){state.previewId=null;state.favorites=!state.favorites;shelfPage();}
 if(action==='reset'){state.genre='all';state.movieLimit=60;state.previewId=null;state.category='all';state.search='';state.favorites=false;state.status='all';shelfPage();}
 if(action==='start'){if(await persist({started:true,entries:collection.entries})){wallConfig.selected=null;render();openLog();}}
 if(action==='csv-template')download('trace-import-template.csv','Title,Type,Creator,Year,Rating,Date,Status,Notes,Favorite,Image,URL\nIn Rainbows,album,Radiohead,2007,5,2026-10-23,done,"A late-night favorite",true,,\nA rainy Sunday,memory,,,0,2026-11-01,done,A small day worth keeping,false,,\n','text/csv');
 if(action==='cancel-import'){importSequence++;state.pending=null;state.importError='';state.importNotice='';$('#import-feedback').innerHTML='';$('#import-file').value='';}
 if(action==='confirm-import'&&state.pending){const p=state.pending;const m=p.preview&&p.base===collection.entries&&p.started===collection.started?p.preview:mergeEntries(collection.started?collection.entries:[],p.entries);if(await persist({started:true,entries:m.entries})){state.pending=null;state.importNotice=`Your collection is home. ${m.added} new items added, ${m.merged} entries enriched, ${m.duplicates} duplicates left unchanged.`;$('#import-feedback').innerHTML=importFeedback();$('#import-file').value='';notify('Your collection is home.');}}
 if(action==='wall-mix'){wallConfig.selected=null;wallPageIndex=0;saveWall();renderWall();}
 if(action==='wall-card'){
  wallPageIndex+=Number(value);
  renderWall();
  (main.querySelector(`[data-action="wall-card"][data-value="${value}"]:enabled`)||main.querySelector('[data-action="wall-card"]:enabled'))?.focus();
 }
 if(action==='wall-theme'&&Object.hasOwn(wallPalettes,value)){
  wallConfig.theme=value;saveWall();renderWall();
  main.querySelector(`[data-action="wall-theme"][data-value="${value}"]`)?.focus();
 }
 if(action==='wall-category'){
  wallPicker.category=value;
  wallPicker.limit=24;
  renderWallPicker();
 }
 if(action==='wall-more'){
  const firstNew=wallPicker.limit;
  wallPicker.limit+=24;
  renderWallPicker();
  $('#wall-picker-results').querySelectorAll('[data-action="wall-select"]')[firstNew]?.focus();
 }
 if(action==='wall-find'){
  if(wallPicker.category==='memory')openLog('memory');
  else{
   Object.assign(catalogState,{kind:wallPicker.category==='all'?'movie':wallPicker.category,query:'',genre:'',mode:'browse',page:0});
   location.hash='discover';
  }
 }
 if(action==='wall-select'){
  const selected=selectedWall().map(item=>item.id);
  if(selected.includes(id))wallConfig.selected=selected.filter(pick=>pick!==id);
  else if(selected.length<12)wallConfig.selected=[...selected,id];
  else{notify('Choose up to 12 keepsakes for one Tracewall.');return;}
  saveWall();
  renderWall();
  const tile=[...main.querySelectorAll('#wall-picker-results [data-action="wall-select"]')].find(button=>button.dataset.id===id);
  (tile||$('#wall-picker-search'))?.focus({preventScroll:true});
 }
 if(action==='export-wall')await exportWall(b);
});
$('#log-top').addEventListener('click',()=>openLog());
function download(name,data,type){const url=URL.createObjectURL(new Blob([data],{type}));const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}
function backupCollection(){download(`trace-backup-${today()}.json`,JSON.stringify({format:'trace-backup-v1',exportedAt:new Date().toISOString(),entries:collection.entries},null,2),'application/json');notify(collection.entries.length?'Your collection backup is ready.':'Backup saved. Sample entries aren’t included in your personal collection.');}
$('#backup').addEventListener('click',backupCollection);
function loadImage(src){return new Promise(resolve=>{const urls=artworkCandidates(src);if(!urls.length)return resolve(null);const img=new Image();img.crossOrigin='anonymous';let settled=false;const finish=result=>{if(settled)return;settled=true;clearTimeout(timer);img.onload=img.onerror=null;if(!result)img.src='';resolve(result);};const timer=setTimeout(()=>finish(null),9000);const next=()=>{if(settled)return;if(urls.length)img.src=urls.shift();else finish(null);};img.onload=()=>{if(img.naturalWidth<=2&&img.naturalHeight<=2)next();else finish(img);};img.onerror=next;next();});}
function fitCover(ctx,img,x,y,w,h){const box=artworkFit(img.naturalWidth||img.width,img.naturalHeight||img.height,w,h);if(!box)return;ctx.fillStyle='#fff4df';ctx.fillRect(x,y,w,h);ctx.drawImage(img,x+box.x,y+box.y,box.width,box.height);}
function crop(ctx,img,x,y,w,h){const ratio=Math.max(w/img.width,h/img.height),sw=w/ratio,sh=h/ratio;ctx.drawImage(img,(img.width-sw)/2,(img.height-sh)/2,sw,sh,x,y,w,h);}
function canvasText(ctx,text,x,y,maxWidth,lineHeight,maxLines=2){const words=String(text).split(/\s+/);let lines=[],line='';for(const word of words){const next=line?line+' '+word:word;if(ctx.measureText(next).width>maxWidth&&line){lines.push(line);line=word;}else line=next;}if(line)lines.push(line);lines=lines.slice(0,maxLines);lines.forEach((l,i)=>{if(ctx.measureText(l).width>maxWidth){while(ctx.measureText(l+'…').width>maxWidth&&l.length)l=l.slice(0,-1);l+='…';}ctx.fillText(l,x,y+i*lineHeight);});}
function paintWallPaper(ctx,width,height,palette){
 const wash=ctx.createLinearGradient(0,0,width,height);
 wash.addColorStop(0,palette.edge);
 wash.addColorStop(.36,palette.paper);
 wash.addColorStop(.65,palette.paper);
 wash.addColorStop(1,palette.edge);
 ctx.fillStyle=wash;
 ctx.fillRect(0,0,width,height);
 // Fixed fibers keep the paper texture steady when the title changes.
 ctx.fillStyle=palette.ink+'0b';
 for(let y=2;y<height;y+=5)ctx.fillRect(0,y,width,.7);
 for(let x=3;x<width;x+=7)ctx.fillRect(x,0,.6,height);
 ctx.fillStyle=palette.ink+'14';
 for(let i=0;i<3200;i++){
  const x=(i*149+Math.floor(i/13)*173)%width;
  const y=(i*137+Math.floor(i/7)*43)%height;
  ctx.fillRect(x,y,2+i%4,i%5===0?1.2:.6);
 }
}
function paintWallTile(ctx,item,img,slot,palette){
 const art=tracewallArtifact(item.type),box=art.image;
 ctx.save();ctx.translate(slot.x+slot.width/2,slot.y+slot.height/2);ctx.rotate(slot.tilt*Math.PI/180);ctx.scale(slot.scale,slot.scale);ctx.translate(-160,-187);
 ctx.shadowColor=palette.dark?'#00000080':'#482d1c26';ctx.shadowBlur=8;ctx.shadowOffsetY=5;
 if(art.kind==='album'){
  ctx.fillStyle='#292820';ctx.beginPath();ctx.arc(190,157,112,0,Math.PI*2);ctx.fill();ctx.shadowBlur=0;ctx.shadowOffsetY=0;
  ctx.strokeStyle=palette.dark?'#696558':'#474438';for(let r=40;r<110;r+=4){ctx.beginPath();ctx.arc(190,157,r,0,Math.PI*2);ctx.stroke();}
  ctx.fillStyle='#cda55d';ctx.beginPath();ctx.arc(190,157,28,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#eee0c2';ctx.beginPath();ctx.arc(190,157,4,0,Math.PI*2);ctx.fill();
 }else if(art.kind==='memory'){ctx.fillStyle='#fffdf5';ctx.fillRect(36,4,248,284);}
 else if(art.kind==='book'){ctx.fillStyle='#ddcca6';ctx.fillRect(72,15,188,274);ctx.fillStyle='#79573c';ctx.fillRect(64,8,192,278);}
 else{ctx.fillStyle='#4c4c3b';ctx.fillRect(60,4,200,284);ctx.fillStyle='#75634b';ctx.fillRect(60,4,6,284);}
 ctx.shadowBlur=0;ctx.shadowOffsetY=0;
 if(img){if(art.kind==='memory')crop(ctx,img,box.x,box.y,box.width,box.height);else fitCover(ctx,img,box.x,box.y,box.width,box.height);}
 else{ctx.fillStyle=colorFor(item);ctx.fillRect(box.x,box.y,box.width,box.height);ctx.fillStyle='#fff4df';ctx.textAlign='center';ctx.font='500 24px "Fraunces", Georgia';canvasText(ctx,item.title,box.x+box.width/2,box.y+box.height*.38,box.width-24,28,3);ctx.font='11px "DM Sans", Arial';ctx.fillText(item.type==='memory'?'No photo added':'Cover unavailable',box.x+box.width/2,box.y+box.height-18);}
 if(art.kind==='memory'){ctx.save();ctx.translate(161,5);ctx.rotate(-5*Math.PI/180);ctx.fillStyle='#dbc48ac4';ctx.fillRect(-31,-9,62,20);ctx.restore();}
 ctx.fillStyle=palette.ink;ctx.textAlign='center';ctx.font='28px Caveat, cursive';canvasText(ctx,item.title,160,326,310,30,2);
 ctx.fillStyle=palette.muted;ctx.font='14px "DM Sans", Arial';ctx.fillText(art.label.toUpperCase(),160,373);ctx.restore();
}
function paintSocialWall(canvas,card,images,logo,info){
 const ctx=canvas.getContext('2d');
 const {width,height,format,slots,picks,index,pages}=card;
 const story=format==='story', square=format==='square';
 const top=story?249:square?53:66;
 const rule=story?454:square?216:246;
 const footer=story?1670:height-54;
 const {palette}=info;
 const {ink,muted}=palette;
 const margin=68;
 paintWallPaper(ctx,width,height,palette);
 const frameTop=story?219:27;
 const frameBottom=story?height-218:height-27;
 ctx.strokeStyle=palette.rule+'75';
 ctx.strokeRect(28,frameTop,width-56,frameBottom-frameTop);
 ctx.strokeStyle=palette.rule+'40';
 ctx.strokeRect(35,frameTop+7,width-70,frameBottom-frameTop-14);
 ctx.fillStyle=muted;
 ctx.textAlign='left';
 ctx.font='600 20px "DM Sans", Arial';
 ctx.fillText(info.label.toUpperCase()+(info.sample?' · SAMPLE':''),margin,top+26);
 if(logo)ctx.drawImage(logo,width-margin-138,top,138,138*logo.height/logo.width);
 ctx.fillStyle=ink;
 ctx.font=`500 ${story?70:62}px "Fraunces", Georgia`;
 canvasText(ctx,wallTitle(),margin,top+(story?112:square?85:99),width-2*margin,story?70:62,2);
 ctx.strokeStyle=palette.rule+'86';
 ctx.beginPath();ctx.moveTo(margin,rule);ctx.lineTo(width-margin,rule);ctx.stroke();
 const feature=slots[0];
 if(feature){
  ctx.save();
  ctx.translate(feature.x+feature.width/2,feature.y+feature.height/2);
  ctx.rotate(-7*Math.PI/180);
  ctx.scale(feature.scale,feature.scale);
  ctx.fillStyle=palette.mat;
  ctx.beginPath();
  ctx.moveTo(-133,-150);ctx.lineTo(114,-158);ctx.lineTo(131,112);
  ctx.lineTo(35,119);ctx.lineTo(-127,109);ctx.closePath();ctx.fill();
  ctx.strokeStyle=palette.rule+'46';
  for(let y=-118;y<110;y+=22){
   ctx.beginPath();ctx.moveTo(-117,y);ctx.lineTo(109,y);ctx.stroke();
  }
  ctx.restore();
 }
 picks.forEach((item,i)=>paintWallTile(ctx,item,images[i],slots[i],palette));
 ctx.strokeStyle=palette.rule+'86';
 ctx.beginPath();ctx.moveTo(margin,footer-33);ctx.lineTo(width-margin,footer-33);ctx.stroke();
 ctx.fillStyle=muted;
 ctx.textAlign='left';
 ctx.font='26px Caveat, cursive';
 ctx.fillText('good things leave a trace.',margin,footer);
 ctx.textAlign='right';
 ctx.font='600 17px "DM Sans", Arial';
 const count=picks.length+' '+(picks.length===1?'KEEPSAKE':'KEEPSAKES');
 ctx.fillText(pages>1?`${count}   ·   ${index+1} / ${pages}`:count,width-margin,footer);
}
async function prepareSocialWall(card,palette,button){
 const canvas=$('#wall-share-canvas');
 const status=canvas.nextElementSibling;
 const preview={canvas,card,period:wallConfig.period,paint:null,missing:0};
 const info={palette,label:wallPeriod(),sample:isDemo()};
 socialWallPreview=preview;
 try{
  const [images,logo]=await Promise.all([
   Promise.all(card.picks.map(item=>loadImage(item.image))),
   loadImage('/assets/trace-wordmark.png'),
   Promise.allSettled(['28px Caveat','500 62px Fraunces','600 20px "DM Sans"'].map(font=>document.fonts.load(font))),
  ]);
  if(socialWallPreview!==preview||!canvas.isConnected)return;
  preview.missing=images.filter((image,i)=>!image&&card.picks[i].image).length;
  const paint=()=>paintSocialWall(canvas,card,images,logo,info);
  paint();
  preview.paint=paint;
  status.textContent=preview.missing?'Unavailable covers are shown as title cards.':'';
  button.disabled=false;
 }catch{
  if(canvas.isConnected)status.textContent='Could not make this card. Switch formats to try again.';
 }
}
async function exportSocialWall(button){
 const preview=socialWallPreview;
 if(!preview?.paint||!preview.canvas.isConnected)return;
 const {canvas,card,period}=preview;
 button.disabled=true;
 try{
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
  if(!blob)throw Error('Could not make this image. Please try again.');
  const suffix=card.pages>1?`-${card.index+1}`:'';
  download(`tracewall-${period}-${card.format}${suffix}.png`,blob,'image/png');
  notify(`${TRACEWALL_FORMATS[card.format]} saved. Add the PNG when you create your Instagram ${card.format==='story'?'story':'post'}.`);
 }catch(error){
  notify(`Image export failed. ${error.message}`);
 }finally{
  if(socialWallPreview===preview)button.disabled=false;
 }
}
async function exportWall(button){
 if(wallFormat()!=='full')return exportSocialWall(button);
 const list=selectedWall();if(!list.length)return;
 const config={title:wallTitle(),period:wallConfig.period,label:wallPeriod(),sample:isDemo()};
 const palette=wallPalette();
 const layout=tracewallLayout(list.length,wallColumns());button.disabled=true;button.textContent='Making your keepsake…';
 try{
  await document.fonts.ready;const [imgs,logo]=await Promise.all([Promise.all(list.map(item=>loadImage(item.image))),loadImage('/assets/trace-wordmark.png')]);
  const canvas=document.createElement('canvas');canvas.width=layout.width;canvas.height=layout.height;const ctx=canvas.getContext('2d');
  paintWallPaper(ctx,canvas.width,canvas.height,palette);
  if(logo)ctx.drawImage(logo,70,42,170,170*logo.height/logo.width);else{ctx.fillStyle=palette.ink;ctx.font='600 49px "Fraunces", Georgia';ctx.fillText('trace.',70,88);}
  ctx.fillStyle=palette.muted;ctx.textAlign='right';ctx.font=`${layout.smallText}px "DM Sans", Arial`;ctx.fillText(config.label.toUpperCase()+(config.sample?' · SAMPLE':''),1130,75);
  ctx.textAlign='center';ctx.fillStyle=palette.ink;ctx.font='500 60px "Fraunces", Georgia';canvasText(ctx,config.title,600,189,1060,64,2);
  ctx.fillStyle=palette.muted;ctx.font='32px Caveat, cursive';ctx.fillText('read, watched, played. lived.',600,296);
  list.forEach((item,i)=>paintWallTile(ctx,item,imgs[i],layout.slots[i],palette));
  ctx.strokeStyle=palette.rule;ctx.beginPath();ctx.moveTo(70,canvas.height-92);ctx.lineTo(1130,canvas.height-92);ctx.stroke();
  ctx.textAlign='left';ctx.fillStyle=palette.muted;ctx.font=`${layout.smallText}px "DM Sans", Arial`;ctx.fillText(wallFooter(list),70,canvas.height-42);
  ctx.textAlign='right';ctx.font='26px Caveat, cursive';ctx.fillText('good things leave a trace.',1130,canvas.height-42);
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(!blob)throw Error('Could not make this image. Please try again.');
  download(`tracewall-${config.period}.png`,blob,'image/png');const missing=imgs.filter((image,i)=>!image&&list[i].image).length;
  notify(missing?`Tracewall saved. ${missing} unavailable covers became title cards.`:'Your Tracewall is ready to keep or share.');
 }catch(err){notify(`Image export failed. ${err.message}`);}finally{button.disabled=!selectedWall().length;button.innerHTML=`${icon('download')} Save as image`;}
}
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(state.page==='shelf'&&state.view==='shelf')renderShelfContents();else if(state.page==='wall'&&wallFormat()==='full'&&wallColumnsRendered!==wallColumns())renderWall();},120);});
route();if(storageProblem)notify(storageProblem);
for(const [service,link] of Object.entries(connections))if(Date.now()-Date.parse(link.syncedAt||'')>6*60*60*1000)connectProfile(service,link.identifier,null,true);

function preferenceKey(name){return activeSession.user?`trace.${name}.account.${activeSession.user.id}.v1`:`trace.${name}.v1`;}
function loadAccountPreferences(){
 wallPageIndex=0;
 Object.assign(wallPicker,{category:'all',query:'',limit:24});
 connections={};wallConfig={title:'The things that stayed.',period:today().slice(0,7),theme:'paper',selected:null};
 try{const saved=JSON.parse(localStorage.getItem(preferenceKey('connections'))||'{}');for(const service of ['letterboxd','goodreads'])if(typeof saved[service]?.identifier==='string')connections[service]=saved[service];}catch{}
 try{const saved=JSON.parse(localStorage.getItem(preferenceKey('wall'))||'null');if(saved&&typeof saved.title==='string')wallConfig={...wallConfig,...saved};}catch{}
}
function redrawLibrary(){if(['shelf','diary','wall','imports'].includes(state.page))render();}
function receiveLibrary(snapshot){
 if(!snapshot.userId||snapshot.userId!==activeSession.user?.id)return;
 try{collection={started:true,entries:validateEntries(snapshot.entries)};redrawLibrary();}catch{storageProblem='This account library could not be read. Download a backup before changing it.';}
 renderLibraryStatus();
}
async function handleSession(next,context={}){
 if(context.reason==='login')try{localStorage.removeItem('trace.pending-logout.v1');}catch{}
 if(context.reason==='refresh'&&localStorage.getItem('trace.pending-logout.v1'))return;
 const previous=activeSession.user?.id||null,id=next.user?.id||null;activeSession=next;
 if(previous===id){if(id&&libraryReady&&syncStatus.state==='auth-required')await librarySync.setSession(next);renderLibraryStatus();return;}
 const epoch=++sessionEpoch;libraryReady=!id;storageProblem=id?'':guestStorageProblem;
 collection=id?{started:true,entries:[]}:guestCollection;
 state.previewId=null;state.search='';state.genre='all';state.category='all';state.pending=null;pendingGuestCopy=null;
 logDialog.close();detailDialog.close();libraryDialog.close();loadAccountPreferences();
 try{if(id)localStorage.setItem('trace.active-account.v1',JSON.stringify({id,username:next.user.username,displayName:next.user.displayName}));else localStorage.removeItem('trace.active-account.v1');}catch{}
 redrawLibrary();renderLibraryStatus();
 try{await librarySync.setSession(next);if(epoch===sessionEpoch){libraryReady=true;renderLibraryStatus();redrawLibrary();}}
 catch(error){if(epoch===sessionEpoch){libraryReady=false;notify(error.message);renderLibraryStatus();}}
}
async function libraryRequest(path,body){
 const id=activeSession.user?.id;if(!id)throw Object.assign(Error('Sign in to sync your library.'),{status:401});
 const check=await apiFetch('/api/auth/session',{credentials:'same-origin',cache:'no-store'});if(!check.ok)throw Object.assign(Error('Sign in again to sync.'),{status:check.status});
 const verified=await check.json();if(activeSession.user?.id!==id||verified.user?.id!==id||localStorage.getItem('trace.pending-logout.v1'))throw Object.assign(Error('Your account changed. Sign in again before syncing.'),{status:401});
 const response=await apiFetch(path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',...(body?{headers:{'Content-Type':'application/json','X-Trace-CSRF':verified.csrf},body:JSON.stringify(body)}:{})});
 let data;try{data=await response.json();}catch{throw Error('The library server could not be reached. Changes remain saved on this device.');}
 if(!response.ok)throw Object.assign(Error(data.error||'Library sync is temporarily unavailable.'),{status:response.status});return data;
}
async function initializeLibrarySession(){
 try{
  if(localStorage.getItem('trace.pending-logout.v1')){
   const response=await apiFetch('/api/auth/session',{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw Error('Server unavailable');const session=await response.json();
   if(session.user){const result=await apiFetch('/api/auth/logout',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Trace-CSRF':session.csrf},body:'{}'});if(!result.ok)throw Error('Sign-out is pending.');}
   localStorage.removeItem('trace.pending-logout.v1');await handleSession({user:null,csrf:''},{reason:'logout'});
  }else await social.refreshSession();
 }catch{
  if(!activeSession.user&&!localStorage.getItem('trace.pending-logout.v1')){
   try{const remembered=JSON.parse(localStorage.getItem('trace.active-account.v1')||'null');if(remembered&&/^[a-f0-9]{32}$/.test(remembered.id)&&typeof remembered.username==='string')await handleSession({user:remembered,csrf:''});}catch{}
  }
  renderLibraryStatus();
 }
}
let mobileGuidance='';
function renderLibraryStatus(){
 const host=$('#library-status');if(!host)return;const user=activeSession.user;
 const labels={loading:'Opening your private library…',saving:'Saving on this device…',syncing:'Syncing your library…',synced:'Synced with your account',offline:'Saved on this device · waiting for connection',conflict:'Changes need your review', 'auth-required':'Saved on this device · sign in to sync',error:'Saved changes need attention'};
 const status=user?(labels[syncStatus.state]||'Saved on this device'):'Saved on this device · sign in to sync';
 const pending=user&&syncStatus.pendingCount?` · ${syncStatus.pendingCount} pending`:'';
 const update=window.TraceMobile?.state?.updateAvailable||window.TraceMobile?.state?.refreshRequired;
 const installNote=mobileGuidance||(window.TraceMobile?.state?.offlineError?'Offline installation could not start. Open Trace in Safari or Chrome to try again.':'');
 host.innerHTML=`<div class="library-status-copy"><strong>${e(user?user.displayName||user.username:'Your device shelf')}</strong><span>${e(status+pending)}</span></div><div class="library-status-actions">${user?`<button type="button" data-library-action="sync" ${!libraryReady?'disabled':''}>Sync now</button>${syncStatus.conflictCount?`<button type="button" data-library-action="conflicts">Review ${syncStatus.conflictCount} changes</button>`:''}<button type="button" data-library-action="signout">Sign out</button>`:'<button type="button" data-library-action="signin">Sign in</button>'}<button type="button" data-install-trace ${window.TraceMobile?.state?.standalone?'hidden':''}>Install app</button><button type="button" data-update-trace ${update?'':'hidden'}>Update app</button></div>${syncStatus.state==='error'&&syncStatus.error?`<p class="library-status-error" role="alert">${e(syncStatus.error)}</p>`:''}<p data-install-trace-status class="install-guidance" role="status">${e(installNote)}</p>`;
 $('#log-top').disabled=!!user&&!libraryReady;
 const badge=document.querySelector('.local-badge');if(badge)badge.textContent=user?(syncStatus.state==='synced'?'Synced':'Saved on device'):'Saved on this device';
 const profile=document.querySelector('.sidebar .profile');if(profile){profile.querySelector('.avatar').textContent=Array.from(user?.displayName||user?.username||'Y')[0].toUpperCase();profile.querySelector('strong').textContent=user?user.displayName||user.username:'Your collection';profile.querySelector('strong').nextElementSibling.textContent=user?'Private account library':'Saved on this browser';}
}
function libraryAccountPanel(){
 return `<section class="library-account-panel"><h2>${activeSession.user?'Your private account library':'Keep your shelf with you'}</h2><p>${activeSession.user?'Your ratings, diary, and memories sync privately. Only reviews you choose to post appear in Circle.':'Sign in to keep a private library across devices. Your current device shelf remains here until you choose to copy it.'}</p>${activeSession.user?`${guestCollection.entries.length?`<p>Your separate device shelf contains ${guestCollection.entries.length} items.</p><button class="button secondary" data-library-action="copy-preview" ${!libraryReady?'disabled':''}>Preview copying device shelf</button>`:''}<button class="text-button" data-library-action="sync">Sync now</button>`:'<button class="button primary" data-library-action="signin">Sign in to Trace</button>'}<p class="source-hint">Other devices need the same hosted Trace server. Localhost is this computer only.</p></section>`;
}
function showLibraryDialog(content){libraryDialog.innerHTML=`<div class="modal-head"><h2>Private library</h2><button class="close-button" aria-label="Close library dialog" data-library-action="close">×</button></div><div class="modal-body">${content}</div>`;if(!libraryDialog.open)libraryDialog.showModal();}
function previewGuestCopy(){
 if(!activeSession.user||!libraryReady)return;const merged=mergeEntries(collection.entries,guestCollection.entries);pendingGuestCopy={userId:activeSession.user.id};
 showLibraryDialog(`<h3>Copy this device shelf?</h3><p>${guestCollection.entries.length} device items · ${merged.added} new · ${merged.merged} enriched · ${merged.duplicates} unchanged.</p><p>Your existing account notes and ratings take priority. The original device shelf remains available when signed out. This copy stays private.</p><div class="library-dialog-actions"><button class="button primary" data-library-action="copy-confirm">Copy to my private library</button><button class="button secondary" data-library-action="close">Cancel</button></div>`);
}
function renderLibraryConflicts(){
 const conflicts=librarySync.getState().conflicts;if(!conflicts.length){libraryDialog.close();return;}
 const describe=entry=>entry?`<strong>${e(entry.title)}</strong><span>${e(entry.date||'No date')} · ${entry.rating?e(entry.rating)+' / 5':'Not rated'}</span><p>${e(entry.notes||'No note')}</p>`:'<p>This entry was removed.</p>';
 showLibraryDialog(`<h3>Choose which changes to keep</h3><p>Both versions stay saved until you choose.</p>${conflicts.map(row=>`<section class="library-conflict"><h4>${e(row.localEntry?.title||row.remoteEntry?.title||'Removed entry')}</h4><div class="library-conflict-versions"><div><h5>This device</h5>${describe(row.localEntry)}</div><div><h5>Account version</h5>${describe(row.remoteEntry)}</div></div><div class="library-dialog-actions"><button class="button secondary" data-library-action="resolve" data-id="${e(row.id)}" data-choice="local">Use this device</button><button class="button secondary" data-library-action="resolve" data-id="${e(row.id)}" data-choice="remote">Use account version</button>${row.localEntry&&row.remoteEntry?`<button class="text-button" data-library-action="resolve" data-id="${e(row.id)}" data-choice="both">Keep both</button>`:''}</div></section>`).join('')}`);
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-library-action]');if(!button)return;const action=button.dataset.libraryAction;button.disabled=true;
 try{
  if(action==='signin')social.showAuth();
  if(action==='sync'){await initializeLibrarySession();await librarySync.syncNow();}
  if(action==='signout'){try{await social.signOut();}catch{localStorage.setItem('trace.pending-logout.v1','true');await handleSession({user:null,csrf:''},{reason:'logout'});notify('Signed out on this device. Server sign-out will finish when connected.');}}
  if(action==='copy-preview')previewGuestCopy();
  if(action==='copy-confirm'&&pendingGuestCopy?.userId===activeSession.user?.id){const merged=mergeEntries(collection.entries,guestCollection.entries);if(await persist({started:true,entries:merged.entries})){libraryDialog.close();pendingGuestCopy=null;redrawLibrary();notify('Copied to your private account library.');}}
  if(action==='conflicts')renderLibraryConflicts();
  if(action==='resolve'){await librarySync.resolveConflict(button.dataset.id,button.dataset.choice);renderLibraryConflicts();}
  if(action==='close')libraryDialog.close();
 }catch(error){notify(error.message);}finally{if(button.isConnected)button.disabled=false;}
});
libraryDialog.addEventListener('click',event=>{if(event.target===libraryDialog)libraryDialog.close();});
window.addEventListener('trace:mobile-state',renderLibraryStatus);
window.addEventListener('trace:mobile-update',renderLibraryStatus);
window.addEventListener('trace:mobile-error',event=>{mobileGuidance=event.detail.message;renderLibraryStatus();});
window.addEventListener('trace:mobile-guidance',event=>{mobileGuidance=event.detail.message;renderLibraryStatus();});
document.addEventListener('click',event=>{if(event.target.closest('[data-update-trace]')&&document.querySelector('dialog[open]')){event.preventDefault();event.stopImmediatePropagation();notify('Finish or close the open form before updating Trace.');}},true);

renderLibraryStatus();initializeLibrarySession();
window.addEventListener('online',()=>initializeLibrarySession());
window.addEventListener('offline',()=>renderLibraryStatus());

window.addEventListener('storage',async event=>{
 if(!['trace.active-account.v1','trace.pending-logout.v1'].includes(event.key))return;
 try{if(!localStorage.getItem('trace.active-account.v1')||localStorage.getItem('trace.pending-logout.v1'))await handleSession({user:null,csrf:''},{reason:'logout'});await initializeLibrarySession();}catch{renderLibraryStatus();}
});
