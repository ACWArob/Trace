import test from 'node:test';
import assert from 'node:assert/strict';
import {recapEntries,selectTracewall,tracewallLayout,tracewallArtifact,tracewallShareCard} from '../core.mjs';

const item=(id,type='movie',extra={})=>({id,type,title:id,status:'done',date:'2026-10-12',...extra});
test('Share cards use fixed dimensions and carry all picks in order',()=>{
 const rows=Array.from({length:12},(_,i)=>item('pick-'+i));
 for(const [format,height,size] of [['story',1920,5],['portrait',1350,4],['square',1080,3]]){
  const first=tracewallShareCard(rows,format);
  assert.deepEqual([first.width,first.height,first.picks.length],[1080,height,size]);
  const cards=Array.from({length:first.pages},(_,i)=>tracewallShareCard(rows,format,i));
  assert.deepEqual(cards.flatMap(card=>card.picks),rows);
  assert.equal(tracewallShareCard(rows,format,99).index,first.pages-1);
  assert.equal(tracewallShareCard(rows,format,-1).index,0);
  assert.deepEqual(tracewallShareCard([],format,3).picks,[]);
 }
 assert.equal(tracewallShareCard(rows,'full'),null);
});
test('Tilted share layouts leave room for the header, footer, and story overlays',()=>{
 for(const [format,top,bottom,size] of [['story',480,1630,5],['portrait',262,1240,4],['square',228,984,3]]){
  for(let count=1;count<=size;count++){
   const {slots}=tracewallShareCard(Array.from({length:count},(_,i)=>item('pick-'+i)),format);
   for(const slot of slots){
    const radians=slot.tilt*Math.PI/180;
    const cos=Math.cos(radians),sin=Math.sin(radians);
    for(const x of [-slot.width/2,slot.width/2])for(const y of [-slot.height/2,slot.height/2]){
     const px=slot.x+slot.width/2+x*cos-y*sin;
     const py=slot.y+slot.height/2+x*sin+y*cos;
     assert.ok(px>=20&&px<=1060,`${format} horizontal bounds`);
     assert.ok(py>=top-.001&&py<=bottom+.001,`${format} vertical bounds`);
    }
   }
  }
 }
});
test('A large film import cannot crowd the other available categories out',()=>{
 const rows=[...Array.from({length:1000},(_,i)=>item('film-'+i)),item('book','book'),item('album','album'),item('memory','memory'),item('tv','tv'),item('anime','anime')];
 const snapshot=structuredClone(rows),selected=selectTracewall(rows);
 assert.equal(selected.length,9);
 for(const id of ['book','album','memory','tv','anime'])assert.ok(selected.some(x=>x.id===id),id);
 assert.equal(selected.filter(x=>x.type==='movie').length,4);
 assert.deepEqual(rows,snapshot);
});
test('Automatic picks keep order within a category and use only actual records',()=>{
 const rows=[item('a','book'),item('b','book'),item('c','album')];
 assert.deepEqual(selectTracewall(rows).map(x=>x.id),['a','c','b']);
 assert.deepEqual(selectTracewall([item('film')]).map(x=>x.id),['film']);
 assert.deepEqual(selectTracewall([]),[]);
});
test('Explicit picks keep their order, remove duplicates and ignore missing IDs',()=>{
 const rows=[item('a'),item('b','book'),item('c','album')];
 assert.deepEqual(selectTracewall(rows,['c','missing','a','c']).map(x=>x.id),['c','a']);
 assert.deepEqual(selectTracewall(rows,[]),[]);
});
test('Periods include only completed work; all time alone includes undated imports',()=>{
 const rows=[item('oct','book'),item('nov','album',{date:'2026-11-01'}),item('undated','memory',{date:''}),item('want','tv',{status:'want'}),item('progress','movie',{status:'progress'})];
 assert.deepEqual(selectTracewall(recapEntries(rows,'2026-10')).map(x=>x.id),['oct']);
 assert.deepEqual(new Set(selectTracewall(recapEntries(rows,'all')).map(x=>x.id)),new Set(['oct','nov','undated']));
});
test('Automatic and manual selection stays bounded and duplicate IDs never repeat',()=>{
 const rows=Array.from({length:30},(_,i)=>item(String(i)));
 assert.equal(selectTracewall([...rows,rows[0]],null,50).length,12);
 assert.equal(selectTracewall(rows,rows.map(x=>x.id)).length,12);
 assert.deepEqual(selectTracewall(rows,null,0),[]);
});
test('Phone and desktop compositions keep all tiles between header and footer',()=>{
 for(const columns of [2,3])for(const count of [0,1,2,5,9,12]){
  const layout=tracewallLayout(count,columns);
  assert.equal(layout.slots.length,count);
  for(const slot of layout.slots){assert.ok(slot.x>=0&&slot.x+slot.width<=layout.width);assert.ok(slot.y>=330&&slot.y+slot.height<layout.height-100);}
  if(count%columns===1){const last=layout.slots.at(-1);assert.ok(Math.abs(last.x+last.width/2-600)<1e-8);}
 }
});
test('Artwork shapes preserve media identity and fit inside their shared tile',()=>{
 for(const type of ['book','movie','tv','anime','album','memory']){
  const {image:box}=tracewallArtifact(type);
  assert.ok(box.x>=0&&box.y>=0&&box.x+box.width<=320&&box.y+box.height<302);
 }
 assert.equal(tracewallArtifact('album').image.width,tracewallArtifact('album').image.height);
 assert.ok(tracewallArtifact('book').image.height>tracewallArtifact('book').image.width);
 assert.equal(tracewallArtifact('anime').label,'TV');
 assert.equal(tracewallArtifact('memory').kind,'memory');
});
