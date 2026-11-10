import unittest
import tempfile
from unittest.mock import patch
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class SearchTests(unittest.TestCase):
    def setUp(self):
        server.CACHE.clear()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        for name, store in [('CATALOG_STORE',server.CatalogStore),('RATINGS_STORE',server.RatingsStore),('SOCIAL_STORE',server.SocialStore)]:
            patcher=patch.object(server,name,store(Path(directory.name)/'test.sqlite3'));patcher.start();self.addCleanup(patcher.stop)

    @patch('server.fetch')
    def test_album_adapter_rejects_tracks_and_keeps_source(self, fetch):
        fetch.return_value={'results':[{'collectionType':'Album','collectionName':'In Rainbows','collectionId':1,'artistName':'Radiohead','collectionViewUrl':'https://music.apple.com/album/1','artworkUrl100':'https://example.org/100x100bb.jpg'}, {'wrapperType':'track','trackName':'Nude','trackId':2}]}
        result=server.search('album','rainbows')
        self.assertEqual(len(result['items']),1)
        item=result['items'][0]
        self.assertEqual(item['type'],'album')
        self.assertEqual(item['sourceUrl'],'https://music.apple.com/album/1')
        self.assertIn('600x600bb',item['image'])
        server.search('album','rainbows')
        self.assertEqual(fetch.call_count,2)

    @patch('server.search_albums')
    def test_old_album_cache_is_refreshed_for_the_second_provider(self, albums):
        server.get_catalog().save('album','Frank Ocean',{'items':[],'provider':'Apple Music'})
        albums.return_value={'items':[],'provider':'Apple + MusicBrainz','catalogVersion':server.MUSIC_CATALOG_VERSION}
        self.assertEqual(server.search('album','Frank Ocean')['catalogVersion'],server.MUSIC_CATALOG_VERSION)
        albums.assert_called_once()

    @patch('server.search_albums')
    def test_partial_album_results_retry_after_one_minute(self, albums):
        albums.return_value={'items':[],'provider':'Apple + MusicBrainz','catalogVersion':server.MUSIC_CATALOG_VERSION,
                             'warnings':['MusicBrainz is unavailable.'],'providerStatus':{'musicbrainz':'unavailable'}}
        with patch('server.time.time',return_value=1000):
            server.search('album','partial')
        with patch('server.time.time',return_value=1030):
            server.search('album','partial')
        self.assertEqual(albums.call_count,1)
        with patch('server.time.time',return_value=1061):
            server.search('album','partial')
        self.assertEqual(albums.call_count,2)

    @patch('server.search_wikidata')
    def test_old_in_memory_screen_results_are_refreshed_after_type_validation(self, lookup):
        server.CACHE[('movie','people')] = (server.time.time(), {'items': [], 'catalogVersion': 3})
        lookup.return_value=[]
        self.assertEqual(server.search('movie','people')['catalogVersion'],server.SCREEN_CATALOG_VERSION)
        lookup.assert_called_once()

    def test_outage_cache_cannot_restore_people_as_films_or_shows(self):
        source='Wikidata / Wikipedia'
        good={'type':'movie','title':'A film','source':source,'description':'2020 film about an actor'}
        cached={'items':[good,{'type':'movie','source':source,'description':'actor known for films'},
                         {'type':'tv','source':source,'description':'writer of television series'}],'stale':True}
        self.assertEqual(server.safe_cached_catalog(cached)['items'],[good])
        self.assertEqual(len(cached['items']),3)

    @patch('server.fetch')
    def test_books_and_tv_handle_missing_covers(self, fetch):
        fetch.return_value={'docs':[{'key':'/works/OL1W','title':'A Book','author_name':['A Writer']}]}
        item=server.search('book','a book')['items'][0]
        self.assertEqual(item['image'],'')
        self.assertEqual(item['sourceUrl'],'https://openlibrary.org/works/OL1W')
        fetch.return_value=[{'show':{'id':1,'name':'A Show','image':None,'premiered':None,'network':None}}]
        item=server.search('tv','a show')['items'][0]
        self.assertEqual(item['year'],'')
        self.assertEqual(item['image'],'')

    def test_personal_memories_and_legacy_places_are_not_catalogs(self):
        self.assertNotIn('memory',server.PROVIDERS)
        self.assertNotIn('place',server.PROVIDERS)
        self.assertNotIn('restaurant',server.PROVIDERS)

