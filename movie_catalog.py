"""Reject catalog entities that mention films but are not individual films.

Public catalog reconciliation marks mistakes reversibly; it never deletes rows
or touches a person's library, ratings, notes, or imports.
"""
import json
import re
import unicodedata


# Verified Wikidata classes: Q11424 film; Q24869 feature film; Q24862 short
# film; Q202866 animated film; Q506240 television film; Q93204 documentary;
# Q20650540 anime film; Q29168811 animated feature film. Their item pages are
# https://www.wikidata.org/wiki/{id}; no subclass/network traversal is needed.
FILM_CLASSES = frozenset({'Q11424', 'Q24869', 'Q24862', 'Q202866', 'Q506240',
                          'Q93204', 'Q20650540', 'Q29168811'})
# Verified Wikidata Q5=human, Q43229=organization. These are not creative works,
# even when a person's biography mentions films or television series.
NON_WORK_CLASSES = frozenset({'Q5', 'Q43229'})
EXCLUDED_REASON = 'Not an individual film'
WIKIDATA_SOURCE = 'Wikidata / Wikipedia'
_FILM = re.compile(r'\b(?:films?|movies?|motion pictures?|telefilms?)\b', re.I)
_NON_FILM = re.compile(
    r'\b(?:video[\s-]+games?|albums?|soundtracks?|books?|novels?|songs?|comics?|manga|'
    r'(?:tv|television|web|radio)[\s-]+(?:series|episodes?|programs?|shows?|miniseries)|'
    r'episodes?|miniseries|media franchises?|franchises?|'
    r'(?:film|movie)[\s-]+(?:series|franchises?|soundtracks?|scores?|music|festivals?|'
    r'awards?|studios?|schools?|directors?|producers?|actors?|actresses?|scripts?)|'
    r'series\s+of(?:\s+\w+){0,4}\s+films?)\b', re.I)
_RELATION = re.compile(r'\b(?:based on|adapted from|adaptation of|about|inspired by|'
                       r'featuring|depicts|tells|follows)\b', re.I)
_SCREEN_WORK = re.compile(r'\b(?:films?|movies?|motion pictures?|telefilms?|'
                          r'(?:tv|television|web|streaming)(?:[\s-]+[\w-]+){0,4}[\s-]+(?:series|shows?|programs?)|'
                          r'miniseries|sitcom|docuseries|telenovela|series)\b|فيلم|مسلسل', re.I)
_NON_WORK = re.compile(r'\b(?:(?:film|movie)[\s-]+production[\s-]+(?:studios?|companies|company)|'
                       r'(?:tv|television|web|streaming)[\s-]+(?:series|shows?|programs?)[\s-]+(?:franchises?|characters?)|'
                       r'(?:film|movie|tv|television)[\s-]+(?:actors?|actresses?|directors?|writers?|'
                       r'producers?|critics?|studios?|networks?|channels?)|'
                       r'actors?|actresses?|directors?|writers?|novelists?|screenwriters?|musicians?|singers?|'
                       r'composers?|producers?|filmmakers?|cinematographers?|animators?|critics?|journalists?|'
                       r'presenters?|artists?|people|persons?|companies|company|organizations?|organisations?|'
                       r'studios?|networks?|channels?|festivals?|awards?|franchises?|characters?)\b|'
                       r'ممثل(?:ة)?|مخرج(?:ة)?|كاتب(?:ة)?|موسيقار|مغني|شركة|استوديو|مؤسسة|مذيع', re.I)


