import test from 'node:test';
import assert from 'node:assert/strict';
import {crc32,deflateRawSync} from 'node:zlib';
import {readLetterboxdZip} from '../letterboxd_zip.mjs';
import {mergeEntries,normalizeImport} from '../core.mjs';

function zip(entries){
 const local=[],central=[];
 let offset=0;
 for(const [name,text,method=8] of entries){
  const file=Buffer.from(name), raw=Buffer.from(text), data=method===0?raw:deflateRawSync(raw);
  const header=Buffer.alloc(30);
  header.writeUInt32LE(0x04034b50);
  header.writeUInt16LE(method,8);
  header.writeUInt32LE(crc32(raw),14);
  header.writeUInt32LE(data.length,18);
  header.writeUInt32LE(raw.length,22);
  header.writeUInt16LE(file.length,26);
  local.push(header,file,data);
  const directory=Buffer.alloc(46);
  directory.writeUInt32LE(0x02014b50);
  directory.writeUInt16LE(method,10);
  directory.writeUInt32LE(crc32(raw),16);
  directory.writeUInt32LE(data.length,20);
  directory.writeUInt32LE(raw.length,24);
  directory.writeUInt16LE(file.length,28);
  directory.writeUInt32LE(offset,42);
  central.push(directory,file);
  offset+=header.length+file.length+data.length;
 }
 const directory=Buffer.concat(central), end=Buffer.alloc(22);
 end.writeUInt32LE(0x06054b50);
 end.writeUInt16LE(entries.length,8);
 end.writeUInt16LE(entries.length,10);
 end.writeUInt32LE(directory.length,12);
 end.writeUInt32LE(offset,16);
 return Uint8Array.from(Buffer.concat([...local,directory,end])).buffer;
}

test('ZIP imports ratings without a diary date and ignores deleted entries and lists',async()=>{
 const archive=zip([
  ['ratings.csv','Date,Name,Year,Rating\n2026-10-31,Arrival,2016,4.5\n'],
  ['diary.csv','Name,Year,Watched Date\nPerfect Days,2023,2026-10-30\n',0],
  ['reviews.csv','Date,Name,Year,Review\n'],
  ['deleted/diary.csv','Name,Year,Watched Date\nDeleted Movie,2000,2026-10-10\n'],
  ['lists/ratings.csv','Name,Year,Rating\nPrivate List,2026,5\n'],
 ]);
 const files=await readLetterboxdZip(archive);
 assert.deepEqual(files.map(x=>x.name),['diary.csv','reviews.csv','ratings.csv']);
 const rows=files.flatMap(file=>normalizeImport(file.text,file.name,'letterboxd',{allowEmpty:true}).entries);
 const result=mergeEntries([],rows).entries;
 assert.deepEqual(result.map(x=>[x.title,x.rating,x.date]),[['Perfect Days',0,'2026-10-30'],['Arrival',4.5,'']]);
});

test('ZIP rejects truncated, corrupted, duplicate, and over-limit selected entries',async()=>{
 await assert.rejects(readLetterboxdZip(new ArrayBuffer(4)),/Could not read/);
 const valid=zip([['ratings.csv','Name,Year,Rating\nArrival,2016,4.5\n',0]]);
 await assert.rejects(readLetterboxdZip(valid.slice(0,-1)),/Could not read/);
 const corrupt=valid.slice(0);
 new Uint8Array(corrupt)[30+'ratings.csv'.length]^=1;
 await assert.rejects(readLetterboxdZip(corrupt),/Could not read/);
 await assert.rejects(readLetterboxdZip(zip([['ratings.csv','Name'],['letterboxd-export/ratings.csv','Name']])),/Could not read/);
 const large=zip([['ratings.csv','a'.repeat(10*1024*1024+1)]]);
 await assert.rejects(readLetterboxdZip(large),/expands beyond/);
});
