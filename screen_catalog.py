"""International movie/TV metadata from bounded, attributed Wikidata lookups."""
import html
import re
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlparse

from movie_catalog import (FILM_CLASSES, NON_WORK_CLASSES, WIKIDATA_SOURCE,
                           is_movie_result, is_non_work_description)


_WIKIDATA = 'https://www.wikidata.org/w/api.php?'
CATALOG_VERSION = 4
# Verified at https://www.wikidata.org/wiki/{id}: television series,
# miniseries, television program, and television-series episode respectively.
_TV_CLASSES = frozenset({'Q5398426', 'Q1259759', 'Q15416', 'Q7724161'})
_EPISODE_CLASSES = frozenset({'Q21191270'})
_TV = re.compile(r'\b(?:(?:tv|television|web|streaming)(?:[\s-]+[\w-]+){0,4}[\s-]+(?:series|show|program|sitcom)|'
                 r'miniseries|sitcom|docuseries|telenovela)\b|مسلسل', re.I)
_OTHER = re.compile(r'\b(?:episodes?|video[\s-]+games?|albums?|soundtracks?|films?|movies?|novels?|'
                    r'characters?|radio[\s-]+(?:series|program|show|sitcom))\b|حلقة|لعبة|فيلم|ألبوم', re.I)
_OPTIONAL_ERRORS = (URLError, TimeoutError, ValueError, OSError)


def _text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) else ''


def query_language(query):
    # Script selects a search index, never a country or the work's origin.
    for pattern, language in [
        (r'[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]', 'ar'),
        (r'[\u3040-\u30ff]', 'ja'), (r'[\uac00-\ud7a3]', 'ko'),
        (r'[\u3400-\u9fff]', 'zh'), (r'[\u0590-\u05ff]', 'he'),
        (r'[\u0900-\u097f]', 'hi'), (r'[\u0400-\u04ff]', 'ru'),
    ]:
        if re.search(pattern, query):
            return language
    return 'en'


def _claims(entity, property_id):
    claims = entity.get('claims') if isinstance(entity, dict) else None
    rows = claims.get(property_id) if isinstance(claims, dict) else None
    values = []
    for row in rows[:100] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get('rank') == 'deprecated':
            continue
        snak = row.get('mainsnak')
        data = snak.get('datavalue') if isinstance(snak, dict) else None
        if isinstance(data, dict):
            values.append(data.get('value'))
    return values


def _ids(entity, property_id, limit=8):
    identifiers = []
    for value in _claims(entity, property_id):
        identifier = value.get('id') if isinstance(value, dict) else None
        if isinstance(identifier, str) and re.fullmatch(r'Q[1-9]\d{0,14}', identifier) and identifier not in identifiers:
            identifiers.append(identifier)
        if len(identifiers) == limit:
            break
    return identifiers


def _localized(entity, field, language):
    values = entity.get(field) if isinstance(entity, dict) else None
    if not isinstance(values, dict):
        return ''
    for code in dict.fromkeys([language, 'en']):
        row = values.get(code)
        value = _text(row.get('value'), 12000 if field == 'descriptions' else 500) if isinstance(row, dict) else ''
        if value:
            return value
    return ''


def _is_tv(hit, entity):
    classes = set(_ids(entity, 'P31', 30))
    if classes & (_EPISODE_CLASSES | FILM_CLASSES | NON_WORK_CLASSES):
        return False
    description = hit.get('description', '')
    if is_non_work_description(description, hit.get('label', '')):
        return False
    television, other = _TV.search(description), _OTHER.search(description)
    if other and (not television or other.start() <= television.start()):
        return False
    return bool(classes & _TV_CLASSES) or bool(television)


def _year(entity, kind, description):
    for property_id in (['P577'] if kind == 'movie' else ['P580', 'P577']):
        years = []
        for value in _claims(entity, property_id):
            date = value.get('time') if isinstance(value, dict) else None
            match = re.match(r'^\+(\d{4})-\d{2}-\d{2}T', date) if isinstance(date, str) else None
            if match and match.group(1) != '0000':
                years.append(match.group(1))
        if years:
            return min(years)
    match = re.search(r'\b(?:18|19|20)\d{2}\b', description)
    return match.group(0) if match else ''


def _entities(data):
    entities = data.get('entities') if isinstance(data, dict) else None
    if not isinstance(entities, dict):
        raise ValueError('Invalid Wikidata metadata response.')
    return entities


def _wiki_title(entity, language):
    links = entity.get('sitelinks') if isinstance(entity, dict) else None
    row = links.get(language + 'wiki') if isinstance(links, dict) else None
    return _text(row.get('title')) if isinstance(row, dict) else ''


def _image(value):
    value = _text(value, 2000)
    try:
        parsed = urlparse(value)
        parsed.port
        host = parsed.hostname or ''
        if (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and (host == 'wikimedia.org' or host.endswith('.wikimedia.org'))
                and not re.search(r'[\x00-\x20\x7f]', value)):
            return value
    except ValueError:
        pass
    return ''


def _wiki_pages(data):
    query = data.get('query') if isinstance(data, dict) else None
    pages = query.get('pages') if isinstance(query, dict) else None
    if not isinstance(pages, dict):
        raise ValueError('Invalid Wikipedia metadata response.')
    aliases = {}
    for field in ('normalized', 'redirects'):
        rows = query.get(field)
        for row in rows[:40] if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get('from'), str) and isinstance(row.get('to'), str):
                aliases[row['from']] = row['to']
    by_title = {_text(page.get('title')): page for page in list(pages.values())[:40]
                if isinstance(page, dict) and 'missing' not in page and 'invalid' not in page}
    for old, new in aliases.items():
        for _ in range(5):
            if new not in aliases:
                break
            new = aliases[new]
        if new in by_title:
            by_title[old] = by_title[new]
    return by_title


