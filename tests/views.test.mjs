import test from 'node:test';
import assert from 'node:assert/strict';
import {calendarDays,shiftMonth,chapters,wheelItems,bookRows} from '../views.mjs';

test('Calendar uses six Monday-first weeks, includes leap day, and has unique dates',()=>{
 const days=calendarDays('2024-02');assert.equal(days.length,42);assert.equal(days[0].date,'2024-01-29');assert.equal(new Set(days.map(x=>x.date)).size,42);assert.equal(days.filter(x=>x.inMonth).length,29);assert.ok(days.some(x=>x.date==='2024-02-29'&&x.inMonth));assert.throws(()=>calendarDays('2024-13'));
});
test('Calendar month navigation crosses year boundaries',()=>{assert.equal(shiftMonth('2026-12',1),'2027-01');assert.equal(shiftMonth('2026-01',-1),'2025-12');});
test('Diary chapters preserve chronological order and keep undated entries separate',()=>{
 const items=[{id:'a',status:'done',date:'2026-10-20'},{id:'b',status:'done',date:'2026-11-01'},{id:'c',status:'want',date:'2026-11-02'},{id:'d',status:'done',date:''}];const result=chapters(items);assert.deepEqual(result.map(x=>x.month),['2026-11','2026-10','undated']);assert.equal(result.flatMap(x=>x.entries).length,3);assert.equal(result[0].number,3);
});
test('Album wheel wraps around and never repeats an album in a small collection',()=>{
 const items=Array.from({length:7},(_,id)=>({id}));const visible=wheelItems(items,0);assert.equal(visible.find(x=>x.offset===0).item.id,0);assert.equal(visible.find(x=>x.offset===-1).item.id,6);for(let n=1;n<5;n++){const small=wheelItems(items.slice(0,n),-1);assert.equal(new Set(small.map(x=>x.item.id)).size,n);}assert.deepEqual(wheelItems([],0),[]);
});
test('Dedicated bookcase creates vertical rows without dropping or repeating books',()=>{const items=Array.from({length:29},(_,id)=>({id}));for(const width of [320,390,900,1440]){const rows=bookRows(items,width);assert.ok(rows.length>1);assert.deepEqual(rows.flat(),items);assert.ok(rows.every(x=>x.length<=12));}});


test('Book rows reserve a full-cover slot without moving selected books between rows',()=>{
 const items=Array.from({length:29},(_,id)=>({id}));
 for(const [width,reserved] of [[282,84],[352,84],[550,84],[760,107],[1100,107]]){
  const rows=bookRows(items,width,reserved);
  assert.deepEqual(rows.flat(),items);
  for(const row of rows)assert.ok(row.length*44+Math.max(0,row.length-1)*3+reserved<=width-30,'Expanded cover must fit inside the shelf');
 }
 assert.equal(bookRows(items,352,84)[0].length,4);
});
