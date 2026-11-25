"""Open Library work search and exact ISBN edition lookups."""
import re
import math
import os
import unicodedata
from urllib.parse import urlencode

CATALOG_VERSION = 1
SOURCE = 'Open Library'
WORK_FIELDS = 'key,title,author_name,first_publish_year,cover_i,subject,ratings_average,ratings_count'


def book_genres(values, *, limit=8):
    if not isinstance(values, list):
        return []
    aliases = {'fiction (fictional works by one author)': 'Fiction'}
    return list(dict.fromkeys(aliases.get(value.casefold(), value) for value in values[:max(40, limit)]
                              if isinstance(value, str) and value.strip()
                              and not value.casefold().startswith('collection:')
                              and value.casefold() != 'new york times reviewed'))[:limit]


def book_identity(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold()
                   if c.isalnum())


def openlibrary_scores(item, fetch):
    identifier = item.get('externalId') or ''
    isbn = normalize_isbn(item.get('isbn') or '')
    if not re.fullmatch(r'/works/OL\d+W', identifier):
        if not isbn:
            return []
        data = fetch('https://openlibrary.org/search.json?' + urlencode({
            'q': 'isbn:' + isbn, 'fields': 'key,ratings_average,ratings_count', 'limit': 3}), 'openlibrary')
        if not isinstance(data, dict) or not isinstance(data.get('docs'), list):
            raise ValueError('Invalid Open Library ratings response.')
        works = {row.get('key'): row for row in data['docs'][:3] if isinstance(row, dict)
                 and re.fullmatch(r'/works/OL\d+W', str(row.get('key') or ''))}
        if len(works) != 1:
            return []
        identifier, row = next(iter(works.items()))
        average, count = row.get('ratings_average'), row.get('ratings_count')
    else:
        data = fetch('https://openlibrary.org' + identifier + '/ratings.json', 'openlibrary')
        summary = data.get('summary') if isinstance(data, dict) else None
        if not isinstance(summary, dict):
            raise ValueError('Invalid Open Library ratings response.')
        average, count = summary.get('average'), summary.get('count')
    if (type(average) not in (float, int) or not math.isfinite(average) or not 0 <= average <= 5
            or type(count) is not int or count <= 0):
        return []
    return [{'source': SOURCE, 'value': average, 'max': 5, 'count': count,
             'url': 'https://openlibrary.org' + identifier}]


def google_books_scores(item, fetch):
    isbn = normalize_isbn(item.get('isbn') or '')
    title, author = item.get('title') or '', item.get('creator') or ''
    if not isbn and not (title and author):
        return []
    query = 'isbn:' + isbn if isbn else f'intitle:"{title}" inauthor:"{author}"'
    params = {'q': query, 'maxResults': 5, 'printType': 'books'}
    if os.environ.get('GOOGLE_BOOKS_API_KEY'):
        params['key'] = os.environ['GOOGLE_BOOKS_API_KEY']
    data = fetch('https://www.googleapis.com/books/v1/volumes?' + urlencode(params), 'googlebooks')
    if not isinstance(data, dict) or 'error' in data:
        raise ValueError('Google Books is unavailable.')
    rows = data.get('items', [])
    if not isinstance(rows, list):
        raise ValueError('Invalid Google Books response.')
    for row in rows[:5]:
        if not isinstance(row, dict) or not isinstance(row.get('volumeInfo'), dict):
            continue
        info = row['volumeInfo']
        if info.get('printType') != 'BOOK':
            continue
        identifiers = info.get('industryIdentifiers')
        identifiers = identifiers if isinstance(identifiers, list) else []
        same_isbn = isbn and any(isinstance(x, dict) and normalize_isbn(str(x.get('identifier') or '')) == isbn for x in identifiers[:8])
        authors = info.get('authors')
        authors = authors if isinstance(authors, list) else []
        same_author = any(isinstance(x, str) and book_identity(x) in
                          [book_identity(a) for a in author.split(',')] for x in authors[:8])
        same_title = book_identity(str(info.get('title') or '')) == book_identity(title.split(':')[0])
        if not (same_isbn if isbn else same_title and same_author):
            continue
        value, count = info.get('averageRating'), info.get('ratingsCount')
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 5:
            continue
        identifier = row.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[\w-]{1,100}', identifier):
            continue
        return [{'source': 'Google Books', 'value': value, 'max': 5,
                 'count': count if type(count) is int and count > 0 else None,
                 'url': 'https://books.google.com/books?id=' + identifier}]
    return []


