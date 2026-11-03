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

if __name__=='__main__': unittest.main()
