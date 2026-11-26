import http.client
import json
from pathlib import Path
import threading
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from social import SocialStore, image_url
from tests.pg_support import PostgresTestCase, bind_server


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class SocialHTTPTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        bind_server(self,self.database)
        server.auth_budget().clear()
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
        self.host=f'127.0.0.1:{self.http.server_port}'

    def request(self,path,body=None,account=None,headers=None):
        head={'Host':self.host}
        if body is not None:
            head.update({'Origin':'http://'+self.host,'Content-Type':'application/json'})
        if account:
            head.update({'Cookie':account['cookie'],'X-Trace-CSRF':account['csrf']})
        head.update(headers or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.http.server_port)
        connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,head)
        response=connection.getresponse();raw=response.read();status=response.status;result_headers=dict(response.getheaders());connection.close()
        return status,json.loads(raw) if result_headers.get('Content-Type','').startswith('application/json') else raw,result_headers

    def signup(self,name):
        status,data,headers=self.request('/api/auth/signup',{'username':name,'displayName':name.title(),'password':'synthetic-test-passphrase'})
        self.assertEqual(status,201)
        return {**data,'cookie':headers['Set-Cookie'].split(';')[0]}

    def test_session_csrf_origin_and_host_boundaries(self):
        alice=self.signup('alice')
        status,data,headers=self.request('/api/auth/session',account=alice)
        self.assertEqual(data['user']['username'],'alice');self.assertTrue(data['csrf'])
        self.assertEqual(self.request('/api/social/profile',{'displayName':'Changed'},alice,{'X-Trace-CSRF':'wrong'})[0],403)
        self.assertEqual(self.request('/api/auth/login',{},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/auth/session',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/auth/signup',{'username':'alice','password':'another-test-password'})[0],409)
        self.assertEqual(self.request('/api/auth/signup',{'username':'ALICE','password':'another-test-password'})[0],409)
        self.assertEqual(self.request('/api/auth/login',{'username':'alice','password':'wrong-password'})[0],401)
        self.assertEqual(self.request('/api/auth/logout',{},alice)[0],200)
        self.assertIsNone(self.request('/api/auth/session',account=alice)[1]['user'])
        for path in ['/.trace/trace.sqlite3','/social.py','/AGENTS.md']:
            self.assertEqual(self.request(path)[0],404)

    def test_two_people_follow_like_comment_edit_remove_and_restore(self):
        alice,bob=self.signup('alice'),self.signup('bob')
        payload={'item':{'type':'movie','title':'A Test Film','year':'2020','genres':['Drama'],'notes':'PRIVATE','date':'2020-01-01'},'text':'A public test review.','rating':4.5}
        status,data,_=self.request('/api/social/reviews',payload,bob);self.assertEqual(status,201);review=data['review'];rid=review['id']
        self.assertNotIn('notes',review['item']);self.assertNotIn('date',review['item'])
        self.assertEqual(self.request('/api/social/feed?scope=following',account=alice)[1]['items'],[])
        followed=self.request('/api/social/follow',{'username':'bob','following':True},alice)[1]['user'];self.assertTrue(followed['isFollowing']);self.assertEqual(followed['followers'],1)
        self.assertEqual(len(self.request('/api/social/feed?scope=following',account=alice)[1]['items']),1)
        for _ in range(2):
            result=self.request('/api/social/like',{'id':rid,'liked':True},alice)[1]['review'];self.assertEqual(result['likes'],1)
        result=self.request('/api/social/comment',{'id':rid,'text':'A second opinion.'},alice)[1]['review'];self.assertEqual(result['commentCount'],1)
        self.assertEqual(result['comments'][0]['username'],'alice')
        self.assertEqual(self.request('/api/social/reviews',{**payload,'id':rid},alice)[0],403)
        self.assertEqual(self.request('/api/social/hide',{'id':rid,'hidden':True},alice)[0],403)
        self.assertEqual(self.request('/api/social/hide',{'id':rid,'hidden':True},bob)[0],200)
        self.assertEqual(self.request('/api/social/feed',account=alice)[1]['items'],[])
        self.request('/api/social/hide',{'id':rid,'hidden':False},bob)
        self.assertEqual(len(self.request('/api/social/feed',account=alice)[1]['items']),1)
        self.assertEqual(self.request('/api/social/follow',{'username':'bob','following':False},alice)[1]['user']['followers'],0)

    def test_profile_updates_and_latest_rating_per_person(self):
        alice,bob=self.signup('alice'),self.signup('bob')
        item={'type':'book','title':'A Book','year':'2020'}
        for rating in [5,2]:self.request('/api/social/reviews',{'item':item,'text':'Test review','rating':rating},alice)
        self.request('/api/social/reviews',{'item':item,'text':'Another view','rating':4},bob)
        score=server.get_social().community_rating(item)
        self.assertEqual(score['count'],2);self.assertEqual(score['value'],3)
        self.assertIsNone(server.get_social().community_rating({**item,'year':'1990'}))
        result=self.request('/api/social/profile',{'displayName':'Reader <b>One</b>','bio':'A little biography.'},alice)[1]['user']
        self.assertEqual(result['displayName'],'Reader <b>One</b>')
        self.assertNotIn('password_hash',result);self.assertNotIn('salt',result)

    def test_people_search_is_case_insensitive_and_escapes_wildcards(self):
        alice,bob=self.signup('alice'),self.signup('bob')
        self.request('/api/social/profile',{'displayName':'Wonder_Land 100%'},bob)
        store=server.get_social()
        self.assertEqual([p['username'] for p in store.people('ALIC')],['alice'])
        self.assertEqual([p['username'] for p in store.people('wonder_land')],['bob'])
        self.assertEqual([p['username'] for p in store.people('100%')],['bob'])
        self.assertEqual([p['username'] for p in store.people('%')],['bob'])
        self.assertEqual([p['username'] for p in store.people('b_b')],[])
        self.assertEqual(len(store.people('')),2)

    def test_community_rating_matches_creator_case_insensitively_for_books(self):
        alice=self.signup('alice')
        item={'type':'book','title':'A Book','creator':'Some Author','year':'2020'}
        self.request('/api/social/reviews',{'item':item,'text':'x','rating':4},alice)
        store=server.get_social()
        self.assertEqual(store.community_rating({**item,'title':'a BOOK','creator':'SOME author'})['count'],1)
        self.assertIsNone(store.community_rating({**item,'creator':'Other'}))
        self.assertIsNone(store.community_rating({**item,'type':'album'}))

    def test_invalid_reviews_are_rejected_without_creating_posts(self):
        alice=self.signup('alice')
        for payload in [{'item':{'type':[],'title':'A Film'},'text':'test'},{'item':{'type':{},'title':'A Film'},'text':'test'},{'item':{'type':'movie','title':'A Film'},'rating':float('nan')}, {'item':{'type':'bogus','title':'A Film'},'text':'test'}, {'item':{'type':'movie','title':'A Film'},'rating':4.3}]:
            self.assertEqual(self.request('/api/social/reviews',payload,alice)[0],400)
        self.assertEqual(self.request('/api/social/feed')[1]['items'],[])