class DescriptionTests(unittest.TestCase):
    def setUp(self):
        server.CACHE.clear()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        for name, store in [('CATALOG_STORE',server.CatalogStore),('RATINGS_STORE',server.RatingsStore),('SOCIAL_STORE',server.SocialStore)]:
            patcher=patch.object(server,name,store(Path(directory.name)/'test.sqlite3'));patcher.start();self.addCleanup(patcher.stop)

    @patch('server.fetch')
    def test_openlibrary_description_formats_and_cache(self, fetch):
        fetch.return_value={'description':{'type':'/type/text','value':'A book description.'}}
        self.assertEqual(server.book_description('/works/OL123W')['description'],'A book description.')
        server.book_description('/works/OL123W')
        self.assertEqual(fetch.call_count,1)
        fetch.return_value={'description':'A second description.'}
        self.assertEqual(server.book_description('/works/OL124W')['description'],'A second description.')

    @patch('server.fetch')
    def test_description_rejects_external_urls_and_paths(self, fetch):
        for value in ['https://example.org', '/works/../../secret', '/works/OL123W?redirect=bad']:
            with self.assertRaises(ValueError): server.book_description(value)
        fetch.assert_not_called()

    @patch('server.fetch')
    def test_anime_films_are_movies_and_shows_keep_synopsis(self, fetch):
        fetch.return_value={'data':[{'id':'1','attributes':{'canonicalTitle':'An Anime Film','subtype':'movie','synopsis':'A film description.'}}]}
        movie=server.search('anime','a film')['items'][0]
        self.assertEqual(movie['type'],'movie')
        self.assertEqual(movie['description'],'A film description.')
        fetch.return_value=[{'show':{'id':2,'name':'A Show','summary':'<p>A &amp; B</p>'}}]
        show=server.search('tv','a show')['items'][0]
        self.assertEqual(show['description'],'A & B')

class GenreTests(unittest.TestCase):
    def setUp(self):
        server.CACHE.clear()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        for name, store in [('CATALOG_STORE',server.CatalogStore),('RATINGS_STORE',server.RatingsStore),('SOCIAL_STORE',server.SocialStore)]:
            patcher=patch.object(server,name,store(Path(directory.name)/'test.sqlite3'));patcher.start();self.addCleanup(patcher.stop)

    @patch('server.fetch')
    def test_movie_claims_resolve_genres_without_guessing_from_description(self, fetch):
        fetch.side_effect=[{'search':[{'id':'Q1','label':'A Film','description':'2020 film'}]},
                           {'entities':{'Q1':{'claims':{'P136':[{'mainsnak':{'datavalue':{'value':{'id':'Q9'}}}}]}}}},
                           {'entities':{'Q9':{'labels':{'en':{'value':'science fiction film'}}}}}]
        item=server.search('movie','a film')['items'][0]
        self.assertEqual(item['genres'],['Science Fiction'])
        self.assertEqual(item['year'],'2020')

    @patch('server.fetch')
    def test_album_tv_books_use_provider_genres(self, fetch):
        fetch.return_value={'results':[{'collectionType':'Album','collectionName':'Blue','primaryGenreName':'Folk','collectionId':9}]}
        self.assertEqual(server.search('album','blue')['items'][0]['genres'],['Folk'])
        fetch.return_value=[{'show':{'id':7,'name':'The Bear','genres':['Drama','Comedy']}}]
        self.assertEqual(server.search('tv','bear')['items'][0]['genres'],['Drama','Comedy'])
        fetch.return_value={'docs':[{'key':'/works/OL9W','title':'Dune','subject':['Science Fiction']}]}
        self.assertEqual(server.search('book','dune')['items'][0]['genres'],['Science Fiction'])

