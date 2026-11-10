import test from 'node:test';
import assert from 'node:assert/strict';
import {parseCSV,normalizeImport,mergeEntries,recapEntries,dateOnly,safeUrl,normalizeGenres,normalizePublicScores,blendRatings,filmConsensus,reviewLinks} from '../core.mjs';

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

test('For later membership and descriptions merge without changing a completed entry',()=>{
 const old={id:'logged-film',type:'movie',title:'Arrival',year:'2016',status:'done',date:'2026-10-10',rating:4.5,notes:'My review',history:['2026-10-10']};
 const incoming={...old,id:'other-id',status:'want',date:'',rating:0,forLater:true,description:'A linguist communicates with extraterrestrial visitors.'};
 const result=mergeEntries([old],[incoming]);assert.equal(result.entries.length,1);const item=result.entries[0];assert.equal(item.forLater,true);assert.equal(item.status,'done');assert.equal(item.rating,4.5);assert.equal(item.date,old.date);assert.equal(item.notes,old.notes);assert.equal(item.description,incoming.description);
});


test('Genres normalize bounded arrays and CSV text, and merge without losing personal history',()=>{
 assert.deepEqual(normalizeGenres('Drama, drama; Science Fiction | Drama'),['Drama','Science Fiction']);
 assert.deepEqual(normalizeGenres([null,2,'  Indie   Rock ','indie rock']),['Indie Rock']);
 assert.equal(normalizeGenres(Array.from({length:20},(_,i)=>'Genre '+i)).length,8);
 const original={id:'mine',type:'movie',title:'Arrival',rating:4.5,notes:'My note',date:'2026-10-01',genres:['Drama'],history:[]};
 const result=mergeEntries([original],[{type:'movie',title:'Arrival',genres:['Science Fiction','Drama'],rating:1,notes:'Catalog note',date:'2026-11-03'}]);
 assert.deepEqual(result.entries[0].genres,['Drama','Science Fiction']);
 assert.equal(result.entries[0].rating,4.5);assert.equal(result.entries[0].notes,'My note');assert.equal(result.entries[0].date,'2026-10-01');
 assert.deepEqual(original.genres,['Drama']);
 const csv=normalizeImport('Title,Type,Genres\nArrival,movie,"Drama, Science Fiction"','trace.csv','generic');
 assert.deepEqual(csv.entries[0].genres,['Drama','Science Fiction']);
});


test('Trace ratings normalize real scales, weight known vote counts, and count each source once',()=>{
 const input=[{source:'IMDb',value:8,max:10,count:10000},{source:'Rotten Tomatoes',value:90,max:100,count:null}];
 assert.equal(blendRatings(input).value,4.2);assert.equal(blendRatings(input).count,2);
 assert.equal(blendRatings([...input,{source:'Internet Movie Database',value:9,max:10,count:1}]).count,2);
 assert.equal(blendRatings([{source:'TVmaze',value:8.2,max:10,count:null}]).value,4.1);
 assert.equal(blendRatings([{source:'Trace',value:4,max:5,count:25}]).value,4);
 assert.equal(blendRatings([]).value,null);
 assert.deepEqual(normalizePublicScores([{source:'Missing',value:null,max:5},{source:'Blank',value:'',max:5},{source:'Fake',value:1000,max:10},{source:'Empty',value:0,max:5,count:0},{source:'NaN',value:NaN,max:5}]),[]);
 assert.equal(input[0].value,8);
});
test('External review links use exact IDs when available and label title searches honestly',()=>{
 const exact=reviewLinks({type:'movie',title:'Moonlight',year:'2016',imdbId:'tt4975722',sourceUrl:'https://letterboxd.com/acwarob/film/moonlight-2016/'});
 assert.equal(exact[0].url,'https://www.imdb.com/title/tt4975722/reviews/');
 assert.equal(exact[1].url,'https://letterboxd.com/film/moonlight-2016/reviews/');
 assert.equal(reviewLinks({type:'book',title:'Dune'} )[0].label,'Find on Goodreads');
 assert.equal(reviewLinks({type:'book',title:'Dune',source:'goodreads',externalId:'44767458'})[0].url,'https://www.goodreads.com/book/show/44767458');
 assert.deepEqual(reviewLinks({type:'memory',title:'A Sunday'}),[]);
});

test('Movie consensus gives four distinct rating feeds equal weight on /10',()=>{
 const input=[{source:'IMDb',value:8,max:10,count:1_000_000},{source:'Rotten Tomatoes',value:90,max:100,count:300},{source:'popcorn',value:80,max:100},{source:'Letterboxd',value:4,max:5,count:20}];
 const result=filmConsensus(input);
 assert.equal(result.value,8.3);assert.equal(result.count,4);assert.deepEqual(result.missing,[]);
 assert.deepEqual(result.sources.map(x=>x.source),['IMDb','Rotten Tomatoes critics','Rotten Tomatoes audience','Letterboxd']);
 assert.equal(filmConsensus(input.map(x=>({...x,count:1}))).value,result.value,'Vote counts must not change the approved equal weights');
 assert.equal(input[1].source,'Rotten Tomatoes');
});
test('Movie consensus excludes absent sources instead of treating them as zero',()=>{
 const imdb={source:'IMDb',value:8,max:10};
 const result=filmConsensus([imdb,{source:'Letterboxd',value:4.5,max:5},{source:'Rotten Tomatoes',value:null,max:100}]);
 assert.equal(result.value,8.5);assert.equal(result.count,2);assert.equal(result.missing.length,2);
 assert.equal(filmConsensus([imdb]).value,null,'A single rating is not a Trace blend');
 assert.equal(filmConsensus([]).value,null);
 assert.equal(filmConsensus([imdb,{source:'Rotten Tomatoes audience',value:0,max:100}]).value,4,'An actual zero is valid');
});
test('Movie consensus deduplicates provider aliases and excludes other scores',()=>{
 const result=filmConsensus([{source:'IMDb',value:8,max:10},{source:'Internet Movie Database',value:9,max:10},{source:'popcorn',value:80,max:100},{source:'tomatoesaudience',value:90,max:100},{source:'Metacritic',value:100,max:100},{source:'Trace',value:5,max:5}]);
 assert.equal(result.value,8);assert.equal(result.count,2);assert.equal(result.other.length,2);
 assert.equal(filmConsensus([{source:'Metacritic',value:100,max:100}]).value,null);
 assert.equal(normalizePublicScores([{source:'__proto__',value:4,max:5}])[0].source,'__proto__');
});
