import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from book_catalog import google_books_scores, openlibrary_scores, book_genres
from catalog_store import CatalogStore
from music_catalog import search_albums, album_details, match_album, clean_album_genres
from ratings import RatingsStore
from score_service import ScoreService
from tests.pg_support import PostgresTestCase

MBID='2f40e601-5e93-4e3a-90f4-223f661ea4a7'

class MediaScoreTests(PostgresTestCase):
    def test_album_genres_exclude_chart_and_personal_library_tags(self):
        self.assertEqual(clean_album_genres(['electronic','ambient','offizielle charts','1–4 wochen','ph_2_stars','Classical']),
                         ['electronic','ambient','Classical'])
        self.assertEqual(book_genres(['Fiction (fictional works by one author)','collection:Name','Fiction','New York Times reviewed']),['Fiction'])

    def test_community_scores_separate_same_named_books_and_albums_by_creator(self):
        with tempfile.TemporaryDirectory() as folder:
            store=server.SocialStore(Path(folder)/'db.sqlite3')
            session,_=store.register({'username':'media_test','password':'synthetic-test-password'})
            for kind in ('book','album'):
                item={'type':kind,'title':'Same Title','creator':'First Creator','year':'2000'}
                store.publish(session['user']['id'],{'item':item,'rating':4})
                store.publish(session['user']['id'],{'item':{**item,'creator':'Second Creator'},'rating':1})
                self.assertEqual(store.community_rating(item)['value'],4)
                self.assertEqual(store.community_rating({**item,'creator':'Second Creator'})['value'],1)

    def test_openlibrary_work_ratings_and_isbn_resolution_keep_the_correct_work(self):
        item={'externalId':'/works/OL123W'}
        rows={'summary':{'average':4.25,'count':12}}
        result=openlibrary_scores(item,lambda url,provider:rows)
        self.assertEqual(result[0]['url'],'https://openlibrary.org/works/OL123W')
        self.assertEqual(result[0]['value'],4.25)
        rows['summary']['count']=0
        self.assertEqual(openlibrary_scores(item,lambda *args:rows),[])
        docs=[{'key':'/works/OL123W','ratings_average':4,'ratings_count':10}]
        self.assertEqual(openlibrary_scores({'isbn':'9780140328721'},lambda *args:{'docs':docs})[0]['count'],10)
        docs.append({'key':'/works/OL456W','ratings_average':5,'ratings_count':1})
        self.assertEqual(openlibrary_scores({'isbn':'9780140328721'},lambda *args:{'docs':docs}),[])

    def test_google_books_requires_correct_author_or_exact_isbn(self):
        item={'title':'A Book','creator':'An Author'}
        rows=[{'id':'wrong','volumeInfo':{'title':'A Book','authors':['Another Person'],'printType':'BOOK','averageRating':5}},
              {'id':'right','volumeInfo':{'title':'A Book','authors':['An Author'],'printType':'BOOK','averageRating':4,'ratingsCount':24}}]
        def fetch(url, provider):
            self.assertEqual((urlparse(url).netloc,provider),('www.googleapis.com','googlebooks'))
            return {'items':rows}
        self.assertEqual(google_books_scores(item, fetch)[0]['url'],'https://books.google.com/books?id=right')
        self.assertEqual(google_books_scores({**item,'isbn':'9780140328721'}, fetch),[])
        rows[1]['volumeInfo']['industryIdentifiers']=[{'type':'ISBN_13','identifier':'9780140328721'}]
        self.assertEqual(google_books_scores({**item,'isbn':'9780140328721'},fetch)[0]['value'],4)

    def test_verified_year_is_bound_to_its_record_and_survives_provider_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CatalogStore(Path(folder)/'db.sqlite3')
            item={'type':'book','title':'A Book','creator':'An Author','year':'1969',
                  'source':'Open Library','externalId':'/works/OL123W'}
            store.save('book',None,{'items':[item]})
            with store.connect() as db:
                db.execute("UPDATE catalog SET payload=json_set(payload,'$.year','2013','$.verifiedYear','2013')")
            store.save('book',None,{'items':[item,{**item,'externalId':'/works/OL456W'}]})
            self.assertEqual(store.identified('book','Open Library','/works/OL123W')['year'],'2013')
            self.assertEqual(store.identified('book','Open Library','/works/OL456W')['year'],'1969')

    def test_tv_and_anime_keep_native_scores_after_details_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog=CatalogStore(Path(folder)/'db.sqlite3')
            catalog.save('tv',None,{'items':[{'type':'tv','title':'A Show','year':'2000','source':'TVmaze','externalId':'123',
                'publicScores':[{'source':'TVmaze','value':8.5,'max':10}], 'imdbId':'tt1234567'}]})
            with patch.object(server,'CATALOG_STORE',catalog),patch('server.get_ratings') as ratings,patch('server.get_social') as social,patch('server.remote_scores') as remote:
                ratings.return_value.lookup.return_value=[]
                social.return_value.community_rating.return_value=None
                remote.return_value={'sources':[{'source':'IMDb','value':8,'max':10},{'source':'Letterboxd','value':4,'max':5}], 'providerStatus':{},'providerMeta':{}}
                for kind in ('tv','anime'):
                    result=server.title_scores(kind,'A Show','2000',source='TVmaze',external_id='123')
                    self.assertEqual([x['source'] for x in result['sources']],['TVmaze','IMDb'])
                    self.assertEqual(social.return_value.community_rating.call_args[0][0]['type'],kind)

    def test_album_lookup_validates_identity_and_does_not_treat_nonratings_as_ratings(self):
        data={'id':MBID,'title':'An Album','primary-type':'Album','artist-credit':[{'name':'An Artist'}],
              'genres':[{'name':'rai'},{'name':'folk'}],'rating':{'value':'4.2','votes-count':12}}
        def fetch(url, provider):
            self.assertEqual(parse_qs(urlparse(url).query)['inc'],['artist-credits+genres+ratings'])
            return data
        row=album_details(MBID,fetch)
        self.assertEqual(row['genres'],['rai','folk'])
        self.assertEqual(row['publicScores'][0]['value'],4.2)
        data['rating']['value']=True
        self.assertEqual(album_details(MBID,fetch)['publicScores'],[])
        data['id']='another-id'
        with self.assertRaises(ValueError):album_details(MBID,fetch)

    def test_genre_search_uses_provider_tags_and_excludes_unrelated_apple_titles(self):
        def fetch(url,provider):
            if provider=='apple':
                return {'results':[{'collectionType':'Album','wrapperType':'collection','collectionId':123,
                    'collectionName':'Rai','artistName':'Another Artist','primaryGenreName':'Pop'}]}
            self.assertIn('tag:',parse_qs(urlparse(url).query)['query'][0])
            self.assertIn('rai',parse_qs(urlparse(url).query)['query'][0])
            return {'release-groups':[{'id':MBID,'title':'An Album','primary-type':'Album',
                'artist-credit':[{'name':'An Artist'}],'tags':[{'name':'raï'}]}]}
        rows=search_albums('',fetch,genre='Raï')['items']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source'],'MusicBrainz')

    def test_apple_album_matching_rejects_a_different_artist_and_ambiguous_release(self):
        row={'id':MBID,'title':'An Album','primary-type':'Album','artist-credit':[{'name':'An Artist'}],'first-release-date':'2000'}
        def fetch(*args):return {'release-groups':[row]}
        item={'title':'An Album','creator':'An Artist','year':'2000'}
        self.assertEqual(match_album(item,fetch)['externalId'],MBID)
        self.assertIsNone(match_album({**item,'creator':'Another Artist'},fetch))
        with patch('music_catalog._musicbrainz_items',return_value=[
            {'title':'An Album','creator':'An Artist','year':'2000','externalId':MBID},
            {'title':'An Album','creator':'An Artist','year':'2000','externalId':'f555ea92-966e-4cc3-8d55-73ab39cda521'}]):
            self.assertIsNone(match_album(item,fetch))

    def test_book_and_album_cache_is_durable_and_separate_by_identity(self):
        first=ScoreService(RatingsStore(self.database))
        for kind,key,source in [('book','/works/OL123W','Google Books'),('album',MBID,'MusicBrainz')]:
            result=first.lookup(kind,key,{'public':{'enabled':True,'load':lambda s=source:[{'source':s,'value':4,'max':5}]}})
            self.assertEqual(result['sources'][0]['source'],source)
        second=ScoreService(RatingsStore(self.database))
        result=second.lookup('album',MBID,{'public':{'enabled':True,'load':lambda:self.fail('Fetched durable rating again')}})
        self.assertEqual(result['sources'][0]['value'],4)

    def test_metadata_patch_keeps_newer_artwork_and_adds_verified_source_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CatalogStore(Path(folder)/'db.sqlite3')
            original={'type':'album','source':'Apple Music','externalId':'123','title':'An Album',
                      'image':'https://example.org/old.jpg','genres':['Rock']}
            store.save('album',None,{'items':[{**original,'image':'https://example.org/new.jpg'}]})
            source={'source':'MusicBrainz','externalId':MBID,'sourceUrl':'https://musicbrainz.org/release-group/'+MBID}
            store.save('album',None,{'items':[{**original,'genres':['folk'],'catalogSources':[source]}]},metadata_only=True)
            saved=store.identified('album','Apple Music','123')
            self.assertEqual(saved['image'],'https://example.org/new.jpg')
            self.assertEqual(saved['genres'],['Rock','folk'])
            self.assertEqual(saved['catalogSources'],[source])

    def test_old_cached_album_genres_are_cleaned_without_provider_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CatalogStore(Path(folder)/'db.sqlite3')
            item={'type':'album','source':'MusicBrainz','externalId':MBID,'title':'An Album',
                  'genres':['ambient','offizielle charts','1–4 wochen']}
            store.save('album','old query',{'items':[item]})
            with store.connect() as db:
                db.execute('UPDATE catalog SET genres=?,payload=?',(json.dumps(item['genres']),json.dumps(item)))
                db.execute('UPDATE catalog_queries SET payload=?',(json.dumps({'items':[item]}),))
            self.assertEqual(store.cached('album','old query')['items'][0]['genres'],['ambient'])
            self.assertEqual(store.identified('album','MusicBrainz',MBID)['genres'],['ambient'])
            self.assertEqual(store.browse('album')['genres'],['ambient'])
            self.assertEqual(store.browse('all')['genres'],['ambient'])
            books=[{'type':'book','source':'Open Library','externalId':f'/works/OL{n}W','title':f'Book {n}',
                    'genres':[f'Genre {n*4+j}' for j in range(4)]} for n in range(3)]
            store.save('book',None,{'items':books})
            self.assertEqual(len(store.browse('book')['genres']),12)

    def test_server_persists_album_genres_without_replacing_matched_cover_or_identity(self):
        catalog=CatalogStore(self.database);ratings=RatingsStore(self.database)
        item={'type':'album','title':'An Album','creator':'An Artist','source':'Apple Music','externalId':'123',
              'image':'https://example.org/exact-cover.jpg','year':'2000','genres':['World']}
        catalog.save('album',None,{'items':[item]})
        details={'type':'album','source':'MusicBrainz','externalId':MBID,'title':'An Album','creator':'An Artist',
                 'sourceUrl':'https://musicbrainz.org/release-group/'+MBID,
                 'image':'https://example.org/canonical.jpg','genres':['rai'],'publicScores':[{'source':'MusicBrainz','value':4.2,'max':5}]}
        with patch.object(server,'CATALOG_STORE',catalog),patch.object(server,'RATINGS_STORE',ratings),patch.object(server,'SCORE_SERVICE',None), \
             patch('server.album_details',return_value=details) as load,patch('server.match_album',return_value={'externalId':MBID}) as match,patch('server.get_social') as social:
            social.return_value.community_rating.return_value=None
            try:
                result=server.title_scores('album','An Album','2000',creator='An Artist',source='Apple Music',external_id='123')
                again=server.title_scores('album','An Album','2000',creator='An Artist',source='Apple Music',external_id='123')
                self.assertEqual(result['sources'][0]['value'],4.2)
                self.assertEqual(again['genres'],['World','rai'])
                load.assert_called_once()
                match.assert_called_once()
                saved=catalog.identified('album','Apple Music','123')
                self.assertEqual(saved['image'],item['image'])
                self.assertEqual(catalog.browse('album')['total'],2)
            finally:server.SCORE_SERVICE.close()
