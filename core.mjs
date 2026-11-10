export const TYPES = { movie: 'Movies', tv: 'TV shows', anime: 'Anime', book: 'Books', album: 'Albums', memory: 'Memories' };
export const singular = { movie: 'Movie', tv: 'TV show', anime: 'Anime', book: 'Book', album: 'Album', memory: 'Memory' };
export const uid = () => globalThis.crypto?.randomUUID?.() || `trace-${Date.now()}-${Math.random().toString(36).slice(2)}`;
export const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
export const escapeHTML = (s = '') => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function safeUrl(value) { try { const u = new URL(value, 'http://localhost'); return ['https:', 'http:'].includes(u.protocol) ? value : ''; } catch { return ''; } }

export function normalizeGenres(value) {
  const values=Array.isArray(value)?value:typeof value==='string'?value.split(/[,;|]/):[];
  const seen=new Set();return values.filter(x=>typeof x==='string').map(x=>x.trim().replace(/\s+/g,' ').slice(0,40)).filter(x=>{const key=x.toLocaleLowerCase();if(!x||seen.has(key))return false;seen.add(key);return true;}).slice(0,8);
}

export function normalizeAttachments(items) {
  if(!Array.isArray(items)) return [];
  const seen=new Set();
  return items.flatMap(item=>{
    if(!item || !Object.hasOwn(TYPES,item.type) || item.type==='memory' || typeof item.title!=='string' || !item.title.trim()) return [];
    const id=typeof item.id==='string'&&/^[a-zA-Z0-9_-]{1,100}$/.test(item.id)?item.id:'';
    const key=id||`${item.type}:${item.title}:${item.year||''}`;
    if(seen.has(key))return [];seen.add(key);
    return [{id,type:item.type,title:item.title.slice(0,300),creator:String(item.creator||'').slice(0,300),year:String(item.year||'').slice(0,4),image:safeImageUrl(item.image)}];
  }).slice(0,12);
}
export function safeImageUrl(v) {
  return typeof v==='string' && (/^\/assets\/[\w.-]+$/.test(v)||/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(v))?v:safeUrl(v||'');
}
export function migrateEntry(entry) {
  const legacy=['restaurant','place'].includes(entry?.type);
  if(!entry || (!legacy && entry.type!=='memory'))return entry;
  return {...entry,type:'memory',rating:0,attachments:normalizeAttachments(entry.attachments),
    ...(legacy?{legacyType:entry.type,legacyRating:Number(entry.rating)||0}:{} )};
}