class PublicScoreTests(unittest.TestCase):
    def setUp(self):
        server.CACHE.clear()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        for name, store in [('CATALOG_STORE',server.CatalogStore),('RATINGS_STORE',server.RatingsStore),('SOCIAL_STORE',server.SocialStore)]:
            patcher=patch.object(server,name,store(Path(directory.name)/'test.sqlite3'));patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.dict('os.environ',{'MDBLIST_API_KEY':'synthetic-mdblist','OMDB_API_KEY':'synthetic-omdb'},clear=True)
        patcher.start();self.addCleanup(patcher.stop)
        with server.get_ratings().db() as db:
            db.execute('INSERT INTO public_scores VALUES(?,?,?,?,?,?,?)',('IMDb','tt1234567',8,10,1000,'https://www.imdb.com/title/tt1234567/ratings/',0))

    @patch('server.mdblist_scores')
    @patch('server.omdb_scores')
    def test_detail_and_discover_requests_share_provider_cache_and_deduplicate(self,omdb,mdblist):
        omdb.return_value=[{'source':'Internet Movie Database','value':8.1,'max':10},{'source':'Rotten Tomatoes','value':90,'max':100}]
        mdblist.return_value=[{'source':'IMDb','value':8.2,'max':10},{'source':'Rotten Tomatoes critics','value':91,'max':100},
                              {'source':'Rotten Tomatoes audience','value':88,'max':100},{'source':'Letterboxd','value':4.1,'max':5}]
        for _ in range(2):
            result=server.title_scores('movie','Test','2026','tt1234567')
            self.assertEqual(len(result['sources']),4)
            self.assertEqual(result['sources'][0]['value'],8.2)
            self.assertEqual(result['providerStatus'],{'omdb':'available','mdblist':'available'})
            self.assertNotIn('synthetic-',str(result))
        self.assertEqual(omdb.call_count,1);self.assertEqual(mdblist.call_count,1)

    @patch('server.mdblist_scores')
    @patch('server.omdb_scores')
    def test_provider_outage_keeps_imdb_and_other_provider_scores(self,omdb,mdblist):
        omdb.return_value=[{'source':'Rotten Tomatoes critics','value':90,'max':100}]
        mdblist.side_effect=URLError('Synthetic provider failure')
        result=server.title_scores('movie','Test','2026','tt1234567')
        self.assertEqual([x['source'] for x in result['sources']],['IMDb','Rotten Tomatoes critics'])
        self.assertEqual(result['providerStatus']['mdblist'],'unavailable')
        server.title_scores('movie','Test','2026','tt1234567')
        self.assertEqual(mdblist.call_count,1,'Do not retry failures on every render')

    @patch('server.mdblist_scores')
    @patch('server.omdb_scores')
    def test_account_rate_limit_backs_off_across_different_titles(self,omdb,mdblist):
        omdb.return_value=[]
        mdblist.side_effect=HTTPError('https://api.mdblist.com/',429,'Limited',{'Retry-After':'600'},None)
        first=server.title_scores('movie','Test','2026','tt1234567')
        second=server.title_scores('movie','Another','2026','tt7654321')
        self.assertEqual(first['providerStatus']['mdblist'],'rate_limited')
        self.assertEqual(second['providerStatus']['mdblist'],'rate_limited')
        self.assertEqual(mdblist.call_count,1)

    @patch('server.mdblist_scores')
    @patch('server.omdb_scores')
    def test_no_key_or_unmatched_title_never_requests_provider(self,omdb,mdblist):
        with patch.dict('os.environ',{},clear=True):
            result=server.title_scores('movie','Test','2026','tt1234567')
        self.assertEqual(result['providerStatus']['mdblist'],'not_configured')
        result=server.title_scores('movie','Unmatched','2026','../bad')
        self.assertEqual(result['providerStatus']['mdblist'],'unmatched')
        self.assertEqual(result['sources'],[])
        omdb.assert_not_called();mdblist.assert_not_called()

    @patch('server.mdblist_scores')
    @patch('server.omdb_scores',return_value=[])
    def test_distinct_titles_can_fetch_concurrently(self,omdb,mdblist):
        barrier=Barrier(2)
        def load(imdb_id,*args):
            barrier.wait(timeout=2)
            return [{'source':'Letterboxd','value':4,'max':5}]
        mdblist.side_effect=load
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(server.title_scores,'movie','Test','2026',imdb_id) for imdb_id in ['tt1234567','tt7654321']]
            for future in futures:self.assertEqual(future.result(timeout=5)['providerStatus']['mdblist'],'available')
        self.assertEqual(mdblist.call_count,2)

    @patch('server.mdblist_scores')
    @patch('server.omdb_scores',return_value=[])
    def test_simultaneous_copies_of_one_title_share_one_request(self,omdb,mdblist):
        entered,release=Event(),Event()
        def load(*args):
            entered.set()
            if not release.wait(timeout=3):raise TimeoutError()
            return [{'source':'Letterboxd','value':4,'max':5}]
        mdblist.side_effect=load
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(server.title_scores,'movie','Test','2026','tt1234567')
            self.assertTrue(entered.wait(timeout=2))
            second=executor.submit(server.title_scores,'movie','Test','2026','tt1234567')
            release.set()
            self.assertEqual(first.result(timeout=5)['sources'],second.result(timeout=5)['sources'])
        self.assertEqual(mdblist.call_count,1)

if __name__=='__main__': unittest.main()
