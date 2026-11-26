import test from 'node:test';
import assert from 'node:assert/strict';
import {mediaConsensus} from '../core.mjs';
import {TV_STARTERS} from '../tv_starters.mjs';
import {COLLECTION_STARTERS} from '../collection_starters.mjs';
import {DEMO} from '../data.js';

test('TV uses the TV community alongside IMDb and excludes Letterboxd from its blend',()=>{
 const result=mediaConsensus([{source:'IMDb',value:8,max:10},{source:'TVmaze',value:9,max:10},{source:'Letterboxd',value:1,max:5}],'tv');
 assert.equal(result.value,8.5);
 assert.equal(result.count,2);
 assert.deepEqual(result.missing,['Rotten Tomatoes critics','Rotten Tomatoes audience']);
 assert.ok(!result.other.some(row=>row.source==='Letterboxd'));
});
test('books and albums use the same scale and require independent sources',()=>{
 assert.equal(mediaConsensus([{source:'Open Library',value:4,max:5}],'book').value,null);
 assert.equal(mediaConsensus([{source:'Open Library',value:4,max:5},{source:'Google Books',value:3,max:5}],'book').value,7);
 assert.equal(mediaConsensus([{source:'MusicBrainz',value:4.5,max:5},{source:'Trace',value:4,max:5}],'album').value,8.5);
 assert.equal(mediaConsensus([{source:'MusicBrainz',value:4.5,max:5},{source:'MusicBrainz',value:2,max:5}],'album').count,1);
});
test('the first TV room has enough real titles without fetching or inventing viewing history',()=>{
 assert.ok(TV_STARTERS.length>=20);
 assert.ok(DEMO.filter(x=>x.type==='tv').length>=24);
 for(const item of TV_STARTERS){
  assert.equal(item.rating,0);
  assert.equal(item.date,'');
  assert.equal(item.status,'want');
  assert.ok(item.externalId);
  assert.ok(item.sourceUrl.startsWith('https://'));
  assert.ok(item.image.startsWith('https://'));
 }
 for(const kind of ['book','album']){
  assert.ok(COLLECTION_STARTERS[kind].length>=12);
  for(const item of COLLECTION_STARTERS[kind]){
   assert.equal(item.type,kind);
   assert.equal(item.date,'');
   assert.equal(item.rating,0);
   assert.equal(item.status,'want');
   assert.ok(item.sourceUrl.startsWith('https://'));
  }
 }
});
