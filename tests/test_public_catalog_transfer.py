from pathlib import Path
from copy import deepcopy
import importlib.util
import json
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('catalog_transfer',Path(__file__).resolve().parents[1]/'scripts'/'public_catalog_transfer.py')
transfer=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)


class PublicTransferTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.source=self.root/'source.sqlite3'
        self.store=transfer.CatalogStore(self.source)
        self.item={'type':'book','title':'Nedjma','creator':'Kateb Yacine','year':'1956',
                   'source':'Open Library','externalId':'/works/OL793980W','sourceUrl':'https://openlibrary.org/works/OL793980W',
                   'genres':['Fiction'],'image':'https://covers.openlibrary.org/b/id/123-L.jpg',
                   'publicScores':[{'source':'Open Library','value':4,'max':5,'count':8,'url':'https://openlibrary.org/works/OL793980W'}],
                   'catalogSources':[{'source':'Publication reference','externalId':'Nedjma-1956','sourceUrl':'https://www.bibliotheque.nat.tn/BNT/doc/SYRACUSE/135358'}]}
        self.store.save('book',None,{'items':[self.item]})
        self.output=self.root/'public.json'

    def payload(self):
        transfer.export_catalog(self.source,self.output)
        return json.loads(self.output.read_text())

    def test_export_reads_only_catalog_and_strips_unknown_private_fields_at_every_level(self):
        with self.store.connect() as db:
            db.execute('CREATE TABLE secret_accounts(note TEXT)')
            db.execute('INSERT INTO secret_accounts VALUES(?)',('ACCOUNT_FIXTURE_PRIVATE',))
            row=deepcopy(self.item);row.update(notes='ENTRY_FIXTURE_PRIVATE',rating=5,attachments=[{'image':'PRIVATE'}],verifiedYear='1956')
            row['publicScores'][0]['notes']='SCORE_FIXTURE_PRIVATE';row['catalogSources'][0]['notes']='SOURCE_FIXTURE_PRIVATE'
            db.execute('UPDATE catalog SET payload=?',(json.dumps(row),))
            tables=[tuple(row) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        self.payload()
        encoded=self.output.read_text()
        self.assertNotIn('PRIVATE',encoded)
        self.assertNotIn('notes',encoded)
        self.assertNotIn('attachments',encoded)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode),0o600)
        with sqlite3.connect(self.source) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),tables)
            self.assertEqual(db.execute('SELECT count(*) FROM secret_accounts').fetchone()[0],1)

    def test_round_trip_preserves_identity_public_scores_and_verified_year_without_queries(self):
        with self.store.connect() as db:
            row={**self.item,'verifiedYear':'1956','verifiedImage':self.item['image']}
            db.execute('UPDATE catalog SET payload=?',(json.dumps(row),))
        self.payload();destination=self.root/'destination.sqlite3'
        result=transfer.import_catalog(self.output,destination)
        self.assertEqual(result['imported'],1)
        restored=transfer.CatalogStore(destination).identified('book','Open Library','/works/OL793980W')
        for key in ['title','creator','year','source','sourceUrl','externalId','publicScores']:
            self.assertEqual(restored[key],self.item[key])
        self.assertEqual(restored['verifiedYear'],'1956')
        self.assertEqual(restored['verifiedImage'],self.item['image'])
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM catalog_queries').fetchone()[0],0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='secret_accounts'").fetchone())
        self.assertEqual(transfer.import_catalog(self.output,destination)['imported'],1)
        self.assertEqual(transfer.CatalogStore(destination).browse('book')['total'],1)

    def test_kitsu_anime_films_keep_movie_category_and_exact_provider_identity(self):
        film={'type':'movie','title':'Anime film fixture','creator':'Studio fixture','year':'2001',
              'source':'Kitsu','externalId':'1234','sourceUrl':'https://kitsu.app/anime/1234',
              'image':'https://media.kitsu.app/anime/poster_images/1234/large.jpg','genres':['Animation']}
        self.store.save('movie',None,{'items':[film]})
        payload=self.payload()
        self.assertEqual(next(row for row in payload['items'] if row['source']=='Kitsu')['type'],'movie')
        destination=self.root/'films.sqlite3'
        transfer.import_catalog(self.output,destination)
        restored=transfer.CatalogStore(destination).identified('movie','Kitsu','1234')
        self.assertEqual(restored['sourceUrl'],film['sourceUrl'])
        self.assertEqual(restored['type'],'movie')
        self.assertRaises(transfer.TransferError,transfer.clean_item,{**film,'sourceUrl':'https://kitsu.app/anime/9999'})

    def test_export_refuses_overwrite_and_missing_source(self):
        self.output.write_text('keep')
        self.assertRaises(transfer.TransferError,transfer.export_catalog,self.source,self.output)
        self.assertEqual(self.output.read_text(),'keep')
        missing=self.root/'missing.sqlite3'
        self.assertRaises(transfer.TransferError,transfer.export_catalog,missing,self.root/'new.json')
        self.assertFalse(missing.exists())

    def test_import_rejects_private_or_unattributed_payload_before_destination_creation(self):
        for bad in [{**self.item,'notes':'private'}, {**self.item,'image':'data:image/jpeg;base64,AAAA'},
                    {**self.item,'source':'user upload'}, {**self.item,'type':[]},
                    {**self.item,'sourceUrl':'https://attacker.example/work'},
                    {**self.item,'sourceUrl':'https://openlibrary.org/works/OL999999W'},
                    {**self.item,'sourceUrl':self.item['sourceUrl']+'?api_key=fixture'},
                    {**self.item,'verifiedYear':'1969'},
                    {**self.item,'publicScores':[{'source':[],'value':4,'max':5}]}]:
            with self.subTest(bad=bad):
                self.output.write_text(json.dumps({'format':transfer.FORMAT,'items':[self.item,bad]}))
                destination=self.root/'should-not-exist.sqlite3'
                self.assertRaises(transfer.TransferError,transfer.import_catalog,self.output,destination)
                self.assertFalse(destination.exists())

    def test_export_skips_excluded_unknown_provider_and_private_inline_art(self):
        with self.store.connect() as db:
            row={**self.item,'catalogExcludedReason':'incorrect work'}
            db.execute('UPDATE catalog SET payload=?',(json.dumps(row),))
        result=transfer.export_catalog(self.source,self.output)
        self.assertEqual(result['exported'],0);self.assertEqual(result['skipped'],1)

    def test_previous_server_community_and_explicit_zero_vote_scores_are_not_transferred(self):
        row={**self.item,'publicScores':[{'source':'Trace','value':4,'max':5,'count':12},
                                        {'source':'Open Library','value':5,'max':5,'count':0}]}
        self.assertEqual(transfer.clean_item(row)['publicScores'],[])

    def test_import_limits_items_bytes_and_rejects_collection_exports(self):
        for value in [{'started':True,'entries':[self.item]}, {'format':transfer.FORMAT,'items':[self.item]*3}]:
            self.output.write_text(json.dumps(value))
            with patch.object(transfer,'MAX_ITEMS',2):
                self.assertRaises(transfer.TransferError,transfer.import_catalog,self.output,self.root/'dest.sqlite3')
        self.output.write_bytes(b'x'*101)
        with patch.object(transfer,'MAX_BYTES',100):
            self.assertRaises(transfer.TransferError,transfer.import_catalog,self.output,self.root/'dest.sqlite3')

    def test_export_enforces_byte_budget_before_writing(self):
        with patch.object(transfer,'MAX_BYTES',100):
            self.assertRaises(transfer.TransferError,transfer.export_catalog,self.source,self.output)
        self.assertFalse(self.output.exists())


if __name__=='__main__':unittest.main()
