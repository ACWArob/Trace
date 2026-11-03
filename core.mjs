export const TYPES = { movie: 'Movies', tv: 'TV shows', anime: 'Anime', book: 'Books', album: 'Albums', memory: 'Memories' };
export const singular = { movie: 'Movie', tv: 'TV show', anime: 'Anime', book: 'Book', album: 'Album', memory: 'Memory' };
export const uid = () => globalThis.crypto?.randomUUID?.() || `trace-${Date.now()}-${Math.random().toString(36).slice(2)}`;
export const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
export const escapeHTML = (s = '') => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function safeUrl(value) { try { const u = new URL(value, 'http://localhost'); return ['https:', 'http:'].includes(u.protocol) ? value : ''; } catch { return ''; } }

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
      notes:textReview(get('Review','My Review','Notes')),favorite:/^(true|yes|1)$/i.test(get('Favorite','Liked')),
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
    for(const key of ['creator','year','date','rating','notes','description','image','sourceUrl','externalId']) if(!old[key] && item[key]) {old[key]=item[key]; changed=true;}
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
