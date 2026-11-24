"""Transfer an attributed public catalog without accounts, notes, or private tables."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_store import CatalogStore

MAX_ITEMS = 10000
MAX_BYTES = 20 * 1024 * 1024
FORMAT = 'trace-public-catalog-v1'
FIELDS = {'type','title','creator','year','image','genres','description','source','sourceUrl',
          'externalId','imdbId','isbn','publicScores','catalogSources','verifiedYear','verifiedImage','unavailableImages'}
SCORE_FIELDS = {'source','value','max','count','url'}
SOURCE_FIELDS = {'source','sourceUrl','externalId'}
PUBLIC_SCORES = {'IMDb','Rotten Tomatoes critics','Rotten Tomatoes audience','Letterboxd','TVmaze',
                 'Kitsu','Open Library','Google Books','MusicBrainz','Metacritic'}
PROVIDERS = {
 'Wikidata / Wikipedia': ({'movie','tv','anime'}, r'Q[1-9]\d*', {'www.wikidata.org','wikidata.org'}),
 'TVmaze': ({'tv','anime'}, r'[1-9]\d*', {'www.tvmaze.com','tvmaze.com'}),
 'Kitsu': ({'anime','movie'}, r'[1-9]\d*', {'kitsu.app','kitsu.io'}),
 'Open Library': ({'book'}, r'/(?:works/OL[1-9]\d*W|books/OL[1-9]\d*M)', {'openlibrary.org','www.openlibrary.org'}),
 'MusicBrainz': ({'album'}, r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', {'musicbrainz.org'}),
 'Apple Music': ({'album'}, r'[1-9]\d*', {'music.apple.com'}),
}


class TransferError(ValueError):
    pass


def _text(value, limit, *, required=False):
    if not isinstance(value,str) or len(value)>limit or (required and not value.strip()):
        raise TransferError('Catalog text is missing or exceeds a field limit.')
    return value


def _url(value, *, required=False):
    value = _text(value, 2000, required=required)
    if not value:
        return ''
    if re.fullmatch(r'/assets/[\w.-]+', value) and not required:
        return value
    try:
        parsed = urlparse(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443)
                or any(name.casefold() in {'api_key','apikey','access_token','token','key','secret'} for name,_ in parse_qsl(parsed.query))):
            raise ValueError()
    except ValueError as exc:
        raise TransferError('Catalog links must be public HTTPS URLs without credentials.') from exc
    return value


def _identity(item):
    source = item.get('source')
    provider = PROVIDERS.get(source) if isinstance(source,str) else None
    if not provider or not isinstance(item.get('type'),str) or item['type'] not in provider[0]:
        raise TransferError('Catalog record does not use a known provider and media type.')
    identifier = _text(item.get('externalId'), 500, required=True)
    url = _url(item.get('sourceUrl'),required=True)
    parsed=urlparse(url)
    if not re.fullmatch(provider[1],identifier) or parsed.hostname not in provider[2]:
        raise TransferError('Catalog provider identity is invalid.')
    path=parsed.path.rstrip('/')
    linked = {
        'Wikidata / Wikipedia': path=='/wiki/'+identifier,
        'TVmaze': path=='/shows/'+identifier or path.startswith('/shows/'+identifier+'/'),
        'Kitsu': path=='/anime/'+identifier,
        'Open Library': path==identifier or path.startswith(identifier+'/') or (isinstance(item.get('isbn'),str) and bool(item['isbn']) and path=='/isbn/'+item['isbn']),
        'MusicBrainz': path=='/release-group/'+identifier,
        'Apple Music': '/album/' in path and path.endswith('/'+identifier),
    }[source]
    if not linked:
        raise TransferError('Catalog attribution does not match its provider identity.')
    return source, identifier, url


def clean_item(row, *, strict=False):
    if not isinstance(row,dict) or (strict and set(row)-FIELDS):
        raise TransferError('Only allowlisted public catalog fields may be imported.')
    source, identifier, source_url = _identity(row)
    item = {'type':row['type'],'source':source,'externalId':identifier,'sourceUrl':source_url,
            'title':_text(row.get('title'),500,required=True)}
    for name,limit in [('creator',500),('description',20000),('year',4),('imdbId',20),('isbn',20)]:
        item[name] = _text(row.get(name,''),limit)
    if item['year'] and not re.fullmatch(r'(?:1\d{3}|20\d{2})',item['year']):
        raise TransferError('Catalog year must be a four-digit release year.')
    if item['imdbId'] and not re.fullmatch(r'tt\d{5,12}',item['imdbId']):
        raise TransferError('Catalog IMDb identity is invalid.')
    if item['isbn'] and not re.fullmatch(r'(?:\d{13}|\d{9}[\dX])',item['isbn']):
        raise TransferError('Catalog ISBN is invalid.')
    item['image'] = _url(row.get('image',''))
    genres = row.get('genres',[])
    if not isinstance(genres,list) or len(genres)>40:
        raise TransferError('Catalog genres must be a bounded list.')
    item['genres'] = list(dict.fromkeys(_text(g,100,required=True) for g in genres))
    scores = row.get('publicScores',[])
    if not isinstance(scores,list) or len(scores)>20:
        raise TransferError('Public score list exceeds its limit.')
    item['publicScores'] = []
    for score in scores:
        if not isinstance(score,dict) or (strict and set(score)-SCORE_FIELDS):
            raise TransferError('Public score record is invalid.')
        if not isinstance(score.get('source'),str):
            raise TransferError('Public score source is invalid.')
        if score['source'] not in PUBLIC_SCORES:
            continue  # A previous server's Trace community is not this server's community.
        value,maximum,count = score.get('value'),score.get('max'),score.get('count')
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise TransferError('Public score must be finite.')
        if isinstance(maximum,bool) or maximum not in (5,10,100) or not 0<=value<=maximum:
            raise TransferError('Public score scale is invalid.')
        if count is not None and (isinstance(count,bool) or not isinstance(count,int) or count<0):
            raise TransferError('Public vote count is invalid.')
        if count==0:
            continue
        item['publicScores'].append({'source':score['source'],'value':value,'max':maximum,'count':count,
                                     'url':_url(score.get('url',''))})
    sources = row.get('catalogSources',[])
    if not isinstance(sources,list) or len(sources)>8:
        raise TransferError('Catalog attribution exceeds its limit.')
    item['catalogSources'] = []
    for entry in sources:
        if not isinstance(entry,dict) or (strict and set(entry)-SOURCE_FIELDS):
            raise TransferError('Catalog attribution is invalid.')
        item['catalogSources'].append({'source':_text(entry.get('source'),100,required=True),
                                      'externalId':_text(entry.get('externalId'),500,required=True),
                                      'sourceUrl':_url(entry.get('sourceUrl'),required=True)})
    if row.get('verifiedYear'):
        if row['verifiedYear'] != item['year']:
            raise TransferError('Verified year must match its exact catalog record.')
        item['verifiedYear'] = item['year']
    if row.get('verifiedImage'):
        if row['verifiedImage'] != item['image']:
            raise TransferError('Verified artwork must match its catalog record.')
        item['verifiedImage'] = item['image']
    if row.get('unavailableImages'):
        unavailable = row['unavailableImages']
        if not isinstance(unavailable,list) or len(unavailable)>8:
            raise TransferError('Artwork audit exceeds its limit.')
        item['unavailableImages'] = [_url(url,required=True) for url in unavailable]
    return item


def _source(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise TransferError('Source must be an existing regular file.')
    return path


def export_catalog(source, output):
    source = _source(source)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise TransferError('Export destination already exists; no file was replaced.')
    items,skipped,size = [],0,0
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            # Do not instantiate CatalogStore: export must not initialize/migrate its source.
            rows = db.execute('SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END FROM catalog ORDER BY item_key LIMIT ?', (128*1024,MAX_ITEMS+1))
            for index,(payload,) in enumerate(rows):
                if index>=MAX_ITEMS:
                    raise TransferError('Catalog has more than 10,000 records; export a bounded source.')
                try:
                    row = json.loads(payload)
                    if isinstance(row,dict) and row.get('catalogExcludedReason'):
                        skipped += 1
                        continue
                    item = clean_item(row)
                    size += len(json.dumps(item,ensure_ascii=False,allow_nan=False).encode('utf-8')) + 2
                    if size>MAX_BYTES-100:
                        raise OverflowError()
                    items.append(item)
                except (ValueError,TypeError):
                    skipped += 1
    except OverflowError as exc:
        raise TransferError('Catalog export exceeds 20 MiB.') from exc
    except sqlite3.Error as exc:
        raise TransferError('Source must contain a readable public catalog table.') from exc
    data = json.dumps({'format':FORMAT,'items':items},ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(data)>MAX_BYTES:
        raise TransferError('Catalog export exceeds 20 MiB.')
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name = tempfile.mkstemp(prefix='.trace-public-',suffix='.partial',dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd,'wb') as handle:
            os.fchmod(handle.fileno(),0o600)
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        try:
            os.link(temporary,output)
        except FileExistsError as exc:
            raise TransferError('Export destination already exists; no file was replaced.') from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {'exported':len(items),'skipped':skipped,'path':str(output)}


def import_catalog(source,destination):
    source = _source(source)
    if source.stat().st_size>MAX_BYTES:
        raise TransferError('Catalog import exceeds 20 MiB.')
    try:
        with source.open('rb') as handle:
            encoded = handle.read(MAX_BYTES+1)
        if len(encoded)>MAX_BYTES:
            raise TransferError('Catalog import exceeds 20 MiB.')
        payload = json.loads(encoded.decode('utf-8'))
    except (ValueError,UnicodeError) as exc:
        raise TransferError('Catalog transfer must be valid UTF-8 JSON.') from exc
    if not isinstance(payload,dict) or set(payload)!={'format','items'} or payload.get('format')!=FORMAT:
        raise TransferError('Use a public catalog transfer, never a database or collection export.')
    if not isinstance(payload['items'],list) or len(payload['items'])>MAX_ITEMS:
        raise TransferError('Catalog import must contain at most 10,000 items.')
    items = [clean_item(row,strict=True) for row in payload['items']]
    identities=[(item['type'],item['source'],item['externalId']) for item in items]
    if len(set(identities))!=len(identities):
        raise TransferError('Catalog transfer contains conflicting duplicate identities.')
    destination = Path(destination).absolute()
    if destination.is_symlink() or destination==source:
        raise TransferError('Use a regular destination database separate from the input.')
    store = CatalogStore(destination)
    for kind in ('movie','tv','anime','book','album'):
        group = [item for item in items if item['type']==kind]
        for offset in range(0,len(group),250):
            store.save(kind,None,{'items':group[offset:offset+250]})
    # Preserve exact-record owner audit fields which normal provider ingestion cannot set.
    with store.connect() as db:
        for item in items:
            audits = {name:item[name] for name in ('verifiedYear','verifiedImage','unavailableImages') if name in item}
            if not audits:
                continue
            key = f"{item['type']}:{item['source']}:{item['externalId']}"
            stored = json.loads(db.execute('SELECT payload FROM catalog WHERE item_key=?',(key,)).fetchone()[0])
            if stored.get('year')==item.get('year') and 'verifiedYear' in audits:
                stored['verifiedYear']=audits['verifiedYear']
            if stored.get('image')==item.get('image'):
                for name in ('verifiedImage','unavailableImages'):
                    if name in audits:
                        stored[name]=audits[name]
            db.execute('UPDATE catalog SET payload=? WHERE item_key=?',(json.dumps(stored),key))
    return {'imported':len(items),'path':str(destination)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    export=sub.add_parser('export');export.add_argument('--source',type=Path,required=True);export.add_argument('--output',type=Path,required=True)
    load=sub.add_parser('import');load.add_argument('--source',type=Path,required=True);load.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=export_catalog(args.source,args.output) if args.command=='export' else import_catalog(args.source,args.destination)
    except (TransferError,OSError,sqlite3.Error) as exc:
        parser.exit(1,(str(exc) if isinstance(exc,TransferError) else 'Catalog transfer failed; check file access and database status.')+'\n')
    print(json.dumps(result))


if __name__=='__main__':
    main()