def search_wikidata(query, kind, fetch):
    """Return at most 20 film/show records in at most four provider requests."""
    if kind not in ('movie', 'tv') or not isinstance(query, str) or not 2 <= len(query.strip()) <= 200:
        raise ValueError('Choose movies or TV and enter 2–200 characters.')
    query = query.strip()
    language = query_language(query)
    languages = '|'.join(dict.fromkeys([language, 'en']))
    data = fetch(_WIKIDATA + urlencode({'action': 'wbsearchentities', 'search': query,
                 'language': language, 'uselang': language, 'format': 'json', 'limit': 20}), 'wikidata')
    hits = data.get('search') if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise ValueError('Invalid Wikidata search response.')
    hits_by_id = {}
    for hit in hits[:20]:
        if isinstance(hit, dict) and isinstance(hit.get('id'), str) and re.fullmatch(r'Q[1-9]\d{0,14}', hit['id']):
            hits_by_id.setdefault(hit['id'], hit)
    if not hits_by_id:
        return []
    entities = _entities(fetch(_WIKIDATA + urlencode({'action': 'wbgetentities',
        'ids': '|'.join(hits_by_id), 'props': 'claims|sitelinks|labels|descriptions',
        'languages': languages, 'format': 'json'}), 'wikidata'))
    selected = []
    for identifier, hit in hits_by_id.items():
        entity = entities.get(identifier)
        entity = entity if isinstance(entity, dict) else {}
        brief = _localized(entity, 'descriptions', language) or _text(hit.get('description'), 12000)
        classification = _localized(entity, 'descriptions', 'en') or brief
        title = _localized(entity, 'labels', language) or _text(hit.get('label'))
        typed_hit = {'label': title, 'description': classification}
        if not title or not (is_movie_result(typed_hit, entity) if kind == 'movie' else _is_tv(typed_hit, entity)):
            continue
        creators = _ids(entity, 'P57' if kind == 'movie' else 'P170', 3)
        if kind == 'tv' and not creators:
            creators = _ids(entity, 'P57', 3)
        selected.append({'id': identifier, 'entity': entity, 'title': title, 'brief': brief,
                         'creators': creators, 'genres': _ids(entity, 'P136', 8)})
    if not selected:
        return []
    relation_ids = list(dict.fromkeys(identifier for row in selected for identifier in row['creators'] + row['genres']))[:50]
    labels, requests_left = {}, 2
    if relation_ids:
        requests_left -= 1
        try:
            labels = _entities(fetch(_WIKIDATA + urlencode({'action': 'wbgetentities', 'ids': '|'.join(relation_ids),
                               'props': 'labels', 'languages': languages, 'format': 'json'}), 'wikidata'))
        except _OPTIONAL_ERRORS:
            pass
    # Prefer the queried script's encyclopedia. If a second language batch
    # fits the four-request budget, fill records with English-only sitelinks.
    wiki_languages = [language] if language == 'en' else [language, 'en']
    wiki_results = {}
    for wiki_language in wiki_languages:
        candidates = [(row['id'], _wiki_title(row['entity'], wiki_language)) for row in selected
                      if row['id'] not in wiki_results and _wiki_title(row['entity'], wiki_language)]
        if not candidates or requests_left <= 0:
            continue
        requests_left -= 1
        try:
            pages = _wiki_pages(fetch('https://' + wiki_language + '.wikipedia.org/w/api.php?' + urlencode({
                'action': 'query', 'format': 'json', 'titles': '|'.join(dict.fromkeys(title for _, title in candidates)),
                'redirects': 1, 'prop': 'pageimages|extracts', 'exintro': 1, 'explaintext': 1, 'exsentences': 5,
                'piprop': 'thumbnail', 'pithumbsize': 500, 'pilicense': 'any'}), 'wikipedia'))
            for identifier, title in candidates:
                if title in pages:
                    wiki_results[identifier] = pages[title]
        except _OPTIONAL_ERRORS:
            pass
    items = []
    for row in selected:
        entity = row['entity']
        page = wiki_results.get(row['id'], {})
        thumbnail = page.get('thumbnail')
        image = _image(thumbnail.get('source')) if isinstance(thumbnail, dict) else ''
        if not image:
            file_name = next((_text(value, 300) for value in _claims(entity, 'P18') if _text(value, 300)), '')
            if file_name and not re.search(r'[\x00-\x1f\x7f]', file_name):
                image = 'https://commons.wikimedia.org/wiki/Special:FilePath/' + quote(file_name, safe='') + '?width=500'
        creators = [_localized(labels.get(identifier), 'labels', language) for identifier in row['creators']]
        creator = ', '.join(value for value in creators if value)
        if not creator:
            match = re.search(r'\b(?:directed by|film by|created by)\s+(.+)$', row['brief'], re.I)
            creator = match.group(1)[:500] if match else ''
        genres = []
        for identifier in row['genres']:
            label = _localized(labels.get(identifier), 'labels', language)
            if label:
                label = label.removesuffix(' television series').removesuffix(' film')
                label = label.title()
                if label not in genres:
                    genres.append(label)
        imdb_id = next((value for value in _claims(entity, 'P345') if isinstance(value, str) and re.fullmatch(r'tt\d{5,12}', value)), '')
        items.append({'title': row['title'], 'creator': creator, 'year': _year(entity, kind, row['brief']),
                      'type': kind, 'genres': genres, 'imdbId': imdb_id, 'image': image,
                      'description': html.unescape(_text(page.get('extract'), 12000)) or row['brief'],
                      'source': WIKIDATA_SOURCE, 'sourceUrl': 'https://www.wikidata.org/wiki/' + row['id'],
                      'externalId': row['id']})
    return items
