"""Read-only public profile feeds. No passwords, OAuth tokens, or arbitrary URLs."""
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen, build_opener, HTTPSHandler, HTTPRedirectHandler
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime
from datetime import date
import xml.etree.ElementTree as ET
import html
import json
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
    with urlopen(request, timeout=9, context=context) as response:
        payload = response.read(2 * 1024 * 1024 + 1)
    return {'items': parse_feed(service, payload), 'identifier': identifier, 'profileUrl': profile_url,
            'service': service, 'mode': 'public-feed', 'limited': True}


# A Letterboxd film's own poster, used only when catalog matching found no cover.
LETTERBOXD_PAGE_LIMIT = 600_000
LETTERBOXD_TIMEOUT = 8
_FILM_PATH = re.compile(r'/(?:[A-Za-z0-9_-]{1,40}/)?film/([A-Za-z0-9-]{1,200})/?')
_MEMBER_FILM_PATH = re.compile(r'/(?:[A-Za-z0-9_-]{1,40}/)?film/([A-Za-z0-9-]{1,200})/(?:\d{1,4}/)?')


def _https_parts(value, hosts):
    value = value if isinstance(value, str) else ''
    if not value or len(value) > 400 or re.search(r'[\x00-\x20\x7f\\]', value):
        raise ValueError('Invalid URL.')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc not in hosts or parsed.query or parsed.fragment):
        raise ValueError('Invalid URL.')
    return parsed


def letterboxd_film_url(value):
    """Canonical https://boxd.it/<code> or https://letterboxd.com/film/<slug>/, else ValueError."""
    parsed = _https_parts(value, ('boxd.it', 'letterboxd.com'))
    if parsed.netloc == 'boxd.it':
        match = re.fullmatch(r'/([A-Za-z0-9]{1,16})', parsed.path)
        if match:
            return 'https://boxd.it/' + match[1]
    else:
        match = _FILM_PATH.fullmatch(parsed.path)
        if match:
            return 'https://letterboxd.com/film/' + match[1].lower() + '/'
    raise ValueError('Use a Letterboxd film link.')


def letterboxd_redirect_target(value):
    """A redirect may only land on a letterboxd.com film page (a member's page maps to the film)."""
    parsed = _https_parts(value, ('letterboxd.com',))
    match = _MEMBER_FILM_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError('Letterboxd redirected away from a film page.')
    return 'https://letterboxd.com/film/' + match[1].lower() + '/'


class LetterboxdRedirectHandler(HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return Request(letterboxd_redirect_target(newurl), headers=dict(req.header_items()), method='GET')


def letterboxd_poster_image(value):
    if not isinstance(value, str):
        return ''
    value = html.unescape(value).strip()
    try:
        parsed = _https_parts(value, ('a.ltrbxd.com',))
    except ValueError:
        try:  # Poster URLs may carry a cache-busting query; nothing else.
            base, _, query = value.partition('?')
            if not re.fullmatch(r'[A-Za-z0-9=&._-]{1,80}', query):
                return ''
            parsed = _https_parts(base, ('a.ltrbxd.com',))
        except ValueError:
            return ''
    if 'empty-poster' in parsed.path.lower() or not re.fullmatch(r'/[A-Za-z0-9/._-]{1,300}', parsed.path):
        return ''
    return value


def _json_ld_image(data):
    if isinstance(data, list):
        return next((image for image in map(_json_ld_image, data[:8]) if image), '')
    if not isinstance(data, dict):
        return ''
    image = data.get('image')
    if isinstance(image, dict):
        image = image.get('url')
    if isinstance(image, list):
        image = next((entry for entry in image[:4] if isinstance(entry, str)), '')
    return letterboxd_poster_image(image) or _json_ld_image(data.get('@graph'))


def letterboxd_poster_from_html(payload):
    text = payload.decode('utf-8', 'replace') if isinstance(payload, bytes) else str(payload or '')
    for block in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.S | re.I)[:4]:
        block = re.sub(r'/\*.*?\*/', '', block, flags=re.S).strip()
        try:
            image = _json_ld_image(json.loads(block))
        except ValueError:
            continue
        if image:
            return image
    for tag in re.findall(r'<meta\b[^>]*>', text, re.I)[:200]:
        attrs = dict((name.lower(), content) for name, _, content in re.findall(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S))
        if attrs.get('property', '').lower() == 'og:image':
            image = letterboxd_poster_image(attrs.get('content'))
            if image:
                return image
    return ''


def fetch_letterboxd_poster(url, context, opener=None):
    """Poster URL for a validated film link, '' when there is none; raises on provider failure."""
    url = letterboxd_film_url(url)
    opener = opener or build_opener(HTTPSHandler(context=context), LetterboxdRedirectHandler())
    request = Request(url, headers={'User-Agent': 'Trace/0.1 (https://github.com/ACWArob/Trace)', 'Accept': 'text/html'})
    try:
        with opener.open(request, timeout=LETTERBOXD_TIMEOUT) as response:
            final = response.geturl()
            payload = response.read(LETTERBOXD_PAGE_LIMIT)
    except ValueError:  # Redirected somewhere other than a Letterboxd film page.
        return ''
    except HTTPError as exc:
        if exc.code in (404, 410):
            return ''
        raise
    try:
        if final != url:
            letterboxd_redirect_target(final)
    except ValueError:
        return ''
    return letterboxd_poster_from_html(payload)
