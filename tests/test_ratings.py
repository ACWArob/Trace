from pathlib import Path
import gzip
import io
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ratings import RatingsStore,omdb_scores

class RatingTests(unittest.TestCase):
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
        self.assertTrue(all('apikey' not in x['url'] for x in scores))

if __name__=='__main__':unittest.main()