def _typing_text(description, title=''):
    if not isinstance(description, str):
        return ''
    text = unicodedata.normalize('NFKC', description).strip()[:2000]
    # Wikipedia extracts repeat titles before "is a ...". A film named The
    # Game/The Album/The Soundtrack must not be classified by its proper noun.
    if isinstance(title, str) and title.strip():
        title = unicodedata.normalize('NFKC', title).strip()
        text = re.sub(r'^\s*["\'“‘]?' + re.escape(title) + r'["\'”’]?\s*(?=\W|$)', '', text, count=1, flags=re.I)
    copula = re.search(r'\b(?:is|was|are|were)\s+', text, re.I)
    if copula:
        first_media = min((match.start() for pattern in (_FILM, _NON_FILM)
                           if (match := pattern.search(text))), default=len(text))
        if copula.start() < first_media:
            text = text[copula.end():]
    return text


def _known_non_film(description, title=''):
    if is_non_work_description(description, title):
        return True
    text = _typing_text(description, title)
    film, other = _FILM.search(text), _NON_FILM.search(text)
    if not other or (film and film.start() < other.start()):
        return False
    # "Screen adaptation of a novel" is not a statement that the item itself
    # is a novel. With uncertain old metadata, keep the row visible.
    if _RELATION.search(text[:other.start()]):
        return False
    return True


def is_non_work_description(description, title=''):
    """Reject a person/organization type before the work, not its subject later."""
    text = _typing_text(description, title)
    work, non_work = _SCREEN_WORK.search(text), _NON_WORK.search(text)
    return bool(non_work and (not work or non_work.start() <= work.start())
                and not _RELATION.search(text[:non_work.start()]))


def _instance_classes(entity):
    if not isinstance(entity, dict) or not isinstance(entity.get('claims'), dict):
        return set()
    classes = set()
    claims = entity['claims'].get('P31')
    for claim in claims[:100] if isinstance(claims, list) else []:
        if not isinstance(claim, dict) or claim.get('rank') == 'deprecated':
            continue
        snak = claim.get('mainsnak')
        value = snak.get('datavalue') if isinstance(snak, dict) else None
        value = value.get('value') if isinstance(value, dict) else None
        identifier = value.get('id') if isinstance(value, dict) else None
        if isinstance(identifier, str):
            classes.add(identifier)
    return classes


def is_movie_result(hit, entity=None):
    """Accept a new search hit only when its description or P31 identifies film."""
    if not isinstance(hit, dict):
        return False
    classes = _instance_classes(entity)
    if classes & NON_WORK_CLASSES:
        return False
    description, title = hit.get('description'), hit.get('label', '')
    if _known_non_film(description, title):
        return False
    return bool(_FILM.search(_typing_text(description, title))) or bool(classes & FILM_CLASSES)


def is_movie_metadata(item):
    """Keep uncertain stored metadata; exclude only obvious Wikidata mistakes."""
    if not isinstance(item, dict) or item.get('type') != 'movie' or item.get('source') != WIKIDATA_SOURCE:
        return True
    return not _known_non_film(item.get('description'), item.get('title', ''))


def reconcile_movie_catalog(catalog):
    """Flag/restore public rows atomically; return counts, never row contents.

    excluded/restored count newly changed markers, so a second unchanged run
    reports zero changes. Unknown exclusion reasons belong to their owner.
    """
    counts = {'checked': 0, 'excluded': 0, 'restored': 0}
    with catalog.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT item_key,payload FROM catalog WHERE kind='movie' "
                          "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.source') END=?",
                          (WIKIDATA_SOURCE,)).fetchall()
        for row in rows:
            item = json.loads(row['payload'])
            if not isinstance(item, dict):
                continue
            counts['checked'] += 1
            marker = item.get('catalogExcludedReason')
            eligible = is_movie_metadata(item)
            if not eligible and not marker:
                item['catalogExcludedReason'] = EXCLUDED_REASON
                counts['excluded'] += 1
            elif eligible and marker == EXCLUDED_REASON:
                item.pop('catalogExcludedReason')
                counts['restored'] += 1
            else:
                continue
            db.execute('UPDATE catalog SET payload=? WHERE item_key=?',
                       (json.dumps(item, ensure_ascii=False), row['item_key']))
    return counts
