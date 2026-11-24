from pathlib import Path
from copy import deepcopy
import importlib.util
import json
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from catalog_store import CatalogStore
from tests.pg_support import PostgresTestCase

SPEC=importlib.util.spec_from_file_location('catalog_transfer',Path(__file__).resolve().parents[1]/'scripts'/'public_catalog_transfer.py')
transfer=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)


class PublicTransferTests(PostgresTestCase):
    """Import writes to Postgres; export still reads a legacy local SQLite file (read-only)."""
    def setUp(self):
        super().setUp()
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.source=self.root/'source.sqlite3'
        self.store=CatalogStore(self.database)
        self.item={'type':'book','title':'Nedjma','creator':'Kateb Yacine','year':'1956',
                   'source':'Open Library','externalId':'/works/OL793980W','sourceUrl':'https://openlibrary.org/works/OL793980W',
                   'genres':['Fiction'],'image':'https://covers.openlibrary.org/b/id/123-L.jpg',
                   'publicScores':[{'source':'Open Library','value':4,'max':5,'count':8,'url':'https://openlibrary.org/works/OL793980W'}],
                   'catalogSources':[{'source':'Publication reference','externalId':'Nedjma-1956','sourceUrl':'https://www.bibliotheque.nat.tn/BNT/doc/SYRACUSE/135358'}]}
        self.store.save('book',None,{'items':[self.item]})
        self.output=self.root/'public.json'
        self.legacy_snapshot()

    def legacy_snapshot(self):
        """Copy the Postgres catalog into the legacy SQLite file layout that export reads."""
        with self.store.connect() as db:
            rows=[tuple(row) for row in db.execute('SELECT item_key,kind,title,creator,genres,payload,updated_at FROM catalog')]
        self.source.unlink(missing_ok=True)
        with sqlite3.connect(self.source) as legacy:
            legacy.execute('CREATE TABLE catalog (item_key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, '
                           'creator TEXT NOT NULL, genres TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL)')
            legacy.executemany('INSERT INTO catalog VALUES(?,?,?,?,?,?,?)',rows)
        legacy.close()

    def legacy_payload(self,row):
        with sqlite3.connect(self.source) as legacy:
            legacy.execute('UPDATE catalog SET payload=?',(json.dumps(row),))
        legacy.close()

    def clear_destination(self):
        with self.store.connect() as db:
            db.execute('DELETE FROM catalog')

    def payload(self):
        transfer.export_catalog(self.source,self.output)
        return json.loads(self.output.read_text())

    def test_export_reads_only_catalog_and_strips_unknown_private_fields_at_every_level(self):
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE secret_accounts(note TEXT)')
            db.execute('INSERT INTO secret_accounts VALUES(?)',('ACCOUNT_FIXTURE_PRIVATE',))
            row=deepcopy(self.item);row.update(notes='ENTRY_FIXTURE_PRIVATE',rating=5,attachments=[{'image':'PRIVATE'}],verifiedYear='1956')
            row['publicScores'][0]['notes']='SCORE_FIXTURE_PRIVATE';row['catalogSources'][0]['notes']='SOURCE_FIXTURE_PRIVATE'
            db.execute('UPDATE catalog SET payload=?',(json.dumps(row),))
            tables=[tuple(row) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        db.close()
        self.payload()
        encoded=self.output.read_text()
        self.assertNotIn('PRIVATE',encoded)
        self.assertNotIn('notes',encoded)
        self.assertNotIn('attachments',encoded)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode),0o600)
        with sqlite3.connect(self.source) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),tables)
            self.assertEqual(db.execute('SELECT count(*) FROM secret_accounts').fetchone()[0],1)
        db.close()

    def test_round_trip_preserves_identity_public_scores_and_verified_year_without_queries(self):
        self.legacy_payload({**self.item,'verifiedYear':'1956','verifiedImage':self.item['image']})
        self.payload();self.clear_destination()
        result=transfer.import_catalog(self.output,self.database)
        self.assertEqual(result['imported'],1)
        restored=self.store.identified('book','Open Library','/works/OL793980W')
        for key in ['title','creator','year','source','sourceUrl','externalId','publicScores']:
            self.assertEqual(restored[key],self.item[key])
        self.assertEqual(restored['verifiedYear'],'1956')
        self.assertEqual(restored['verifiedImage'],self.item['image'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM catalog_queries').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM pg_tables WHERE schemaname=? AND tablename='secret_accounts'",(self.schema,)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT title_norm,creator_norm FROM catalog").fetchone()[:],('nedjma','kateb yacine'))
        self.assertEqual(transfer.import_catalog(self.output,self.store)['imported'],1)
        self.assertEqual(self.store.browse('book')['total'],1)

    def test_kitsu_anime_films_keep_movie_category_and_exact_provider_identity(self):
        film={'type':'movie','title':'Anime film fixture','creator':'Studio fixture','year':'2001',
              'source':'Kitsu','externalId':'1234','sourceUrl':'https://kitsu.app/anime/1234',
              'image':'https://media.kitsu.app/anime/poster_images/1234/large.jpg','genres':['Animation']}
        self.store.save('movie',None,{'items':[film]});self.legacy_snapshot()
        payload=self.payload()
        self.assertEqual(next(row for row in payload['items'] if row['source']=='Kitsu')['type'],'movie')
        self.clear_destination()
        transfer.import_catalog(self.output,self.database)
        restored=self.store.identified('movie','Kitsu','1234')
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
                self.clear_destination()
                self.assertRaises(transfer.TransferError,transfer.import_catalog,self.output,self.database)
                self.assertEqual(self.store.browse('all')['total'],0)

    def test_export_skips_excluded_unknown_provider_and_private_inline_art(self):
        self.legacy_payload({**self.item,'catalogExcludedReason':'incorrect work'})
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
                self.assertRaises(transfer.TransferError,transfer.import_catalog,self.output,self.database)
        self.output.write_bytes(b'x'*101)
        with patch.object(transfer,'MAX_BYTES',100):
            self.assertRaises(transfer.TransferError,transfer.import_catalog,self.output,self.database)

    def test_export_enforces_byte_budget_before_writing(self):
        with patch.object(transfer,'MAX_BYTES',100):
            self.assertRaises(transfer.TransferError,transfer.export_catalog,self.source,self.output)
        self.assertFalse(self.output.exists())

    def test_import_strips_nul_characters_that_postgres_cannot_store(self):
        self.output.write_text(json.dumps({'format':transfer.FORMAT,'items':[{**self.item,'title':'Ned\x00jma','description':'Lo\x00ng'}]}))
        self.clear_destination()
        transfer.import_catalog(self.output,self.database)
        restored=self.store.identified('book','Open Library','/works/OL793980W')
        self.assertEqual((restored['title'],restored['description']),('Nedjma','Long'))

    def test_import_without_a_configured_database_fails_cleanly(self):
        self.payload()
        with patch.object(transfer.database.Database,'from_env',side_effect=ValueError('A Postgres connection URL is required.')):
            self.assertRaises(transfer.TransferError,transfer.import_catalog,self.output)

    def test_import_command_uses_the_environment_database(self):
        self.payload();self.clear_destination()
        url=self.database.dsn
        with patch.dict('os.environ',{'TRACE_DATABASE_URL':url},clear=False),\
                patch.object(transfer.database.Database,'from_env',return_value=self.database),\
                patch('builtins.print') as shown:
            transfer.main(['import','--source',str(self.output)])
        self.assertEqual(json.loads(shown.call_args[0][0]),{'imported':1})
        self.assertEqual(self.store.browse('book')['total'],1)


if __name__=='__main__':unittest.main()