export function dateOnly(value) {
  const s = String(value || '').trim();
  const m = s.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:$|T|\s)/);
  if (!m) return '';
  const out = `${m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}`;
  const d = new Date(`${out}T12:00:00Z`);
  return !Number.isNaN(d.valueOf()) && d.toISOString().slice(0,10) === out ? out : '';
}
export function parseCSV(text) {
  const rows = []; let row = [], cell = '', quoted = false;
  text = String(text).replace(/^\uFEFF/, '');
  for (let i=0; i<text.length; i++) {
    const c = text[i];
    if (c === '"') { if (quoted && text[i+1] === '"') { cell += '"'; i++; } else quoted = !quoted; }
    else if (c === ',' && !quoted) { row.push(cell); cell = ''; }
    else if ((c === '\n' || c === '\r') && !quoted) {
      if (c === '\r' && text[i+1] === '\n') i++;
      row.push(cell); if (row.some(x=>x.trim())) rows.push(row); row = []; cell = '';
    } else cell += c;
  }
  if (quoted) throw new Error('A quoted field is unfinished. Please upload the original CSV export.');
  row.push(cell); if (row.some(x=>x.trim())) rows.push(row);
  if (rows.length < 2) throw new Error('This CSV has no entries. Choose a file with a header and at least one item.');
  const headers = rows.shift().map(x=>x.trim());
  if (new Set(headers.map(x=>x.toLowerCase())).size !== headers.length) throw new Error('This CSV has duplicate column names.');
  return rows.map((r,index)=>{ if(r.length !== headers.length) throw new Error(`Row ${index+2} has ${r.length} fields; expected ${headers.length}. Check the CSV formatting.`); return Object.fromEntries(headers.map((h,i)=>[h,r[i].trim()])); });
}
function textReview(s) { return String(s || '').replace(/<br\s*\/?\s*>/gi,'\n').replace(/<[^>]*>/g,'').replace(/&quot;/g,'"').replace(/&amp;/g,'&'); }
function numericRating(v, max=5) { const n = Number(v); return Number.isFinite(n) ? Math.round(Math.max(0,Math.min(5,n*5/max))*2)/2 : 0; }
export function normalizeImport(text, fileName='', source='auto') {
  const rows = parseCSV(text); const columns = Object.keys(rows[0]);
  const has = h => columns.some(x=>x.toLowerCase()===h.toLowerCase());
  if(source === 'auto') source = has('Book Id') || has('Exclusive Shelf') ? 'goodreads' : has('Letterboxd URI') || has('Name') ? 'letterboxd' : has('Const') && has('Title') ? 'imdb' : 'generic';
  if(source==='letterboxd' && !has('Name')) throw new Error('Choose a Letterboxd movie CSV: diary, ratings, watched, watchlist, reviews, or a list.');
  if(!has('Name') && !has('Title')) throw new Error('No Title or Name column found. Use a supported export or the Trace CSV template.');
  let skipped=0;
  const entries = rows.flatMap(row=>{
    const get = (...keys) => { for(const key of keys){ const k=Object.keys(row).find(x=>x.toLowerCase()===key.toLowerCase()); if(k && row[k]) return row[k]; } return ''; };
    const title = get('Name','Title'); if(!title) { skipped++; return []; }
    let type='movie', status='done', date='', rating=0;
    if(source==='goodreads') {
      type='book'; const shelf=get('Exclusive Shelf'); status=shelf==='to-read'?'want':shelf==='currently-reading'?'progress':'done';
      date=dateOnly(get('Date Read')); rating=numericRating(get('My Rating'));
    } else if(source==='letterboxd') {
      status=/watchlist/i.test(fileName)?'want':'done';
      // Date in watched.csv / ratings.csv is an activity date, not a watch date.
      date=dateOnly(get('Watched Date') || (/diary/i.test(fileName)?get('Date'):'')); rating=numericRating(get('Rating'));
    } else if(source==='imdb') {
      type=/tv|series/i.test(get('Title Type'))?'tv':'movie'; rating=numericRating(get('Your Rating'),10);
    } else {
      const candidate=get('Type').toLowerCase(); type=Object.hasOwn(TYPES,candidate)||['restaurant','place'].includes(candidate)?candidate:'book';
      status=['want','progress','done'].includes(get('Status'))?get('Status'):'done';
      date=dateOnly(get('Date')); rating=numericRating(get('Rating'));
    }
    const isbn=get('ISBN13','ISBN').replace(/[="\s]/g,'');
    return [migrateEntry({id:uid(),title,type,status,date,rating,creator:get('Author','Creator','Directors'),year:get('Year','Original Publication Year','Year Published'),
      genres:normalizeGenres(get('Genres','Genre')),notes:textReview(get('Review','My Review','Notes')),favorite:/^(true|yes|1)$/i.test(get('Favorite','Liked')),
      image:safeUrl(get('Image')) || (type==='book' && /^(\d{10}|\d{13})$/.test(isbn)?`https://covers.openlibrary.org/b/isbn/${isbn}-L.jpg`:''),
      source:source==='generic'?'CSV':source,sourceUrl:safeUrl(get('Letterboxd URI','URL','Source URL')),externalId:get('Book Id','Const'),
      addedAt:new Date().toISOString(),history:date?[date]:[]})];
  });
  return {entries, source, skipped};
}
const norm = s => String(s||'').normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu,'');
export function sameItem(a,b) {
  if(a.type!==b.type) return false;
  if(a.type==='memory') return !!(a.id&&a.id===b.id) || !!(a.date && a.date===b.date && norm(a.title)===norm(b.title) && (a.notes||'')===(b.notes||'') && (a.image||'')===(b.image||''));
  if(a.source && a.source===b.source && a.externalId && a.externalId===b.externalId) return true;
  if(norm(a.title)!==norm(b.title)) return false;
  if(a.year && b.year && String(a.year)!==String(b.year)) return false;
  if(['book','album'].includes(a.type) && a.creator && b.creator && norm(a.creator)!==norm(b.creator)) return false;
  return true;
}
export function mergeEntries(existing,incoming) {
  const entries=existing.map(x=>({...x,history:[...(x.history||[])]})); let added=0, merged=0;
  // Restrict identity matching to this title or a provider ID, rather than the whole library.
  const titles=new Map(), external=new Map(), memoryIds=new Map();
  const titleKey=x=>`${x.type}:${norm(x.title)}`;
  const externalKey=x=>x.type!=='memory'&&x.source&&x.externalId?`${x.type}:${x.source}:${x.externalId}`:'';
  const index=x=>{if(x.type==='memory'&&x.id&&!memoryIds.has(x.id))memoryIds.set(x.id,x);const key=titleKey(x);if(!titles.has(key))titles.set(key,[]);titles.get(key).push(x);const ext=externalKey(x);if(ext&&!external.has(ext))external.set(ext,x);};
  entries.forEach(index);
  for(const item of incoming) {
    const direct=item.type==='memory'&&item.id?memoryIds.get(item.id):external.get(externalKey(item));
    const old=direct||(titles.get(titleKey(item))||[]).find(x=>sameItem(x,item));
    if(!old) { const addedItem={...item,history:[...(item.history||[])]};entries.push(addedItem);index(addedItem);added++;continue; }
    let changed=false;
    for(const key of ['creator','year','date','rating','notes','description','image','sourceUrl','externalId','metadataSource','metadataSourceUrl','imdbId']) if(!old[key] && item[key]) {old[key]=item[key]; changed=true;}
    const genres=normalizeGenres([...normalizeGenres(old.genres),...normalizeGenres(item.genres)]);if(JSON.stringify(genres)!==JSON.stringify(old.genres||[])){old.genres=genres;changed=true;}
    if(!old.publicScores?.length&&item.publicScores?.length){old.publicScores=normalizePublicScores(item.publicScores);changed=true;}
    if(item.forLater&&!old.forLater){old.forLater=true;changed=true;}
    if(item.favorite && !old.favorite) {old.favorite=true;changed=true;}
    const ranks={want:0,progress:1,done:2}; if(ranks[item.status]>ranks[old.status]) {old.status=item.status;changed=true;}
    if(old.type==='memory'){const combined=normalizeAttachments([...(old.attachments||[]),...(item.attachments||[])]);if(JSON.stringify(combined)!==JSON.stringify(old.attachments||[])){old.attachments=combined;changed=true;}}
    const history=[...new Set([...(old.history||[]),...(item.history||[])])].sort();
    if(history.length!==(old.history||[]).length) changed=true;
    old.history=history;const ext=externalKey(old);if(ext&&!external.has(ext))external.set(ext,old);if(changed) merged++;
  }
  return {entries,added,merged,duplicates:incoming.length-added-merged};
}
export function recapEntries(entries,period) {
  return entries.filter(x=>x.status==='done' && (period==='all' || (x.date && x.date.startsWith(period))));
}

// Keep imports from one medium from crowding every other part of a recap out.
// Explicit picks are an ordered list, including an intentionally empty one.
export function selectTracewall(candidates, selected=null, limit=9) {
  const unique=new Map();
  for(const item of candidates)if(item.id&&!unique.has(item.id))unique.set(item.id,item);
  if(Array.isArray(selected))return [...new Set(selected)].map(id=>unique.get(id)).filter(Boolean).slice(0,12);
  const groups=['book','movie','album','memory','tv'].map(kind=>[...unique.values()].filter(item=>(item.type==='anime'?'tv':item.type)===kind));
  const count=Math.max(0,Math.min(12,Math.floor(Number(limit)||0))), result=[];
  for(let round=0;result.length<count;round++){
    let added=false;
    for(const group of groups)if(group[round]&&result.length<count){result.push(group[round]);added=true;}
    if(!added)break;
  }
  return result;
}

export const TRACEWALL_THEMES=Object.freeze({paper:'#fff4df',sage:'#e5e9ce',rose:'#f1dfd1'});

// Preview and PNG share these coordinates; artifact coordinates use a 320×374 tile.
export function tracewallLayout(count, columns=3) {
  count=Math.max(0,Math.min(12,Math.floor(Number(count)||0)));
  columns=columns===2?2:3;
  const width=1200, gap=48, tileWidth=(1060-(columns-1)*gap)/columns, scale=tileWidth/320;
  const rows=Math.ceil(count/columns), height=Math.ceil(330+Math.max(1,rows)*400*scale+130);
  const slots=Array.from({length:count},(_,i)=>{
    const row=Math.floor(i/columns), rowCount=Math.min(columns,count-row*columns);
    const left=(width-rowCount*tileWidth-(rowCount-1)*gap)/2;
    return {x:left+(i%columns)*(tileWidth+gap),y:330+row*400*scale,width:tileWidth,height:374*scale,scale,tilt:[-2,2,-1,1,-2,2][i%6]};
  });
  return {width,height,columns,smallText:columns===2?24:16,slots};
}

export function tracewallArtifact(type) {
  if(type==='book')return {kind:'book',label:'Book',image:{x:66,y:10,width:188,height:274}};
  if(type==='album')return {kind:'album',label:'Album',image:{x:25,y:38,width:238,height:238}};
  if(type==='memory')return {kind:'memory',label:'Memory',image:{x:46,y:14,width:228,height:228}};
  return {kind:'dvd',label:type==='movie'?'Film':'TV',image:{x:68,y:14,width:184,height:264}};
}

const socialWallLayouts = {
  story: {
    height: 1920, top: 480, bottom: 1630,
    slots: [[48, 0, 565, -4], [647, 48, 350, 4], [634, 481, 354, -4], [53, 704, 320, 4], [347, 746, 294, -4]],
  },
  portrait: {
    height: 1350, top: 262, bottom: 1240,
    slots: [[59, 20, 503, -4], [614, 7, 359, 4], [625, 449, 350, -4], [199, 622, 284, 5]],
  },
  square: {
    height: 1080, top: 228, bottom: 984,
    slots: [[58, 33, 500, -4], [627, 4, 294, 5], [631, 359, 283, -4]],
  },
};

export const TRACEWALL_FORMATS = Object.freeze({
  story: 'Story', portrait: 'Portrait post', square: 'Square post', full: 'Full keepsake',
});

export function tracewallShareCard(items, format, page = 0) {
  if (!Object.hasOwn(socialWallLayouts, format)) return null;
  const template = socialWallLayouts[format];
  const pageSize = template.slots.length;
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const index = Math.max(0, Math.min(pages - 1, Math.floor(Number(page) || 0)));
  const picks = items.slice(index * pageSize, (index + 1) * pageSize);
  const placements = picks.length === 1
    ? [[format === 'story' ? 210 : 250, 0, format === 'story' ? 660 : 580, -4]]
    : template.slots.slice(0, picks.length);
  const slots = placements.map(([x, y, width, tilt]) => ({
    x, y, width, height: width * 374 / 320, scale: width / 320, tilt,
  }));
  if (slots.length) {
    // Center the occupied spread, including the corners of the tilted cards.
    const extents = slots.map(slot => {
      const radians = Math.abs(slot.tilt) * Math.PI / 180;
      const halfHeight = (slot.height * Math.cos(radians) + slot.width * Math.sin(radians)) / 2;
      const center = slot.y + slot.height / 2;
      return [center - halfHeight, center + halfHeight];
    });
    const first = Math.min(...extents.map(([min]) => min));
    const last = Math.max(...extents.map(([, max]) => max));
    const offset = (template.top + template.bottom - first - last) / 2;
    for (const slot of slots) slot.y += offset;
  }
  return {format, width: 1080, height: template.height, picks, index, pages, slots};
}

export function reviewLinks(item) {
 const query=encodeURIComponent([item.title,item.type==='book'||item.type==='album'?item.creator:item.year].filter(Boolean).join(' '));
 if(['movie','tv','anime'].includes(item.type)){
  const imdb=/^tt\d{5,12}$/.test(item.imdbId||'')?item.imdbId:item.source?.toLowerCase()==='imdb'&&/^tt\d{5,12}$/.test(item.externalId||'')?item.externalId:'';
  const links=[{label:imdb?'IMDb reviews':'Find on IMDb',url:imdb?`https://www.imdb.com/title/${imdb}/reviews/`:`https://www.imdb.com/find/?q=${query}&s=tt`}];
  if(item.type==='movie'){
   const match=String(item.sourceUrl||'').match(/^https:\/\/(?:www\.)?letterboxd\.com\/(?:[A-Za-z0-9_-]+\/)?film\/([a-z0-9-]+)\//i);
   links.push({label:match?'Letterboxd reviews':'Find on Letterboxd',url:match?`https://letterboxd.com/film/${match[1]}/reviews/`:`https://letterboxd.com/search/films/${encodeURIComponent(item.title)}/`});
  }
  links.push({label:'Find on Rotten Tomatoes',url:`https://www.rottentomatoes.com/search?search=${query}`});return links;
 }
 if(item.type==='book'){
  const known=item.source?.toLowerCase()==='goodreads'&&/^\d+$/.test(item.externalId||'');
  return [{label:known?'Goodreads reviews':'Find on Goodreads',url:known?`https://www.goodreads.com/book/show/${item.externalId}`:`https://www.goodreads.com/search?q=${query}`},{label:'Open Library',url:/^\/works\/OL\d+W$/.test(item.externalId||'')?`https://openlibrary.org${item.externalId}`:`https://openlibrary.org/search?q=${query}`}];
 }
 if(item.type==='album')return [{label:'Find on MusicBrainz',url:`https://musicbrainz.org/search?query=${query}&type=release_group&method=indexed`},{label:'Find on Last.fm',url:`https://www.last.fm/search/albums?q=${query}`}];
 return [];
}

export const FILM_SCORE_SOURCES=['IMDb','Rotten Tomatoes critics','Rotten Tomatoes audience','Letterboxd'];
function scoreSourceName(source){
 const aliases={'imdb':'IMDb','internet movie database':'IMDb','rotten tomatoes':'Rotten Tomatoes critics','tomatoes':'Rotten Tomatoes critics','tomatoes-critics':'Rotten Tomatoes critics','rotten tomatoes critics':'Rotten Tomatoes critics','popcorn':'Rotten Tomatoes audience','tomatoesaudience':'Rotten Tomatoes audience','tomatoes-audience':'Rotten Tomatoes audience','rotten tomatoes audience':'Rotten Tomatoes audience','letterboxd':'Letterboxd'};
 return Object.hasOwn(aliases,source.toLowerCase())?aliases[source.toLowerCase()]:source;
}
export function normalizePublicScores(input) {
 if(!Array.isArray(input))return [];
 const seen=new Set();return input.flatMap(row=>{if(!row||typeof row.source!=='string'||row.value===null||row.value===undefined||row.value===''||row.max===null||row.max===undefined||row.max==='')return [];const source=scoreSourceName(row.source.trim().slice(0,60));const key=source.toLowerCase();const value=Number(row.value),max=Number(row.max);const count=row.count===undefined||row.count===null||row.count===''?null:Number(row.count);if(!source||seen.has(key)||!Number.isFinite(value)||!Number.isFinite(max)||max<=0||max>100||value<0||value>max||count!==null&&(!Number.isFinite(count)||count<=0))return [];seen.add(key);return [{source,value,max,count:count===null?null:Math.floor(count),url:safeUrl(row.url||''),via:String(row.via||'').slice(0,60)}];}).slice(0,8);
}
export function filmConsensus(input){
 const all=normalizePublicScores(input),sources=FILM_SCORE_SOURCES.flatMap(name=>all.filter(row=>row.source===name));
 const missing=FILM_SCORE_SOURCES.filter(name=>!sources.some(row=>row.source===name));
 const value=sources.length<2?null:Math.round(sources.reduce((total,row)=>total+row.value/row.max*10,0)/sources.length*10)/10;
 return {value,count:sources.length,sources,missing,other:all.filter(row=>!FILM_SCORE_SOURCES.includes(row.source))};
}
export function blendRatings(input) {
 const sources=normalizePublicScores(input).map(row=>({...row,five:row.value/row.max*5,weight:row.count===null?.5:Math.max(.2,Math.min(1,Math.log10(row.count+1)/4))}));
 if(!sources.length)return {value:null,sources:[],count:0};
 const total=sources.reduce((n,x)=>n+x.five*x.weight,0),weight=sources.reduce((n,x)=>n+x.weight,0);
 return {value:Math.round(total/weight*10)/10,sources,count:sources.length};
}
