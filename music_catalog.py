"""Bounded album-only search across Apple's store and MusicBrainz release groups."""
import re
import unicodedata
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlparse
from uuid import UUID


CATALOG_VERSION = 4
PROVIDER = 'Apple Music + MusicBrainz'
_NON_ALBUM_SUFFIX = re.compile(r'(?:\s[-–—]\s(?:single|ep)|\((?:single|ep)\))\s*$', re.I)


def _text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _https(value, hosts=None):
    value = _text(value, 2000)
    try:
        parsed = urlparse(value)
        parsed.port  # Reject invalid port syntax rather than retaining a broken URL.
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or re.search(r'[\x00-\x20\x7f]', value)
                or (hosts and parsed.hostname.lower() not in hosts)):
            return ''
    except ValueError:
        return ''
    return value


def _year(value):
    value = _text(value)
    return value[:4] if re.match(r'^\d{4}(?:-|$)', value) else ''


def _description(title, artist, year):
    return f'{title} is an album by {artist or "an unknown artist"}.' + (f' Released in {year}.' if year else '')


def _apple_items(data):
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise ValueError('Invalid Apple album response.')
    items = []
    for row in data['results'][:30]:
        if not isinstance(row, dict):
            continue
        title = _text(row.get('collectionName'))
        identifier = str(row.get('collectionId', ''))
        if (row.get('collectionType') != 'Album' or row.get('wrapperType') == 'track'
                or not title or _NON_ALBUM_SUFFIX.search(title)
                or not re.fullmatch(r'[1-9]\d{0,19}', identifier)):
            continue
        artist, year = _text(row.get('artistName')), _year(row.get('releaseDate'))
        url = _https(row.get('collectionViewUrl'), {'music.apple.com', 'itunes.apple.com'})
        genre = _text(row.get('primaryGenreName'), 80)
        items.append({'title': title, 'creator': artist, 'year': year, 'type': 'album',
                      'image': _https(row.get('artworkUrl100')).replace('100x100bb', '600x600bb'),
                      'source': 'Apple Music', 'sourceUrl': url or 'https://music.apple.com/us/album/' + identifier,
                      'externalId': identifier, 'genres': [genre] if genre else [],
                      'description': _description(title, artist, year)})
    return items


ALBUM_GENRES = ('Alternative', 'Rock', 'Pop', 'Hip-Hop', 'R&B', 'Soul', 'Jazz', 'Blues',
                'Electronic', 'Dance', 'Ambient', 'Experimental', 'Folk', 'Country',
                'Classical', 'Metal', 'Punk', 'Reggae', 'Latin', 'Afrobeat', 'Afrobeats',
                'Highlife', 'Soukous', 'Raï', 'Chaâbi', 'Gnawa', 'Desert Blues', 'Arabic Pop')
_TAG_NOISE = re.compile(r'^(?:offizielle charts|seen live|favou?rites?|owned|my collection|'
                        r'\d+\s*[-–]\s*\d+\s*(?:wochen|weeks)|ph_.*stars?|\d{4}s?)$', re.I)


def clean_album_genres(values, *, limit=20):
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value.strip()[:80] for value in values[:max(40, limit)]
                              if isinstance(value, str) and value.strip() and not _TAG_NOISE.fullmatch(value.strip())))[:limit]


def album_genre_tag(genre):
    name = ''.join(c for c in unicodedata.normalize('NFKD', genre.casefold()) if not unicodedata.combining(c))
    return {'hip-hop': 'hip hop', 'r&b': 'rhythm and blues', 'rnb': 'rhythm and blues'}.get(name, name)


def album_genre_matches(genres, genre):
    desired = _identity(album_genre_tag(genre)).replace(' ', '')
    return any(_identity(album_genre_tag(g)).replace(' ', '') == desired for g in genres)


def album_details(identifier, fetch):
    try:
        if str(UUID(identifier)) != identifier:
            raise ValueError('Invalid album ID.')
    except (ValueError, AttributeError, TypeError):
        raise ValueError('Invalid album ID.') from None
    data = fetch('https://musicbrainz.org/ws/2/release-group/' + identifier + '?' + urlencode({
        'inc': 'artist-credits+genres+ratings', 'fmt': 'json'}), 'musicbrainz')
    if not isinstance(data, dict) or data.get('id') != identifier or data.get('primary-type') != 'Album':
        raise ValueError('Album details did not match the requested release group.')
    items = _musicbrainz_items({'release-groups': [data]})
    if not items:
        raise ValueError('Album details were incomplete.')
    item = items[0]
    genres = data.get('genres')
    item['genres'] = clean_album_genres([g.get('name') for g in genres[:40] if isinstance(g, dict)]) if isinstance(genres, list) else []
    rating = data.get('rating')
    rating = rating if isinstance(rating, dict) else {}
    value, count = rating.get('value'), rating.get('votes-count')
    try:
        if isinstance(value, bool):
            raise ValueError('Invalid album rating.')
        value = float(value)
    except (ValueError, TypeError):
        value = -1
    item['publicScores'] = [{'source': 'MusicBrainz', 'value': value, 'max': 5,
                             'count': count if type(count) is int and count > 0 else None,
                             'url': item['sourceUrl']}] if 0 <= value <= 5 else []
    return item


