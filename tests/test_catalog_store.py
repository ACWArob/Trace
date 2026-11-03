from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from catalog_store import CatalogStore

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'test.sqlite3';self.store=CatalogStore(self.path)
        self.item={'type':'movie','title':'Arrival','creator':'Denis Villeneuve','year':'2016','genres':['Drama','Science Fiction'],'source':'Wikidata','externalId':'Q1','notes':'PRIVATE','rating':5,'date':'2026-11-03','publicScores':[]}

    def test_cache_is_persistent_attributed_and_excludes_personal_fields(self):
        self.store.save('movie','Arrival',{'items':[self.item],'provider':'Wikidata'})
        saved=CatalogStore(self.path).cached('movie','ARRIVAL')['items'][0]
        for field in ['notes','rating','date']:self.assertNotIn(field,saved)
        self.assertEqual(saved['source'],'Wikidata')
        self.assertEqual(self.store.browse('movie',genre='Science Fiction')['total'],1)
        self.assertEqual(self.store.browse('movie',query='%')['total'],0)
        self.assertEqual(self.store.matching('movie','Arrival','2016')['externalId'],'Q1')
        self.assertIsNone(self.store.matching('movie','Arrival','1996'))

    def test_expired_cache_can_be_used_explicitly_during_an_outage(self):
        self.store.save('movie','Arrival',{'items':[self.item]})
        with patch('catalog_store.time.time',return_value=10**12):
            self.assertIsNone(self.store.cached('movie','Arrival'))
            self.assertTrue(self.store.cached('movie','Arrival',stale=True)['stale'])

if __name__=='__main__':unittest.main()