class SocialPhotoTests(PostgresTestCase):
    REF = 'trace-photo:' + 'a' * 64

    def test_image_url_rejects_private_photo_refs(self):
        self.assertEqual(image_url(self.REF), '')
        self.assertEqual(image_url('TRACE-PHOTO:' + 'a' * 64), '')
        self.assertEqual(image_url('/api/photos/' + 'a' * 64), '')
        self.assertEqual(image_url('https://covers.example/x.jpg'), 'https://covers.example/x.jpg')

    def test_publish_with_a_photo_ref_stores_an_empty_image(self):
        store = SocialStore(self.database)
        session, _ = store.register({'username': 'ref_author', 'password': 'synthetic-password-1'})
        review = store.publish(session['user']['id'], {'item': {'type': 'memory', 'title': 'Synthetic memory', 'image': self.REF, 'sourceUrl': self.REF}, 'rating': 4, 'text': 'Synthetic'})
        self.assertEqual(review['item']['image'], '')
        self.assertEqual(review['item']['sourceUrl'], '')
        self.assertEqual(store.feed()['items'][0]['item']['image'], '')
        with self.database.connect() as db:
            self.assertNotIn('trace-photo:', db.execute('SELECT item FROM reviews').fetchone()[0])

    def test_delete_account_marks_only_that_accounts_photos(self):
        store = SocialStore(self.database)
        ids = []
        for name in ['photo_owner', 'photo_keeper']:
            session, _ = store.register({'username': name, 'password': 'synthetic-password-1'})
            ids.append(session['user']['id'])
        with self.database.connect() as db:
            for user_id in ids:
                db.execute("INSERT INTO photos(user_id,id,pathname,content_type,bytes,created_at) VALUES(?,?,?,'image/jpeg',1,0)",
                           (user_id, 'b' * 64, f'photos/{user_id}/' + 'b' * 64 + '.jpg'))
        store.delete_account(ids[0], 'synthetic-password-1')
        with self.database.connect() as db:
            marked = dict(db.execute('SELECT user_id,deleted_at IS NOT NULL FROM photos').fetchall())
        self.assertEqual(marked, {ids[0]: True, ids[1]: False})


if __name__=='__main__':unittest.main()
