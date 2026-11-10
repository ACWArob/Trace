from pathlib import Path
import gzip
import io
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ratings import RatingsStore,omdb_scores,mdblist_scores,load_local_rating_key

class RatingTests(unittest.TestCase):
    def test_provider_scores_and_missing_results_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.sqlite3';store=RatingsStore(path)
            sources=[{'source':'IMDb','value':8.4,'max':10,'count':12345,'url':'https://www.imdb.com/title/tt1234567/ratings/'}]
            store.save_provider_cache('mdblist','movie','tt1234567',sources,now=100)
            store.save_provider_cache('mdblist','movie','tt7654321',[],now=100)
            restarted=RatingsStore(path)
            self.assertEqual(restarted.read_provider_cache('mdblist','movie','tt1234567',now=101),
                             {'sources':sources,'status':'ok','fetchedAt':100,'stale':False,'refreshAt':604900})
            self.assertEqual(restarted.read_provider_cache('mdblist','movie','tt7654321',now=3699),
                             {'sources':[],'status':'missing','fetchedAt':100,'stale':False,'refreshAt':3700})
            self.assertTrue(restarted.read_provider_cache('mdblist','movie','tt7654321',now=3700)['stale'])
            self.assertIsNone(restarted.read_provider_cache('mdblist','movie','tt9999999',now=101))

    def test_provider_cache_separates_provider_kind_and_title(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RatingsStore(Path(directory)/'test.sqlite3')
            for provider,kind,imdb_id,value in [('mdblist','movie','tt1234567',8),('omdb','movie','tt1234567',7),
                                                ('mdblist','tv','tt1234567',6),('mdblist','movie','tt7654321',5)]:
                store.save_provider_cache(provider,kind,imdb_id,[{'source':'IMDb','value':value,'max':10}],now=100)
            for provider,kind,imdb_id,value in [('mdblist','movie','tt1234567',8),('omdb','movie','tt1234567',7),
                                                ('mdblist','tv','tt1234567',6),('mdblist','movie','tt7654321',5)]:
                self.assertEqual(store.read_provider_cache(provider,kind,imdb_id,now=101)['sources'][0]['value'],value)

    def test_persistent_failure_backoff_keeps_stale_successful_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.sqlite3';store=RatingsStore(path)
            sources=[{'source':'Letterboxd','value':4.2,'max':5}]
            store.save_provider_cache('mdblist','movie','tt1234567',sources,now=100,refresh_after=200)
            store.set_provider_backoff('mdblist','rate-limited',retry_after=3600,now=201)
            restarted=RatingsStore(path)
            cached=restarted.read_provider_cache('mdblist','movie','tt1234567',now=202)
            self.assertEqual(cached,{'sources':sources,'status':'ok','fetchedAt':100,'stale':True,'refreshAt':200})
            self.assertEqual(restarted.get_provider_backoff('mdblist',now=202),{'status':'rate-limited','refreshAt':3801})
            restarted.set_provider_backoff('mdblist','unavailable',now=203)
            self.assertEqual(restarted.get_provider_backoff('mdblist',now=204),{'status':'rate-limited','refreshAt':3801})
            self.assertIsNone(restarted.get_provider_backoff('omdb',now=204))
            self.assertIsNone(restarted.get_provider_backoff('mdblist',now=3801))
            restarted.set_provider_backoff('omdb','unavailable',now=4000)
            self.assertEqual(RatingsStore(path).get_provider_backoff('omdb',now=4001),{'status':'unavailable','refreshAt':4060})
            self.assertEqual(restarted.read_provider_cache('mdblist','movie','tt1234567',now=4001)['sources'],sources)

    def test_provider_cache_migration_preserves_existing_imdb_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('''CREATE TABLE public_scores (source TEXT NOT NULL,external_id TEXT NOT NULL,
                    value REAL NOT NULL,maximum REAL NOT NULL,votes INTEGER,url TEXT NOT NULL,
                    updated_at REAL NOT NULL,PRIMARY KEY(source,external_id))''')
                db.execute('INSERT INTO public_scores VALUES(?,?,?,?,?,?,?)',('IMDb','tt1234567',8.4,10,12000,'https://www.imdb.com/title/tt1234567/ratings/',100))
            store=RatingsStore(path)
            store.save_provider_cache('mdblist','movie','tt1234567',[{'source':'IMDb','value':9,'max':10}],now=100)
            restarted=RatingsStore(path)
            self.assertEqual(restarted.lookup('tt1234567')[0]['value'],8.4)
            self.assertEqual(restarted.lookup('tt1234567')[0]['count'],12000)
            self.assertEqual(restarted.read_provider_cache('mdblist','movie','tt1234567',now=101)['sources'][0]['value'],9)

    def test_private_local_key_loads_at_startup_with_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{},clear=True):
            path=Path(directory)/'mdblist-api-key'
            self.assertFalse(load_local_rating_key(directory))
            path.write_text('synthetic-local-key\n')
            path.chmod(0o600)
            self.assertTrue(load_local_rating_key(directory))
            self.assertEqual(os.environ['MDBLIST_API_KEY'],'synthetic-local-key')
            path.write_text('another-synthetic-key')
            self.assertTrue(load_local_rating_key(directory))
            self.assertEqual(os.environ['MDBLIST_API_KEY'],'synthetic-local-key')

    @unittest.skipUnless(os.name=='posix','POSIX file permissions')
    def test_local_key_rejects_group_or_world_readable_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{},clear=True):
            path=Path(directory)/'mdblist-api-key';path.write_text('synthetic-local-key')
            for mode in [0o644,0o640,0o606]:
                path.chmod(mode)
                self.assertFalse(load_local_rating_key(directory))
                self.assertNotIn('MDBLIST_API_KEY',os.environ)

    def test_local_key_rejects_empty_malformed_or_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{},clear=True):
            path=Path(directory)/'mdblist-api-key'
            for value in ['', 'two keys', 'line\nbreak', 'x'*513]:
                path.write_text(value)
                path.chmod(0o600)
                self.assertFalse(load_local_rating_key(directory))
            path.write_bytes(b'\xff')
            self.assertFalse(load_local_rating_key(directory))

    @unittest.skipUnless(os.name=='posix','POSIX symlinks')
    def test_local_key_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{},clear=True):
            target=Path(directory)/'other-key';target.write_text('synthetic-local-key');target.chmod(0o600)
            (Path(directory)/'mdblist-api-key').symlink_to(target)
            self.assertFalse(load_local_rating_key(directory))

    def test_imdb_dataset_is_streamed_and_keeps_rating_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RatingsStore(Path(directory)/'test.sqlite3')
            stream=io.BytesIO(gzip.compress(b'tconst\taverageRating\tnumVotes\ntt1234567\t8.4\t12000\nbad\t4.0\t2\n'))
            with patch('ratings.urlopen',return_value=stream):self.assertEqual(store.import_imdb(),1)
            self.assertEqual(store.lookup('tt1234567')[0]['count'],12000)
            self.assertEqual(store.lookup('tt1234567')[0]['value'],8.4)
            self.assertEqual(store.lookup('../../secret'),[])

    def test_omdb_sources_keep_their_scale_and_do_not_require_a_key(self):
        with patch.dict('os.environ',{},clear=True):self.assertEqual(omdb_scores('tt1234567','Test',None),[])
        fetch=lambda url,provider:{'imdbVotes':'12,345','Ratings':[{'Source':'Internet Movie Database','Value':'8.4/10'},{'Source':'Rotten Tomatoes','Value':'90%'},{'Source':'Metacritic','Value':'86/100'},{'Source':'Invalid','Value':'N/A'}]}
        with patch.dict('os.environ',{'OMDB_API_KEY':'synthetic-test-only'}):scores=omdb_scores('tt1234567','Test',fetch)
        self.assertEqual([x['max'] for x in scores],[10,100,100]);self.assertEqual(scores[0]['count'],12345)
        self.assertEqual(scores[1]['source'],'Rotten Tomatoes critics')
        self.assertTrue(all('apikey' not in x['url'] for x in scores))

    def test_mdblist_preserves_four_native_scales_and_safe_original_links(self):
        calls=[]
        def fetch(url,provider):
            calls.append((url,provider))
            return {'imdb_id':'tt1234567','score':99,'ratings':[
                {'source':'imdb','value':8.4,'score':84,'votes':12345},
                {'source':'tomatoes','value':92,'votes':300,'url':'https://www.rottentomatoes.com/m/test?apikey=must-not-leak'},
                {'source':'popcorn','value':88,'votes':'1,234','url':'https://www.rottentomatoes.com/m/test'},
                {'source':'letterboxd','value':4.1,'votes':5000,'url':'https://letterboxd.com/film/test/'},
                {'source':'metacritic','value':90},
            ]}
        with patch.dict('os.environ',{'MDBLIST_API_KEY':'synthetic-test-only'}):
            scores=mdblist_scores('tt1234567','Test','movie',fetch)
        self.assertEqual([x['source'] for x in scores],['IMDb','Rotten Tomatoes critics','Rotten Tomatoes audience','Letterboxd'])
        self.assertEqual([x['max'] for x in scores],[10,100,100,5])
        self.assertEqual([x['value'] for x in scores],[8.4,92,88,4.1])
        self.assertEqual(scores[2]['count'],1234)
        self.assertEqual(calls[0][1],'mdblist');self.assertIn('/imdb/movie/tt1234567/',calls[0][0])
        self.assertTrue(all(x['via']=='MDBList' and 'apikey' not in x['url'] for x in scores))

    def test_mdblist_is_optional_and_ignores_missing_or_invalid_scores(self):
        with patch.dict('os.environ',{},clear=True):self.assertEqual(mdblist_scores('tt1234567','Test','movie',None),[])
        with patch.dict('os.environ',{'MDBLIST_API_KEY':'synthetic-test-only'}):
            self.assertEqual(mdblist_scores('../secret','Test','movie',None),[])
            self.assertEqual(mdblist_scores('tt1234567','Test','book',None),[])
            result={'ratings':[None,{'source':[]}, {'source':'letterboxd','value':None,'score':90},
                               {'source':'imdb','value':True},{'source':'imdb','value':'NaN'},
                               {'source':'tomatoes','value':101},{'source':'letterboxd','value':9},
                               {'source':'popcorn','value':0,'votes':0,'url':'javascript:alert(1)'}]}
            scores=mdblist_scores('tt1234567','Test','movie',lambda *_:result)
            self.assertEqual(len(scores),1);self.assertEqual(scores[0]['value'],0)
            self.assertIsNone(scores[0]['count']);self.assertTrue(scores[0]['url'].startswith('https://www.rottentomatoes.com/search?'))
            with self.assertRaises(ValueError):mdblist_scores('tt1234567','Test','movie',lambda *_:{'imdb_id':'tt7654321','ratings':[]})
            with self.assertRaises(ValueError):mdblist_scores('tt1234567','Test','movie',lambda *_:{'ratings':None})

    def test_mdblist_supports_legacy_audience_name_and_show_ids(self):
        calls=[]
        def fetch(url,provider):
            calls.append(url)
            return {'ratings':[{'source':'tomatoesaudience','value':'80'}, {'source':'popcorn','value':81},
                               {'source':'letterboxd','value':4,'url':'https://evil.example/film/test/'}]}
        with patch.dict('os.environ',{'MDBLIST_API_KEY':'synthetic-test-only'}):
            scores=mdblist_scores('tt1234567','A Show','tv',fetch)
        self.assertEqual(len(scores),2);self.assertEqual(scores[0]['value'],81)
        self.assertIn('/imdb/show/',calls[0]);self.assertIn('/search/films/',scores[1]['url'])

if __name__=='__main__':unittest.main()
