"""Primary cover sources: TVmaze for TV, Kitsu for anime, Open Library for books.

Every lookup matches strictly (normalized title, plus year or author surname) and
returns an image URL on the provider's fixed image host, or '' when there is none.
Provider failures raise so callers never cache an outage as a missing cover.
"""
import re
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from book_catalog import normalize_isbn
from screen_catalog import tvmaze_item

COVER_KINDS = ('tv', 'anime', 'book')
COVER_READ_LIMIT = 1_000_000
COVER_TIMEOUT = 8
TVMAZE_IMAGE_HOSTS = ('static.tvmaze.com',)
KITSU_IMAGE_HOSTS = ('media.kitsu.io', 'media.kitsu.app')
OPENLIBRARY_COVER_HOST = 'covers.openlibrary.org'
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_ARTICLES = re.compile(r"^(?:the|a|an|le|la|les|l'|el|los|las)\s+")
_LIST = re.compile(r'\s*(?:[,;&/]|\band\b|\bfeat\.?|\bft\.)\s*')
_LIST_HINT = re.compile(r'[;&/]|\band\b|\bfeat\.?|\bft\.')


def _plain(value):
    text = unicodedata.normalize('NFKD', str(value or ''))
    return ''.join(c for c in text if not unicodedata.combining(c)).lower()


def _alnum(value):
    return ''.join(c for c in value if c.isalnum())


def cover_title(value):
    """Python twin of core.mjs coverTitle."""
    plain = re.sub(r'[’`]', "'", _plain(value)).replace('&', ' and ')
    return _alnum(_ARTICLES.sub('', plain)) or _alnum(plain)


def _name_words(value):
    words = []
    for token in value.split():
        if re.fullmatch(r'(?:[^\W\d_]\.)+', token):
            continue  # Initials never act as surnames.
        word = _alnum(token)
        if word and word not in ('jr', 'sr', 'ii', 'iii', 'iv'):
            words.append(word)
    return words


def surnames(value):
    """Author surnames, as core.mjs personNames reads them ("Surname, Given" is one person)."""
    raw = _plain(value)
    if not _LIST_HINT.search(raw):
        pieces = raw.split(',')
        if len(pieces) == 2 and pieces[1].strip():
            last, first = _name_words(pieces[0]), _name_words(pieces[1])
            if last and (len(last) == 1 or len(first) <= 1):
                return {(first + last)[-1]}
    return {words[-1] for words in map(_name_words, _LIST.split(raw)) if words}


def _text(value, limit, name):
    if not isinstance(value, str):
        raise ValueError(f'Invalid {name}.')
    value = value.strip()
    if len(value) > limit or _CONTROL.search(value):
        raise ValueError(f'Invalid {name}.')
    return value


def cover_params(kind, title, year='', creator='', isbn=''):
    """Validated (kind, title, year, creator, isbn); creator and ISBN apply to books only."""
    if kind not in COVER_KINDS:
        raise ValueError('Choose TV, anime, or a book.')
    title = _text(title, 300, 'title')
    if not title:
        raise ValueError('Enter a title.')
    year = _text(year, 4, 'year')
    if year and not re.fullmatch(r'[12][0-9]{3}', year):
        raise ValueError('Invalid year.')
    if kind != 'book':
        return kind, title, year, '', ''
    creator = _text(creator, 300, 'author')
    isbn = _text(isbn, 40, 'ISBN')
    if isbn:
        isbn = normalize_isbn(isbn)
        if not isbn:
            raise ValueError('Invalid ISBN.')
    return kind, title, year, creator, isbn


def _image(value, hosts):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\x00-\x20\x7f\\]', value):
        return ''
    try:
        url = urlparse(value)
        port = url.port
    except ValueError:
        return ''
    if url.scheme != 'https' or url.hostname not in hosts or url.username or url.password or port or url.fragment:
        return ''
    return value


def _pick(candidates, year):
    """One image from (year, image) title matches, or '' when the match is ambiguous."""
    if year:
        exact = {image for found, image in candidates if found == year}
        if exact:
            return exact.pop() if len(exact) == 1 else ''
        candidates = [(found, image) for found, image in candidates if not found]
    images = {image for _, image in candidates}
    return images.pop() if len(images) == 1 else ''