def normalize_isbn(value):
    text = re.sub(r'^ISBN(?:-1[03])?\s*:?\s*', '', value.strip(), flags=re.I)
    digits = re.sub(r'[=\s"-]', '', text).upper()
    if re.fullmatch(r'[0-9]{9}[0-9X]', digits):
        checksum = sum((10 if digit == 'X' else int(digit)) * (10 - i)
                       for i, digit in enumerate(digits))
        if checksum % 11:
            return ''
        prefix = '978' + digits[:9]
        return prefix + str((-isbn13_sum(prefix)) % 10)
    if re.fullmatch(r'97[89][0-9]{10}', digits) and isbn13_sum(digits) % 10 == 0:
        return digits
    return ''


def isbn13_sum(digits):
    return sum(int(digit) * (3 if i % 2 else 1) for i, digit in enumerate(digits))


def search_books(query, fetch):
    isbn = normalize_isbn(query)
    if isbn:
        return search_edition(isbn, fetch)
    data = fetch('https://openlibrary.org/search.json?' + urlencode({
        'q': query, 'limit': 16, 'fields': WORK_FIELDS,
    }), 'openlibrary')
    if not isinstance(data, dict) or not isinstance(data.get('docs'), list):
        raise ValueError('Invalid Open Library search response.')
    return [item for item in map(work_item, data.get('docs', [])[:16]) if item]


def work_item(row):
    """One Open Library search.json work row as a catalog item, or None."""
    if not isinstance(row, dict) or not isinstance(row.get('title'), str) or not row['title'].strip():
        return None
    key = str(row.get('key') or '')
    if re.fullmatch(r'OL[0-9]+W', key):
        key = '/works/' + key
    if not re.fullmatch(r'/works/OL[0-9]+W', key):
        return None
    url = 'https://openlibrary.org' + key
    authors = row.get('author_name')
    authors = authors if isinstance(authors, list) else []
    subjects = row.get('subject')
    subjects = subjects if isinstance(subjects, list) else []
    rating = row.get('ratings_average')
    count = row.get('ratings_count')
    rated = type(rating) in (int, float) and math.isfinite(rating) and 0 < rating <= 5 and type(count) is int and count > 0
    scores = [{'source': SOURCE, 'value': rating, 'max': 5, 'count': count, 'url': url}] if rated else []
    cover = row.get('cover_i')
    return {
        'title': row['title'], 'creator': ', '.join(a for a in authors[:2] if isinstance(a, str)),
        'year': str(row.get('first_publish_year') or ''), 'type': 'book',
        'image': f'https://covers.openlibrary.org/b/id/{cover}-L.jpg' if type(cover) is int and cover > 0 else '',
        'source': SOURCE, 'sourceUrl': url, 'externalId': key, 'genres': book_genres(subjects), 'publicScores': scores,
    }


def search_edition(isbn, fetch):
    key = 'ISBN:' + isbn
    data = fetch('https://openlibrary.org/api/books?' + urlencode({
        'bibkeys': key, 'jscmd': 'data', 'format': 'json',
    }), 'openlibrary')
    if not isinstance(data, dict):
        raise ValueError('Invalid Open Library edition response.')
    edition = data.get(key)
    if not isinstance(edition, dict) or not isinstance(edition.get('title'), str) or not edition['title'].strip():
        return []
    identifiers = edition.get('identifiers')
    identifiers = identifiers if isinstance(identifiers, dict) else {}
    reported = {normalize_isbn(value) for field in ('isbn_10','isbn_13')
                if isinstance(identifiers.get(field), list)
                for value in identifiers[field] if isinstance(value, str)} - {''}
    if reported and isbn not in reported:
        return []
    edition_ids = identifiers.get('openlibrary')
    edition_id = next((v for v in edition_ids if isinstance(v, str) and re.fullmatch(r'OL[0-9]+M', v)), '') if isinstance(edition_ids, list) else ''
    authors = edition.get('authors')
    authors = authors if isinstance(authors, list) else []
    published = re.search(r'\b[12][0-9]{3}\b', str(edition.get('publish_date') or ''))
    covers = edition.get('cover')
    cover = covers.get('large') if isinstance(covers, dict) else ''
    cover = cover if isinstance(cover, str) else ''
    # Only use an image linked by the exact edition's response.
    match = re.fullmatch(r'https?://covers\.openlibrary\.org(/b/id/[0-9]+-[LMS]\.jpg)(?:\?[^#]*)?', cover)
    cover = 'https://covers.openlibrary.org' + match[1] + '?default=false' if match else ''
    return [{
        'type': 'book', 'title': edition['title'], 'isbn': isbn,
        'creator': ', '.join(a['name'] for a in authors[:2] if isinstance(a, dict) and isinstance(a.get('name'), str)),
        'year': published[0] if published else '', 'image': cover, 'source': SOURCE,
        'sourceUrl': 'https://openlibrary.org/isbn/' + isbn,
        'externalId': '/books/' + edition_id if edition_id else key, 'genres': [],
    }]
