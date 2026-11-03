import test from 'node:test';
import assert from 'node:assert/strict';
import {parseCSV,normalizeImport,mergeEntries,recapEntries,dateOnly,safeUrl} from '../core.mjs';

test('CSV preserves quoted commas, newlines, escaped quotes, CRLF and BOM',()=>{
 const rows=parseCSV('\uFEFFTitle,Notes\r\n"A, B","Line one\nShe said ""hello"""\r\n');
 assert.deepEqual(rows,[{Title:'A, B',Notes:'Line one\nShe said "hello"'}]);
});
test('Malformed CSV is rejected rather than silently shifting fields',()=>{
 assert.throws(()=>parseCSV('Title,Notes\na,b,c'),/expected 2/);
 assert.throws(()=>parseCSV('Title,Notes\na,"unfinished'),/unfinished/);
 assert.throws(()=>parseCSV('Title,Title\na,b'),/duplicate/);
});
test('Letterboxd ratings and diary merge without replacing a rating or inventing viewing dates',()=>{
 const ratings=normalizeImport('Date,Name,Year,Letterboxd URI,Rating\n2026-10-10,Perfect Days,2023,https://boxd.it/example,4.5','ratings.csv');
 assert.equal(ratings.entries[0].date,'');
 const diary=normalizeImport('Date,Name,Year,Watched Date\n2026-10-12,Perfect Days,2023,2026-09-04','diary.csv');
 const m=mergeEntries(ratings.entries,diary.entries);
 assert.equal(m.entries.length,1);assert.equal(m.entries[0].rating,4.5);assert.equal(m.entries[0].date,'2026-09-04');assert.equal(m.merged,1);
 const repeat=mergeEntries(m.entries,diary.entries);assert.equal(repeat.duplicates,1);assert.equal(repeat.merged,0);
});
test('Letterboxd watchlist upgrades to watched and repeated diary dates are preserved',()=>{
 const want=normalizeImport('Name,Year\nPerfect Days,2023','watchlist.csv').entries;
 const logs=normalizeImport('Name,Year,Watched Date\nPerfect Days,2023,2026-10-01\nPerfect Days,2023,2026-10-04','diary.csv').entries;
 const m=mergeEntries(want,logs);assert.equal(m.entries.length,1);assert.equal(m.entries[0].status,'done');assert.deepEqual(m.entries[0].history,['2026-10-01','2026-10-04']);
});
test('Goodreads imports shelves, ISBN wrappers, blank dates, zero ratings and plain text reviews',()=>{
 const csv='Book Id,Title,Author,ISBN13,My Rating,Date Read,Exclusive Shelf,My Review,Original Publication Year\n12,Just Kids,Patti Smith,"=""9780060936228""",0,,to-read,"<b>hello</b><br>world",2010';
 const x=normalizeImport(csv,'goodreads_library_export.csv').entries[0];
 assert.equal(x.type,'book');assert.equal(x.status,'want');assert.equal(x.date,'');assert.equal(x.rating,0);assert.equal(x.year,'2010');assert.equal(x.notes,'hello\nworld');assert.match(x.image,/9780060936228/);
});
test('IMDb ten-star ratings convert to five; generic albums keep their type',()=>{
 const x=normalizeImport('Const,Your Rating,Title,Title Type,Year\ntt123,9,The Bear,tvSeries,2022','ratings.csv').entries[0];
 assert.equal(x.type,'tv');assert.equal(x.rating,4.5);
 const a=normalizeImport('Title,Type,Creator,Rating,Status\nIn Rainbows,album,Radiohead,5,done','albums.csv').entries[0];assert.equal(a.type,'album');
});
test('Known remakes and different book authors are not merged',()=>{
 const original={id:'a',type:'movie',title:'Dune',year:'1984',rating:4,status:'done'};
 const remake={...original,id:'b',year:'2021'};
 assert.equal(mergeEntries([original],[remake]).entries.length,2);
 const book={id:'c',type:'book',title:'Home',creator:'Author A',status:'done'};
 assert.equal(mergeEntries([book],[{...book,id:'d',creator:'Author B'}]).entries.length,2);
});
test('Merge preserves personal reviews, supports missing metadata and does not mutate inputs',()=>{
 const a={id:'a',type:'book',title:'Just Kids',status:'done',rating:5,notes:'My note',history:[]};
 const b={id:'b',type:'book',title:'Just Kids',status:'done',rating:2,notes:'Import note',creator:'Patti Smith',favorite:true,history:['2026-10-02']};
 const m=mergeEntries([a],[b]);assert.equal(m.entries[0].notes,'My note');assert.equal(m.entries[0].rating,5);assert.equal(m.entries[0].creator,'Patti Smith');assert.equal(m.entries[0].favorite,true);assert.deepEqual(a.history,[]);
});
test('Recaps use completed entries only and do not put undated imports into a month',()=>{
 const rows=[{status:'done',date:'2026-10-10'},{status:'done',date:''},{status:'want',date:'2026-10-11'},{status:'done',date:'2025-10-01'}];
 assert.equal(recapEntries(rows,'2026-10').length,1);assert.equal(recapEntries(rows,'2026').length,1);assert.equal(recapEntries(rows,'all').length,3);
});
test('Dates and URLs reject invalid calendar days and executable schemes',()=>{
 assert.equal(dateOnly('2026/02/31'),'');assert.equal(dateOnly('2026/2/3'),'2026-02-03');assert.equal(safeUrl('javascript:alert(1)'),'');assert.equal(safeUrl('https://example.org/a'),'https://example.org/a');
});