def tvmaze_cover(title, year, fetch):
    data = fetch('https://api.tvmaze.com/search/shows?' + urlencode({'q': title}), 'tvmaze')
    if not isinstance(data, list):
        return ''
    wanted, candidates = cover_title(title), []
    for row in data[:16]:
        item = tvmaze_item(row.get('show')) if isinstance(row, dict) else None
        image = _image(item['image'], TVMAZE_IMAGE_HOSTS) if item else ''
        if image and cover_title(item['title']) == wanted:
            candidates.append((item['year'] if re.fullmatch(r'[0-9]{4}', item['year']) else '', image))
    return _pick(candidates, year)


def kitsu_cover(title, year, fetch):
    data = fetch('https://kitsu.io/api/edge/anime?' + urlencode({'filter[text]': title, 'page[limit]': 10}), 'kitsu')
    rows = data.get('data') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return ''
    wanted, candidates = cover_title(title), []
    for row in rows[:10]:
        attrs = row.get('attributes') if isinstance(row, dict) else None
        if not isinstance(attrs, dict):
            continue
        titles = attrs.get('titles') if isinstance(attrs.get('titles'), dict) else {}
        names = [value for value in [*list(titles.values())[:8], attrs.get('canonicalTitle')] if isinstance(value, str) and value.strip()]
        poster = attrs.get('posterImage') if isinstance(attrs.get('posterImage'), dict) else {}
        image = _image(poster.get('medium'), KITSU_IMAGE_HOSTS)
        if image and any(cover_title(name) == wanted for name in names):
            start = attrs.get('startDate') if isinstance(attrs.get('startDate'), str) else ''
            candidates.append((start[:4] if re.fullmatch(r'[0-9]{4}', start[:4]) else '', image))
    return _pick(candidates, year)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # A redirect to the image store means the cover exists; never follow it.


def openlibrary_isbn_cover(isbn, context, opener=None):
    """Open Library's cover for an ISBN; default=false makes a missing cover a 404."""
    isbn = normalize_isbn(isbn if isinstance(isbn, str) else '')
    if not isbn:
        raise ValueError('Invalid ISBN.')
    url = f'https://{OPENLIBRARY_COVER_HOST}/b/isbn/{isbn}-L.jpg?default=false'
    opener = opener or build_opener(HTTPSHandler(context=context), _NoRedirect())
    request = Request(url, method='HEAD', headers={'User-Agent': 'Trace/0.1 (https://github.com/ACWArob/Trace)'})
    try:
        with opener.open(request, timeout=COVER_TIMEOUT):
            return url
    except HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return url
        if exc.code in (404, 410):
            return ''
        raise


def openlibrary_search_cover(title, creator, year, fetch):
    params = {'title': title, 'fields': 'key,title,author_name,first_publish_year,cover_i', 'limit': 10}
    if creator:
        params['author'] = creator
    data = fetch('https://openlibrary.org/search.json?' + urlencode(params), 'openlibrary')
    if not isinstance(data, dict) or not isinstance(data.get('docs'), list):
        raise ValueError('Invalid Open Library search response.')
    wanted, names, matches = cover_title(title), surnames(creator), []
    for row in data['docs'][:10]:
        if not isinstance(row, dict) or not isinstance(row.get('title'), str) or cover_title(row['title']) != wanted:
            continue
        cover = row.get('cover_i')
        if type(cover) is not int or cover <= 0:
            continue
        authors = row.get('author_name') if isinstance(row.get('author_name'), list) else []
        found = set().union(*(surnames(name) for name in authors[:8] if isinstance(name, str)))
        year_match = bool(year) and str(row.get('first_publish_year') or '') == year
        # Same rule as coverMatch for books: a shared surname, or the year when either side lacks an author.
        if names and found:
            if not names & found:
                continue
        elif not year_match:
            continue
        matches.append((year_match, f'https://{OPENLIBRARY_COVER_HOST}/b/id/{cover}-L.jpg'))
    return next((image for exact, image in matches if exact), matches[0][1] if matches else '')


def find_cover(kind, title, year, creator, isbn, fetch, isbn_cover):
    if kind == 'tv':
        return tvmaze_cover(title, year, fetch)
    if kind == 'anime':
        return kitsu_cover(title, year, fetch)
    if kind == 'book':
        return (isbn and isbn_cover(isbn)) or openlibrary_search_cover(title, creator, year, fetch)
    raise ValueError('Unsupported cover type.')
