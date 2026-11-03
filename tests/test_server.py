import unittest
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class SearchTests(unittest.TestCase):
    def setUp(self):
        server.CACHE.clear()

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
        self.assertEqual(fetch.call_count,1)

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

if __name__=='__main__': unittest.main()