def match_album(item, fetch):
    title, artist = _text(item.get('title')), _text(item.get('creator'))
    if not title or not artist:
        return None
    query = 'primarytype:album AND releasegroup:' + _literal_query(title) + ' AND artist:' + _literal_query(artist)
    data = fetch('https://musicbrainz.org/ws/2/release-group/?' + urlencode({
        'query': query, 'fmt': 'json', 'limit': 5}), 'musicbrainz')
    matches = [row for row in _musicbrainz_items(data)
               if _identity(row['title']) == _identity(title) and _identity(row['creator']) == _identity(artist)]
    same_year = [row for row in matches if row['year'] == str(item.get('year') or '')]
    candidates = same_year or matches
    return candidates[0] if len(candidates) == 1 else None


def _musicbrainz_items(data):
    if not isinstance(data, dict) or not isinstance(data.get('release-groups'), list):
        raise ValueError('Invalid MusicBrainz album response.')
    items = []
    for row in data['release-groups'][:30]:
        if not isinstance(row, dict) or row.get('primary-type') != 'Album':
            continue
        title, identifier = _text(row.get('title')), _text(row.get('id'))
        try:
            if str(UUID(identifier)) != identifier.lower():
                continue
        except (ValueError, AttributeError):
            continue
        identifier = identifier.lower()
        if not title or _NON_ALBUM_SUFFIX.search(title):
            continue
        credits = row.get('artist-credit')
        artist = ''.join(_text(credit.get('name'), 200) +
                         (credit.get('joinphrase') if isinstance(credit.get('joinphrase'), str) else '')
                         for credit in (credits[:20] if isinstance(credits, list) else [])
                         if isinstance(credit, dict))[:500].strip()
        year = _year(row.get('first-release-date'))
        tags = row.get('tags')
        genres = []
        for tag in tags[:40] if isinstance(tags, list) else []:
            name = _text(tag.get('name'), 80) if isinstance(tag, dict) else ''
            if name and name.casefold() not in {genre.casefold() for genre in genres}:
                genres.append(name)
            if len(genres) == 8:
                break
        items.append({'title': title, 'creator': artist, 'year': year, 'type': 'album',
                      'image': 'https://coverartarchive.org/release-group/' + identifier + '/front-500',
                      'source': 'MusicBrainz', 'sourceUrl': 'https://musicbrainz.org/release-group/' + identifier,
                      'externalId': identifier, 'genres': clean_album_genres(genres),
                      'description': _description(title, artist, year)})
    return items


def musicbrainz_query(query):
    # Quote each word separately: terms can match artist OR album fields, while
    # punctuation, field syntax, boolean operators and wildcards stay literal.
    return _literal_query(query) + ' AND primarytype:album'


def _literal_query(query):
    words = ['"' + word.replace('\\', '\\\\').replace('"', '\\"') + '"' for word in query.split()]
    return '(' + ' '.join(words) + ')'


def _identity(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold()))


def _needs_artist_alias(query, items):
    if not any(character.isalpha() and 'LATIN' not in unicodedata.name(character, '') for character in query):
        return False
    terms = set(_identity(query).split())
    return bool(terms) and not any(terms <= set(_identity(item['creator']).split()) for item in items)


def _artist_match(data, query):
    if not isinstance(data, dict) or not isinstance(data.get('artists'), list):
        raise ValueError('Invalid MusicBrainz artist response.')
    wanted, terms, candidates = _identity(query), set(_identity(query).split()), []
    for row in data['artists'][:3]:
        if not isinstance(row, dict):
            continue
        identifier, name = _text(row.get('id')), _text(row.get('name'))
        try:
            if str(UUID(identifier)) != identifier.lower():
                continue
        except (ValueError, AttributeError):
            continue
        if not name:
            continue
        aliases = row.get('aliases')
        names = [name] + [_text(alias.get('name')) for alias in (aliases[:50] if isinstance(aliases, list) else [])
                          if isinstance(alias, dict)]
        identities = [_identity(value) for value in names if value]
        try:
            score = max(0, min(100, int(row.get('score', 0))))
        except (ValueError, TypeError):
            score = 0
        exact = wanted in identities
        full = len(terms) >= 2 and score >= 95 and any(terms <= set(value.split()) for value in identities)
        if exact or full:
            candidates.append((2 if exact else 1, score, identifier.lower(), name))
    candidates.sort(reverse=True)
    if not candidates or len(candidates) > 1 and candidates[0][:2] == candidates[1][:2] and candidates[0][2] != candidates[1][2]:
        return None
    return {'id': candidates[0][2], 'name': candidates[0][3]}


