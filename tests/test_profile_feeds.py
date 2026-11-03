import unittest
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from profile_feeds import profile_identifier,parse_feed,read_profile_feed,FeedUnavailable

class ProfileFeedTests(unittest.TestCase):
    def test_profile_identifiers_accept_only_supported_members(self):
        self.assertEqual(profile_identifier('letterboxd','https://letterboxd.com/test_user/'),'test_user')
        self.assertEqual(profile_identifier('letterboxd','test-user'),'test-user')
        self.assertEqual(profile_identifier('goodreads','https://www.goodreads.com/user/show/123-someone'),'123')
        self.assertEqual(profile_identifier('goodreads','123'),'123')
        for service,value in [('letterboxd','https://evil.example/user'),('goodreads','http://localhost:1234'),('letterboxd','https://letterboxd.com/settings/data/'),('other','123')]:
            with self.assertRaises(ValueError):profile_identifier(service,value)

    @patch('profile_feeds.urlopen')
    def test_invalid_profiles_never_make_a_request(self,open_url):
        with self.assertRaises(ValueError):read_profile_feed('goodreads','https://example.org/secret',None)
        open_url.assert_not_called()

    def test_letterboxd_reads_rating_and_watched_date_not_activity_date(self):
        xml=b'''<rss xmlns:letterboxd="https://letterboxd.com"><channel><item><title>A Film, 2023 - ****</title><link>https://letterboxd.com/test/film/a-film/</link><pubDate>Mon, 02 Nov 2026 12:00:00 +0000</pubDate><letterboxd:filmTitle>A Film</letterboxd:filmTitle><letterboxd:filmYear>2023</letterboxd:filmYear><letterboxd:memberRating>4.5</letterboxd:memberRating><letterboxd:watchedDate>2026-10-20</letterboxd:watchedDate><description><![CDATA[<p><img src="https://example.org/poster.jpg"></p><p>A &amp; B.</p>]]></description></item></channel></rss>'''
        item=parse_feed('letterboxd',xml)[0]
        self.assertEqual(item['rating'],4.5);self.assertEqual(item['date'],'2026-10-20');self.assertEqual(item['notes'],'A & B.');self.assertEqual(item['image'],'https://example.org/poster.jpg')
        item=parse_feed('letterboxd',xml.replace(b'<letterboxd:watchedDate>2026-10-20</letterboxd:watchedDate>',b''))[0]
        self.assertEqual(item['date'],'')

    def test_goodreads_preserves_unread_and_rejects_unsafe_xml(self):
        xml=b'''<rss><channel><item><title>A Book</title><book_id>12</book_id><author_name>A Writer</author_name><user_rating>0</user_rating><user_shelves>to-read</user_shelves><user_read_at></user_read_at></item></channel></rss>'''
        item=parse_feed('goodreads',xml)[0]
        self.assertEqual(item['type'],'book');self.assertEqual(item['status'],'want');self.assertEqual(item['rating'],0);self.assertEqual(item['date'],'')
        with self.assertRaises(FeedUnavailable):parse_feed('goodreads',b'<!DOCTYPE rss [<!ENTITY x "hello">]><rss/>')
        with self.assertRaises(FeedUnavailable):parse_feed('goodreads',b'<html><body>Unavailable</body></html>')

if __name__=='__main__':unittest.main()
