import test from 'node:test';
import assert from 'node:assert/strict';
import {artworkCandidates, artworkFit} from '../artwork.mjs';

test('Portrait covers fit completely instead of cropping title or author', () => {
  const box=artworkFit(1000,1500,224,274);
  assert.equal(box.height,274);
  assert.ok(box.width<224 && box.x>0);
  assert.equal(box.y,0);
  assert.ok(Math.abs(box.width/box.height-1000/1500)<1e-12);
  assert.deepEqual(artworkFit(600,600,226,226),{x:0,y:0,width:226,height:226});
  assert.equal(artworkFit(0,600,226,226),null);
});

test('MusicBrainz alternatives retain the exact release group ID', () => {
  const id='0da340a0-6ad7-4fc2-a272-6f94393a7831';
  assert.deepEqual(artworkCandidates({image:`https://coverartarchive.org/release-group/${id}/front-500`}),[
    `https://coverartarchive.org/release-group/${id}/front-500`,
    `https://coverartarchive.org/release-group/${id}/front`,
  ]);
});

test('Open Library alternatives keep the same cover ID and reject generic missing-cover pixels', () => {
  assert.deepEqual(artworkCandidates({image:'https://covers.openlibrary.org/b/id/123-L.jpg'}),[
    'https://covers.openlibrary.org/b/id/123-L.jpg?default=false',
    'https://covers.openlibrary.org/b/id/123-M.jpg?default=false',
    'https://covers.openlibrary.org/b/id/123-S.jpg?default=false',
  ]);
});

test('Apple alternatives change only the dimensions of the same artwork', () => {
  assert.deepEqual(artworkCandidates('https://is1-ssl.mzstatic.com/image/thumb/Music/example/600x600bb.jpg'),[
    'https://is1-ssl.mzstatic.com/image/thumb/Music/example/600x600bb.jpg',
    'https://is1-ssl.mzstatic.com/image/thumb/Music/example/300x300bb.jpg',
    'https://is1-ssl.mzstatic.com/image/thumb/Music/example/100x100bb.jpg',
  ]);
});

test('TVmaze alternatives retain the image identity', () => {
  assert.deepEqual(artworkCandidates('https://static.tvmaze.com/uploads/images/medium_portrait/1/2.jpg'),[
    'https://static.tvmaze.com/uploads/images/medium_portrait/1/2.jpg',
    'https://static.tvmaze.com/uploads/images/original_untouched/1/2.jpg',
  ]);
});

test('User photos, local covers, and other source images never get guessed replacements', () => {
  for(const source of ['/assets/blonde.jpg','data:image/png;base64,YQ==','https://upload.wikimedia.org/real.jpg','https://example.org/photo.jpg']){
    assert.deepEqual(artworkCandidates(source),[source]);
  }
  assert.deepEqual(artworkCandidates({}),[]);
  assert.deepEqual(artworkCandidates('javascript:alert(1)'),[]);
});
