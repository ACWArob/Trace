"""Read-only public profile feeds. No passwords, OAuth tokens, or arbitrary URLs."""
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen
from email.utils import parsedate_to_datetime
from datetime import date
import xml.etree.ElementTree as ET
import html
import math
import re

class FeedUnavailable(Exception):
    pass


def profile_identifier(service, value):
    value = str(value or '').strip()
    if service not in ('letterboxd', 'goodreads'):
        raise ValueError('Choose Letterboxd or Goodreads.')
    pattern = r'[A-Za-z0-9_-]{1,40}' if service == 'letterboxd' else r'\d{1,15}'
    if re.fullmatch(pattern, value):
        return value
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    hosts = ('letterboxd.com', 'www.letterboxd.com') if service == 'letterboxd' else ('goodreads.com', 'www.goodreads.com')
    if parsed.scheme not in ('https', 'http') or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise ValueError('Enter your public profile URL or ' + ('Letterboxd username.' if service == 'letterboxd' else 'Goodreads user ID.'))
    match = re.fullmatch(r'/([A-Za-z0-9_-]{1,40})/?', parsed.path) if service == 'letterboxd' else re.fullmatch(r'/user/show/(\d{1,15})(?:-[^/]*)?/?', parsed.path)
    if not match:
        raise ValueError('Use the link to your member profile, not a settings or book page.')
    return match.group(1)


def plain(value):
    return html.unescape(re.sub(r'<[^>]*>', '', value or '')).strip()


def date_only(value):
    value = str(value or '').strip()
    if re.match(r'^\d{4}[-/]\d{2}[-/]\d{2}', value):
        try:
            return date.fromisoformat(value[:10].replace('/', '-')).isoformat()
        except ValueError:
            return ''
    try:
        return parsedate_to_datetime(value).date().isoformat() if value else ''
    except (ValueError, TypeError, OverflowError):
        return ''


def rating(value):
    try:
        number = float(value)
        return round(max(0, min(5, number)) * 2) / 2 if math.isfinite(number) else 0
    except (ValueError, TypeError):
        return 0


def parse_feed(service, xml):
    if len(xml) > 2 * 1024 * 1024 or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
        raise FeedUnavailable('The public feed is not a supported RSS document.')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FeedUnavailable('The provider did not return a readable public feed.') from exc
    if root.tag.rsplit('}', 1)[-1] not in ('rss', 'feed'):
        raise FeedUnavailable('The provider did not return a public RSS feed.')
    items = []
    for entry in root.iter():
        if entry.tag.rsplit('}', 1)[-1] != 'item':
            continue
        fields = {child.tag.rsplit('}', 1)[-1]: child.text or '' for child in entry}
        get = lambda *names: next((fields[name] for name in names if fields.get(name)), '')
        if service == 'letterboxd':
            title = get('filmTitle') or re.sub(r', \d{4}(?:\s*-.*)?$', '', get('title'))
            review_html = get('description')
            image = re.search(r'<img[^>]+src=[\"\']([^\"\']+)', review_html, re.I)
            watched = date_only(get('watchedDate'))
            item = {'type': 'movie', 'title': plain(title), 'year': get('filmYear'), 'creator': '',
                    'rating': rating(get('memberRating')), 'date': watched, 'status': 'done',
                    'notes': plain(review_html), 'image': html.unescape(image.group(1)) if image else '',
                    'sourceUrl': get('link'), 'externalId': get('filmId') or get('link'),
                    'favorite': get('memberLike').lower() in ('yes', 'true')}
        else:
            shelves = get('user_shelves')
            status = 'want' if 'to-read' in shelves else 'progress' if 'currently-reading' in shelves else 'done'
            item = {'type': 'book', 'title': plain(get('title')), 'year': get('book_published'),
                    'creator': plain(get('author_name')), 'rating': rating(get('user_rating')),
                    'date': date_only(get('user_read_at')), 'status': status, 'notes': plain(get('user_review')),
                    'description': plain(get('book_description')), 'image': get('book_large_image_url', 'book_medium_image_url', 'book_image_url'),
                    'sourceUrl': get('link'), 'externalId': get('book_id'), 'favorite': False}
        if item['title']:
            item['source'] = service
            item['history'] = [item['date']] if item['date'] else []
            items.append(item)
        if len(items) >= 200:
            break
    return items


def read_profile_feed(service, identifier, context):
    identifier = profile_identifier(service, identifier)
    if service == 'letterboxd':
        profile_url = 'https://letterboxd.com/' + identifier + '/'
        feed_url = profile_url + 'rss/'
    else:
        profile_url = 'https://www.goodreads.com/user/show/' + identifier
        feed_url = 'https://www.goodreads.com/review/list_rss/' + identifier + '?' + urlencode({'shelf': '#ALL#', 'per_page': 200})
    request = Request(feed_url, headers={'User-Agent': 'TraceLocal/0.1 (personal collection)', 'Accept': 'application/rss+xml, application/xml, text/xml'})
    with urlopen(request, timeout=20, context=context) as response:
        payload = response.read(2 * 1024 * 1024 + 1)
    return {'items': parse_feed(service, payload), 'identifier': identifier, 'profileUrl': profile_url,
            'service': service, 'mode': 'public-feed', 'limited': True}