test('Large imports merge by indexed identity and retain updates',()=>{
 const incoming=Array.from({length:10000},(_,i)=>({id:String(i),type:'movie',title:`Film ${i}`,year:'2026',status:'done',rating:4,history:[]}));
 const imported=mergeEntries([],incoming);
 assert.equal(imported.added,10000);
 const again=mergeEntries(imported.entries,incoming.map(x=>({...x,creator:'Director'})));
 assert.equal(again.added,0);assert.equal(again.merged,10000);assert.equal(again.entries.length,10000);
});

test('Legacy food and place records become memories without losing their history', async()=>{
 const {migrateEntry}=await import('../core.mjs');
 const old={id:'old-place',type:'place',title:'A good afternoon',creator:'Kyoto',date:'2026-10-16',rating:4.5,notes:'Took the long way home.',image:'/assets/kyoto.jpg',history:['2026-10-16'],status:'done'};
 const next=migrateEntry(old);
 assert.equal(next.type,'memory');assert.equal(next.rating,0);assert.equal(next.legacyRating,4.5);assert.equal(next.legacyType,'place');
 for(const key of ['id','title','creator','date','notes','image'])assert.equal(next[key],old[key]);
 assert.deepEqual(next.history,old.history);assert.deepEqual(migrateEntry(next),next);assert.equal(old.type,'place');
});
test('Memories with the same title on different days remain separate; backup IDs match', async()=>{
 const {sameItem}=await import('../core.mjs');
 const first={id:'memory-a',type:'memory',title:'A rainy Sunday',date:'2026-10-01',notes:'Tea and a book.',image:'',status:'done'};
 const second={...first,id:'memory-b',date:'2026-10-08'};
 assert.equal(sameItem(first,second),false);assert.equal(mergeEntries([first],[second]).entries.length,2);
 assert.equal(mergeEntries([first],[{...first}]).duplicates,1);
 const different={...first,id:'memory-c',notes:'A different afternoon.'};assert.equal(sameItem(first,different),false);
});
test('Memory attachments sanitize snapshots, preserve covers, and merge on import', async()=>{
 const {normalizeAttachments}=await import('../core.mjs');
 const album={id:'album-a',type:'album',title:'In Rainbows',creator:'Radiohead',image:'/assets/in-rainbows.jpg'};
 const book={id:'book-a',type:'book',title:'Just Kids',image:'javascript:alert(1)'};
 const links=normalizeAttachments([album,album,book,{id:'nested',type:'memory',title:'No nested memories'},{type:'constructor',title:'bad'}]);
 assert.equal(links.length,2);assert.equal(links[0].image,album.image);assert.equal(links[1].image,'');
 const memory={id:'memory-a',type:'memory',title:'A day',date:'2026-11-01',status:'done',attachments:[album]};
 const m=mergeEntries([memory],[{...memory,attachments:[book]}]);assert.equal(m.entries[0].attachments.length,2);
 assert.equal(memory.attachments.length,1);
});
test('Old generic place CSV converts to a memory and remains eligible for a dated recap',()=>{
 const row=normalizeImport('Title,Type,Creator,Rating,Date,Notes\nA good day,restaurant,Denver,4.5,2026-10-20,Unhurried coffee.','old-export.csv').entries[0];
 assert.equal(row.type,'memory');assert.equal(row.rating,0);assert.equal(row.legacyRating,4.5);assert.equal(row.notes,'Unhurried coffee.');assert.equal(recapEntries([row],'2026-10').length,1);
});

test('A renamed memory matches its older backup by ID and keeps the current title and note',()=>{
 const current={id:'kept-memory',type:'memory',title:'The day we took the long way',date:'2026-11-01',status:'done',notes:'The final note.',attachments:[]};
 const old={...current,title:'A good afternoon',notes:'An older note.'};
 const result=mergeEntries([current],[old]);assert.equal(result.entries.length,1);assert.equal(result.added,0);assert.equal(result.entries[0].title,current.title);assert.equal(result.entries[0].notes,current.notes);
});
