from pathlib import Path
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from catalog_store import CatalogStore, catalog_search_rank, catalog_search_text
from tests.pg_support import PostgresTestCase

class CatalogTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.store=CatalogStore(self.database)
        self.item={'type':'movie','title':'Arrival','creator':'Denis Villeneuve','year':'2016','genres':['Drama','Science Fiction'],'source':'Wikidata','externalId':'Q1','notes':'PRIVATE','rating':5,'date':'2026-11-03','publicScores':[]}

    def test_cache_is_persistent_attributed_and_excludes_personal_fields(self):
        self.store.save('movie','Arrival',{'items':[self.item],'provider':'Wikidata'})
        saved=CatalogStore(self.database).cached('movie','ARRIVAL')['items'][0]
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

    def test_partial_provider_cache_retries_after_a_minute_but_success_lasts_a_day(self):
        with patch('catalog_store.time.time',return_value=1000):
            self.store.save('movie','warning',{'items':[self.item],'warnings':['A catalog is unavailable.']})
            self.store.save('movie','status',{'items':[self.item],'providerStatus':{'secondary':'unavailable'}})
            self.store.save('movie','success',{'items':[self.item],'warnings':[],'providerStatus':{'secondary':'ok'}})
        with patch('catalog_store.time.time',return_value=1059):
            for query in ['warning','status']:
                self.assertFalse(self.store.cached('movie',query)['stale'])
        with patch('catalog_store.time.time',return_value=1060):
            for query in ['warning','status']:
                self.assertIsNone(self.store.cached('movie',query))
                self.assertTrue(self.store.cached('movie',query,stale=True)['stale'])
            self.assertFalse(self.store.cached('movie','success')['stale'])
        with patch('catalog_store.time.time',return_value=87399):
            self.assertFalse(self.store.cached('movie','success')['stale'])
        with patch('catalog_store.time.time',return_value=87400):
            self.assertIsNone(self.store.cached('movie','success'))
            self.assertTrue(self.store.cached('movie','success',stale=True)['stale'])

    def test_catalog_provenance_only_persists_public_fields(self):
        sources=[{'source':'MusicBrainz','sourceUrl':'https://musicbrainz.org/release-group/example','externalId':'example',
                  'notes':'PRIVATE','rating':5},
                 {'source':'Apple Music','sourceUrl':'https://music.apple.com/album/1','externalId':'1'},
                 'PRIVATE',{'source':{'notes':'PRIVATE'},'sourceUrl':'https://example.org','externalId':'bad'}]
        incoming={'items':[{**self.item,'catalogSources':sources}]}
        original=deepcopy(incoming)
        saved=self.store.save('movie','provenance',incoming)['items'][0]
        expected=[{key:source[key] for key in ['source','sourceUrl','externalId']} for source in sources[:2]]
        self.assertEqual(saved['catalogSources'],expected)
        self.assertEqual(incoming,original)
        self.assertEqual(self.store.cached('movie','provenance')['items'][0]['catalogSources'],expected)
        self.assertEqual(self.store.browse('movie')['items'][0]['catalogSources'],expected)
        self.assertEqual(self.store.save('movie','partial',{'items':[self.item]})['items'][0]['catalogSources'],expected)

    def test_partial_refresh_retains_public_metadata_in_rows_and_query_cache(self):
        enriched={**self.item,'image':'https://example.org/cover.jpg','description':'A public synopsis.',
                  'imdbId':'tt2543164','publicScores':[{'source':'IMDb','value':7.9,'max':10}]}
        self.store.save('movie','original',{'items':[enriched]})
        partial={**self.item,'creator':None,'year':'','image':'  ','description':None,
                 'imdbId':'','genres':[],'publicScores':[],'notes':'NEW PRIVATE NOTE'}
        incoming={'items':[partial],'provider':'Wikidata','warnings':['A secondary source is unavailable.']}
        original=deepcopy(incoming)
        result=self.store.save('movie','refresh',incoming)
        saved=result['items'][0]
        for field in ['image','description','creator','year','imdbId','genres','publicScores']:
            self.assertEqual(saved[field],enriched[field])
        for field in ['notes','rating','date']:
            self.assertNotIn(field,saved)
        self.assertEqual(incoming,original)
        self.assertEqual(self.store.cached('movie','refresh')['items'][0],saved)
        self.assertEqual(self.store.browse('movie')['items'][0],saved)
        self.assertEqual(result['warnings'],incoming['warnings'])

    def test_nonempty_refresh_can_replace_public_metadata(self):
        self.store.save('movie','original',{'items':[{**self.item,'image':'https://example.org/old.jpg',
            'description':'Old description','imdbId':'tt2543164','publicScores':[{'source':'IMDb','value':7.9,'max':10}]}]})
        replacement={**self.item,'creator':'Updated credit','year':'2017','image':'https://example.org/new.jpg',
                     'description':'Updated description','imdbId':'tt0000001','genres':['Thriller'],
                     'publicScores':[{'source':'IMDb','value':8.0,'max':10}]}
        saved=self.store.save('movie','updated',{'items':[replacement]})['items'][0]
        for field in ['image','description','creator','year','imdbId','genres','publicScores']:
            self.assertEqual(saved[field],replacement[field])
        self.assertEqual(self.store.browse('movie')['items'][0],saved)
        self.assertEqual(self.store.cached('movie','updated')['items'][0],saved)

    def test_genre_facets_follow_category_and_search_but_not_selected_genre(self):
        movies=[self.item, {**self.item,'title':'Zodiac','externalId':'Q2','genres':['Mystery']}]
        self.store.save('movie','movies',{'items':movies})
        self.store.save('book','books',{'items':[{**self.item,'type':'book','title':'The Hobbit','externalId':'OL1W','genres':['Fantasy']}]})
        self.store.save('album','albums',{'items':[{**self.item,'type':'album','title':'Blue','externalId':'1','genres':['Folk']}]})

        self.assertEqual(self.store.browse('movie')['genres'],['Drama','Mystery','Science Fiction'])
        self.assertEqual(self.store.browse('book')['genres'],['Fantasy'])
        self.assertEqual(self.store.browse('album')['genres'],['Folk'])
        selected=self.store.browse('movie',genre='Drama')
        self.assertEqual(selected['total'],1)
        self.assertEqual(selected['genres'],['Drama','Mystery','Science Fiction'])
        searched=self.store.browse('movie',query='Arrival',genre='Science Fiction')
        self.assertEqual(searched['genres'],['Drama','Science Fiction'])
        self.assertEqual(searched['counts'],{'movie':2,'book':1,'album':1})
        self.assertEqual(self.store.browse()['genres'],['Drama','Fantasy','Folk','Mystery','Science Fiction'])

    def test_audited_artwork_survives_a_provider_returning_the_old_missing_thumbnail(self):
        item={**self.item,'image':'https://example.org/verified.jpg'}
        self.store.save('movie','first',{'items':[item]})
        with self.store.connect() as db:
            row=json.loads(db.execute('SELECT payload FROM catalog').fetchone()[0])
            row['verifiedImage']=row['image']
            row['unavailableImages']=['https://example.org/missing.jpg']
            db.execute('UPDATE catalog SET payload=?',(json.dumps(row),))
        refreshed=self.store.save('movie','refresh',{'items':[{**self.item,'image':'https://example.org/missing.jpg'}]})
        self.assertEqual(refreshed['items'][0]['image'],item['image'])
        self.assertEqual(self.store.cached('movie','refresh')['items'][0]['image'],item['image'])
        new=self.store.save('movie','new-image',{'items':[{**self.item,'image':'https://example.org/new-cover.jpg'}]})['items'][0]
        self.assertEqual(new['image'],'https://example.org/new-cover.jpg')
        self.assertNotIn('verifiedImage',new)
        untrusted={**self.item,'externalId':'Q2','verifiedImage':'https://example.org/not-verified.jpg'}
        self.assertNotIn('verifiedImage',self.store.save('movie','untrusted',{'items':[untrusted]})['items'][0])

    def test_category_facets_preserve_paging_and_global_counts(self):
        movies=[{**self.item,'title':f'Film {number:02}','externalId':f'Q{number}','genres':['Drama' if number%2 else 'Science Fiction']} for number in range(42)]
        self.store.save('movie','movies',{'items':movies})
        self.store.save('album','albums',{'items':[{**self.item,'type':'album','externalId':'album1','genres':['Rock']}]})
        first=self.store.browse('movie')
        second=self.store.browse('movie',page=1)
        self.assertEqual(first['total'],42)
        self.assertEqual(len(first['items']),40)
        self.assertTrue(first['hasMore'])
        self.assertEqual(len(second['items']),2)
        self.assertFalse(second['hasMore'])
        self.assertEqual(second['genres'],['Drama','Science Fiction'])
        self.assertEqual(second['counts'],{'movie':42,'album':1})
        self.assertTrue({row['externalId'] for row in first['items']}.isdisjoint(row['externalId'] for row in second['items']))

    def test_nul_characters_and_lone_surrogates_are_stripped_before_saving(self):
        item={**self.item,'title':'Ar\x00rival','creator':'De\x00nis','description':'A\x00 note \ud800','genres':['Dra\x00ma'],
              'publicScores':[{'source':'IMDb\x00','value':7,'max':10}]}
        saved=self.store.save('movie','nul\x00query',{'items':[item]})['items'][0]
        self.assertEqual((saved['title'],saved['creator'],saved['genres']),('Arrival','Denis',['Drama']))
        self.assertEqual(self.store.browse('movie',query='Arrival')['items'][0]['description'],'A note ')
        self.assertEqual(self.store.cached('movie','nulquery')['items'][0]['title'],'Arrival')
        self.assertEqual(item['title'],'Ar\x00rival')

    def test_scalar_genres_are_stored_as_an_empty_list(self):
        self.store.save('movie','scalar',{'items':[{**self.item,'genres':'Drama'}]})
        self.assertEqual(self.store.browse('movie')['genres'],[])

    def test_literal_punctuation_search_is_case_insensitive_and_escaped(self):
        self.store.save('movie','punct',{'items':[{**self.item,'title':'!!!','externalId':'P1'},
                                                  {**self.item,'title':'100%','externalId':'P2'},
                                                  {**self.item,'title':'1000','externalId':'P3'}]})
        self.assertEqual([r['title'] for r in self.store.browse('movie',query='!!')['items']],['!!!'])
        self.assertEqual([r['title'] for r in self.store.browse('movie',query='%')['items']],['100%'])

    def test_title_matching_is_case_insensitive(self):
        self.store.save('movie','x',{'items':[self.item]})
        self.assertEqual(self.store.matching('movie','ARRIVAL','2016')['externalId'],'Q1')

    def test_excluded_rows_are_hidden_from_every_read(self):
        self.store.save('movie','visible',{'items':[self.item,{**self.item,'title':'Hidden','externalId':'Q2'}]})
        with self.store.connect() as db:
            db.execute("UPDATE catalog SET payload=jsonb_set(payload,'{catalogExcludedReason}','\"reason\"') WHERE item_key LIKE '%:Q2'")
        self.assertEqual(self.store.browse('all')['total'],1)
        self.assertEqual(self.store.browse('all')['counts'],{'movie':1})
        self.assertIsNone(self.store.identified('movie','Wikidata','Q2'))
        self.assertIsNotNone(self.store.identified('movie','Wikidata','Q1'))
        self.assertIsNone(self.store.matching('movie','Hidden','2016'))

    def test_isbn_query_matches_the_stored_isbn_exactly(self):
        book={**self.item,'type':'book','title':'The Hobbit','externalId':'OL1W','isbn':'9780261102217','genres':['Fantasy']}
        self.store.save('book','isbn',{'items':[book,{**book,'externalId':'OL2W','isbn':'9780261102218'}]})
        self.assertEqual([r['externalId'] for r in self.store.browse('book',query='978-0-261-10221-7')['items']],['OL1W'])

    def test_concurrent_saves_serialize_without_losing_rows(self):
        import threading
        errors=[]
        def work(number):
            try:
                rows=[{**self.item,'title':f'Film {number}-{index}','externalId':f'T{number}-{index}'} for index in range(5)]
                self.store.save('movie',f'thread-{number}',{'items':rows})
            except Exception as error:
                errors.append(error)
        threads=[threading.Thread(target=work,args=(number,)) for number in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(errors,[])
        self.assertEqual(self.store.browse('movie')['total'],20)


class CatalogRankParityTests(PostgresTestCase):
    """SQL ranking (title_norm/creator_norm CASE) must agree with the catalog_search_rank oracle."""
    FIXTURE=[('Amélie','Jean-Pierre Jeunet'),('Amelie Poulain','Someone Else'),('Blue','Joni Mitchell'),
             ('Blue Velvet','David Lynch'),('Velvet','Blue Oyster'),('Mitchell','Blue'),('Joni Mitchell','Blue'),
             ('Crème brûlée','Zoë Saldaña'),('Zoe','Saldana Creme Brulee'),('Brûlée','Creme'),('Æon Flux','Peter Chung'),
             ('The Blue Room','Georges Simenon'),('Room','The Blue'),('!!!','Chk Chk Chk'),('Hôtel du Nord','Marcel Carné'),
             ('Hotel','Nord'),('Ñandú','Pájaro'),('Nandu Pajaro','Ñandú'),('東京物語','小津安二郎'),('Tokyo Story','Yasujirō Ozu'),
             ('Ozu','Tokyo'),('It','Stephen King'),('Italy','Stephen King Italy'),('A','B'),('B','A')]
    QUERIES=['amelie','Amélie','blue','blue velvet','velvet blue','joni mitchell','mitchell joni','mitchell','creme brulee',
             'zoe saldana','nandu pajaro','pajaro','ozu','tokyo story','小津安二郎','東京物語','hotel nord','nord hotel','it','a','b',
             'stephen king','king stephen','oyster','aeon','room blue','the blue','e','é','zzz']

    def setUp(self):
        super().setUp()
        self.store=CatalogStore(self.database)
        rows=[{'type':'movie','title':title,'creator':creator,'year':'2000','genres':[],'source':'Wikidata','externalId':f'R{index}'}
              for index,(title,creator) in enumerate(self.FIXTURE)]
        self.store.save('movie',None,{'items':rows})

    def test_sql_ranking_matches_the_python_rank_oracle(self):
        for query in self.QUERIES:
            with self.subTest(query=query):
                search=catalog_search_text(query)
                oracle={f'R{index}':catalog_search_rank(title,creator,search) for index,(title,creator) in enumerate(self.FIXTURE)}
                expected={key:rank for key,rank in oracle.items() if rank is not None}
                got=[]
                for page in range(3):
                    result=self.store.browse('movie',query=query,page=page)
                    got.extend((row['externalId'],oracle[row['externalId']]) for row in result['items'])
                    if not result['hasMore']:break
                self.assertEqual(set(key for key,_ in got),set(expected))
                self.assertEqual(self.store.browse('movie',query=query)['total'],len(expected))
                self.assertEqual([rank for _,rank in got],sorted(expected.values()))

    def test_punctuation_only_query_falls_back_to_literal_search(self):
        self.assertEqual([row['title'] for row in self.store.browse('movie',query='!')['items']],['!!!'])

if __name__=='__main__':unittest.main()