def _artist_albums(data, artist):
    if not isinstance(data, dict) or not isinstance(data.get('release-groups'), list):
        raise ValueError('Invalid MusicBrainz artist albums response.')
    rows = []
    for row in data['release-groups'][:30]:
        if not isinstance(row, dict):
            continue
        credits = row.get('artist-credit')
        credits = [credit for credit in (credits[:20] if isinstance(credits, list) else []) if isinstance(credit, dict)]
        identifiers = [_text(credit['artist'].get('id')).lower() for credit in credits if isinstance(credit.get('artist'), dict)
                       and _text(credit['artist'].get('id'))]
        names = [_text(credit.get('name')) for credit in credits]
        if artist['id'] in identifiers or not identifiers and _identity(artist['name']) in {_identity(name) for name in names}:
            rows.append(row)
    return _musicbrainz_items({'release-groups': rows})


def _merge(musicbrainz, apple, query, preferred_releases=()):
    items, positions = [], {}
    for item in musicbrainz + apple:
        key = (_identity(item['title']), _identity(item['creator']), item['year'])
        source = {name: item[name] for name in ('source', 'sourceUrl', 'externalId')}
        if key not in positions:
            positions[key] = len(items)
            items.append({**item, 'catalogSources': [source]})
            continue
        old = items[positions[key]]
        sources = old['catalogSources'] + ([] if source in old['catalogSources'] else [source])
        genres = list(dict.fromkeys(old['genres'] + item['genres']))[:8]
        # Apple's matched collection supplies a direct store link and known
        # artwork; retain MusicBrainz identity/provenance in catalogSources.
        if item['source'] == 'Apple Music':
            items[positions[key]] = {**item, 'image': item['image'] or old['image'],
                                     'genres': genres, 'catalogSources': sources}
        else:
            old['genres'], old['catalogSources'] = genres, sources
    terms = set(_identity(query).split())

    def relevance(item):
        words = set(_identity(item['title'] + ' ' + item['creator']).split())
        matched = len(words & terms)
        preferred = any(source['source'] == 'MusicBrainz' and source['externalId'] in preferred_releases
                        for source in item['catalogSources'])
        return (preferred, matched / max(1, len(terms)), matched / max(1, len(words)))

    # Stable ties preserve MusicBrainz's relevance order. Extra words in an
    # unrelated title do not outrank a precise artist + album query.
    return sorted(items, key=relevance, reverse=True)[:30]


def search_albums(query, fetch, genre=''):
    if genre and (not isinstance(genre, str) or not re.fullmatch(r"[\w &+/'’\-]{2,80}", genre)):
        raise ValueError('Choose a supported album genre.')
    if not isinstance(query, str) or len(query.strip()) > 200 or not genre and len(query.strip()) < 2:
        raise ValueError('Enter 2–200 characters for an album search.')
    query = query.strip()
    mb_query = musicbrainz_query(query) if query else 'primarytype:album'
    if genre:
        mb_query += ' AND tag:"' + album_genre_tag(genre) + '"'
    requests = [
        ('apple', 'Apple Music', 'https://itunes.apple.com/search?' + urlencode({
            'term': query or genre, 'entity': 'album', 'media': 'music', 'limit': 30, 'country': 'US'}, quote_via=quote), _apple_items),
        ('musicbrainz', 'MusicBrainz', 'https://musicbrainz.org/ws/2/release-group/?' + urlencode({
            'query': mb_query, 'fmt': 'json', 'limit': 30}, quote_via=quote), _musicbrainz_items),
    ]
    found, status, warnings = {}, {}, []
    for key, label, url, parse in requests:
        try:
            found[key] = parse(fetch(url, key))
            status[key] = 'ok'
        except (URLError, TimeoutError, ValueError, OSError):
            found[key] = []
            status[key] = 'unavailable'
            warnings.append(label + ' is unavailable. Results from the other album catalog are still shown.')
    if all(value == 'unavailable' for value in status.values()):
        raise URLError('Album catalogs are unavailable right now. Try again or add the album manually.')
    preferred = set()
    if query and not genre and status['musicbrainz'] == 'ok' and _needs_artist_alias(query, found['musicbrainz'] + found['apple']):
        try:
            url = 'https://musicbrainz.org/ws/2/artist/?' + urlencode({
                'query': _literal_query(query), 'fmt': 'json', 'limit': 3}, quote_via=quote)
            artist = _artist_match(fetch(url, 'musicbrainz'), query)
            if artist:
                url = 'https://musicbrainz.org/ws/2/release-group?' + urlencode({
                    'artist': artist['id'], 'type': 'album', 'inc': 'artist-credits+tags',
                    'fmt': 'json', 'limit': 30}, quote_via=quote)
                albums = _artist_albums(fetch(url, 'musicbrainz'), artist)
                preferred = {item['externalId'] for item in albums}
                found['musicbrainz'] = albums + found['musicbrainz']
        except (URLError, TimeoutError, ValueError, OSError):
            warnings.append('Artist-name matching is temporarily unavailable. Existing album results are still shown.')
    items = _merge(found['musicbrainz'], found['apple'], query, preferred)
    if genre:
        items = [item for item in items if album_genre_matches(item['genres'], genre)]
    return {'items': items, 'provider': PROVIDER, 'genres': list(ALBUM_GENRES),
            'providerStatus': status, 'warnings': warnings, 'catalogVersion': CATALOG_VERSION}
