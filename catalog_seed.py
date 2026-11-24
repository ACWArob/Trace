"""Populate Trace's public browse catalog from a fixed, bounded starter plan.

No private library is read. The default command is a dry run. --apply uses the
existing attributed metadata adapters and their persistent query cache.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import json
import os
from pathlib import Path
from urllib.error import URLError
from catalog_store import VISIBLE_CATALOG, catalog_search_text
from screen_catalog import tvmaze_index


STARTER_QUERIES = {
    'movie': ('Interstellar', 'The Matrix', 'Star Wars', 'Godzilla', 'Batman',
              'Love', 'Moonlight', 'Perfect Days', 'Past Lives', 'Spirited Away',
              'Inception', 'Parasite'),
    'tv': ('The Office', 'Star Trek', 'Doctor Who', 'Severance', 'Planet Earth',
           'Breaking Bad'),
    'anime': ('Naruto', 'One Piece', 'Frieren', 'Attack on Titan', 'Cowboy Bebop',
              'Neon Genesis Evangelion'),
    'book': ('subject:fantasy', 'subject:science_fiction', 'subject:mystery',
             'subject:romance', 'subject:memoir', 'subject:poetry',
             'author:Ursula K. Le Guin', 'author:Haruki Murakami'),
    'album': ('Frank Ocean', 'Radiohead', 'Kendrick Lamar', 'Fleetwood Mac',
              'Björk', 'Nina Simone', 'Daft Punk', 'The Beatles'),
}


INTERNATIONAL_QUERIES = {
    'movie': ('Chronicle of the Years of Fire', 'The Battle of Algiers',
              'Omar Gatlato', 'Papicha', 'Zabana!', 'In the Mood for Love',
              'Pather Panchali', 'A Separation', 'The Cloud-Capped Star',
              'Touki Bouki', 'City of God', 'Capernaum'),
    'tv': ('Djemai Family', 'Dar Lefchouch', 'من عاش للموت', 'My Mister',
           'Sacred Games', 'La casa de papel'),
    'anime': ('Heike Monogatari', 'Chihayafuru', 'Natsume Yuujinchou',
              'Shouwa Genroku Rakugo Shinjuu', 'Ousama Ranking', 'Mushishi'),
    'book': ('Nedjma Kateb Yacine', 'The Meursault Investigation Kamel Daoud',
             'Assia Djebar', 'ذاكرة الجسد', 'Ngũgĩ wa Thiong’o',
             'Mahasweta Devi', 'Clarice Lispector', 'Han Kang'),
    'album': ('Souad Massi', 'Rachid Taha', 'Khaled', 'Cheb Mami',
              'Oumou Sangaré', 'Arooj Aftab', 'Nusrat Fateh Ali Khan',
              'Ryuichi Sakamoto'),
}
EXPANDED_BOOK_AUTHORS = {
    'The Yacoubian Building': 'Aswany',
    'Dreams of Trespass': 'Mernissi',
    'The Queue': 'Basma Abdel Aziz',
    'The Memory Police': 'Ogawa',
    'The Mountains Sing': 'Nguyen',
    'The Shadow of the Wind': 'Zafon',
    'The Book of Disquiet': 'Pessoa',
    'The Map of Salt and Stars': 'Joukhadar',
}
EXPANDED_MOVIE_YEARS = {
    'Atlantics': '2019', 'Theeb': '2014', 'Wadjda': '2012',
    'Timbuktu': '2014', 'The Lunchbox': '2013', 'Flee': '2021',
}
EXPANDED_QUERIES = {
    'movie': tuple(EXPANDED_MOVIE_YEARS),
    'tv': ('Pachinko', 'AlRawabi School for Girls', 'Midnight Diner'),
    'anime': ('Ping Pong the Animation', 'A Place Further Than the Universe'),
    'book': tuple(EXPANDED_BOOK_AUTHORS),
    'album': ('Tinariwen', 'Cesária Évora', 'Youssou N’Dour', 'A-WA',
              'Lhasa de Sela', 'Fela Kuti', 'Natacha Atlas', 'Amália Rodrigues'),
}
TV_BOOK_QUERIES = {
    'tv': ('أولاد الحلال', 'عاشور العاشر', 'بنت البلاد', 'نسيبتي العزيزة',
           'نوبة', 'موضوع عائلي', 'Finding Ola', 'Paranormal', 'The Exchange',
           'Jinn', 'Delhi Crime', 'Made in Heaven', 'Extraordinary Attorney Woo',
           'Move to Heaven', 'The Makanai: Cooking for the Maiko House',
           'The Victims’ Game', 'El marginal', 'Invisible City', 'Blood & Water',
           'Queen Sono'),
    'book': ('Assia Djebar', 'Kateb Yacine', 'Mohammed Dib', 'Mouloud Feraoun',
             'Tahar Djaout', 'Ahlam Mosteghanemi', 'Leïla Slimani', 'Naguib Mahfouz',
             'al-Ṭayyib Ṣāliḥ', 'Nawal El Saadawi', 'Chimamanda Ngozi Adichie',
             'Abdulrazak Gurnah', 'Yaa Gyasi', 'Isabel Allende', 'Samanta Schweblin',
             'Jorge Luis Borges', 'Yoko Ogawa', '吉本 ばなな',
             'Pramoedya Ananta Toer', 'Viet Thanh Nguyen'),
}
COLLECTIONS = {'international': INTERNATIONAL_QUERIES, 'classic': STARTER_QUERIES,
               'expanded': EXPANDED_QUERIES, 'tv-books': TV_BOOK_QUERIES}


def expanded_match(kind, query, row):
    if not isinstance(row, dict) or row.get('type') != kind:
        return False
    target = catalog_search_text(query)
    title = catalog_search_text(str(row.get('title') or ''))
    creator = catalog_search_text(str(row.get('creator') or ''))
    if kind == 'album':
        return creator.replace(' ', '') == target.replace(' ', '')
    if kind == 'book':
        author = catalog_search_text(EXPANDED_BOOK_AUTHORS[query]).replace(' ', '')
        return (title == target or title.startswith(target + ' ')) and author in creator.replace(' ', '')
    if kind == 'movie':
        return title == target and str(row.get('year')) == EXPANDED_MOVIE_YEARS[query]
    return title == target or (kind == 'tv' and query == 'Midnight Diner' and title == 'midnight diner tokyo stories')


def tv_book_match(kind, query, row):
    if not isinstance(row, dict) or row.get('type') != kind:
        return False
    target = catalog_search_text(query)
    if kind == 'tv':
        return catalog_search_text(str(row.get('title') or '')) == target
    authors = str(row.get('creator') or '').split(',')
    return bool(row.get('image')) and any(catalog_search_text(author) == target for author in authors)


def seed_plan(kind=None, limit_queries=40, collection='international'):
    if collection not in COLLECTIONS:
        raise ValueError('Choose the international, classic, expanded, or tv-books collection.')
    selected = COLLECTIONS[collection]
    if kind is not None and kind not in selected:
        raise ValueError('Choose a supported catalog category.')
    if type(limit_queries) is not int or not 1 <= limit_queries <= 40:
        raise ValueError('The seed query limit must be between 1 and 40.')
    kinds = [kind] if kind else list(selected)
    # A short smoke run still covers every category, rather than only movies.
    plan = [(media_kind, queries[index])
            for index in range(max(len(selected[value]) for value in kinds))
            for media_kind in kinds
            if index < len(queries := selected[media_kind])]
    return plan[:limit_queries]


def catalog_coverage(catalog):
    """Read aggregate metadata coverage from the public catalog table only."""
    with catalog.connect() as db:
        rows = db.execute('''SELECT kind,count(*) AS total,
            sum(CASE WHEN trim(coalesce(json_extract(payload,'$.image'),'')) != '' THEN 1 ELSE 0 END) AS covers,
            sum(CASE WHEN json_array_length(genres) > 0 THEN 1 ELSE 0 END) AS genres,
            sum(CASE WHEN trim(coalesce(json_extract(payload,'$.description'),'')) != '' THEN 1 ELSE 0 END) AS descriptions
            FROM catalog WHERE ''' + VISIBLE_CATALOG + ' GROUP BY kind').fetchall()
    coverage = {kind: {'total': 0, 'withCover': 0, 'withGenres': 0,
                       'withDescription': 0, 'missingCover': 0,
                       'missingGenres': 0, 'missingDescription': 0}
                for kind in STARTER_QUERIES}
    for row in rows:
        kind, total, covers, genres, descriptions = row
        if kind not in coverage:
            continue
        coverage[kind] = {'total': total, 'withCover': covers or 0,
                          'withGenres': genres or 0,
                          'withDescription': descriptions or 0,
                          'missingCover': total - (covers or 0),
                          'missingGenres': total - (genres or 0),
                          'missingDescription': total - (descriptions or 0)}
    return {'total': sum(row['total'] for row in coverage.values()),
            'categories': coverage}


def run_seed(search, catalog, *, kind=None, limit_queries=40, workers=3,
             progress=None, collection='international'):
    if type(workers) is not int or not 1 <= workers <= 3:
        raise ValueError('Use between one and three seed workers.')
    plan = seed_plan(kind, limit_queries, collection)
    before = catalog_coverage(catalog)

    def query(number, media_kind, text):
        event = {'number': number, 'kind': media_kind, 'query': text}
        try:
            # The real search function handles cache freshness/versioning. Do
            # not bypass it by trusting an older cached provider payload here.
            key = text
            if collection == 'tv-books':
                key = f'trace-seed:tv-books:{text}'
                saved = catalog.cached(media_kind, key)
                if saved:
                    event.update(status='cached', items=len(saved['items']),
                                 warningCount=len(saved.get('warnings') or []))
                    return event
            query_text = f'author:"{text}"' if collection == 'tv-books' and media_kind == 'book' else text
            result = search(media_kind, query_text)
            if not isinstance(result, dict) or not isinstance(result.get('items'), list):
                raise ValueError('Invalid public catalog result.')
            if collection == 'expanded':
                result = {**result, 'items': [row for row in result['items'] if expanded_match(media_kind, text, row)]}
            if collection == 'tv-books':
                result = {**result, 'items': [row for row in result['items'] if tv_book_match(media_kind, text, row)]}
            saved = catalog.save(media_kind, key, result)
            event.update(status='cached' if result.get('cached') else 'saved',
                         items=len(saved['items']),
                         warningCount=len(result.get('warnings') or []))
        except Exception as error:
            # Raw exceptions can contain URLs, credentials, or provider bodies.
            event.update(status='failed', items=0, errorType=type(error).__name__)
        return event

    events = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(query, index, media_kind, text)
                   for index, (media_kind, text) in enumerate(plan, 1)]
        for future in as_completed(pending):
            event = future.result()
            events.append(event)
            if progress:
                progress({**event, 'completed': len(events), 'planned': len(plan)})
    after = catalog_coverage(catalog)
    failed = sum(event['status'] == 'failed' for event in events)
    return {'dryRun': False, 'collection': collection, 'plannedQueries': len(plan),
            'successfulQueries': len(plan) - failed, 'failedQueries': failed,
            'cachedQueries': sum(event['status'] == 'cached' for event in events),
            'before': before, 'after': after,
            'addedTitles': after['total'] - before['total'],
            'queries': sorted(events, key=lambda event: event['number'])}


def run_tv_page(page, fetch, catalog):
    before = catalog_coverage(catalog)
    result = tvmaze_index(page, fetch)
    saved = catalog.save('tv', None, result)
    after = catalog_coverage(catalog)
    return {'dryRun': False, 'provider': 'TVmaze', 'tvPage': page,
            'items': len(saved['items']), 'before': before, 'after': after,
            'addedTitles': after['total'] - before['total']}


def load_seed_server():
    # This CLI owns its process environment. Never load the private local key
    # file, and do not enable credentialed score providers during population.
    os.environ.pop('OMDB_API_KEY', None)
    os.environ.pop('MDBLIST_API_KEY', None)
    import server
    return server


def report_target(path):
    target = Path(path)
    if target.suffix.lower() != '.json':
        raise ValueError('Choose a .json path for the public aggregate report.')
    if target.exists() or target.is_symlink():
        raise ValueError('The report path already exists; choose a new path.')
    if not target.parent.is_dir():
        raise ValueError('The report directory must already exist.')
    return target


def write_report(path, report):
    target = report_target(path)
    # Never replace an existing report or another user file implicitly.
    with target.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Populate the existing public catalog.')
    parser.add_argument('--kind', choices=tuple(STARTER_QUERIES))
    parser.add_argument('--collection', choices=tuple(COLLECTIONS), default='international',
                        help='Choose a fixed discovery plan; international is the default.')
    parser.add_argument('--tv-page', type=int, help='Use one page of the official TVmaze show index.')
    parser.add_argument('--limit-queries', type=int, default=40)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--report', help='Write a new public aggregate .json report.')
    args = parser.parse_args(argv)
    try:
        if args.tv_page is not None:
            if not 0 <= args.tv_page <= 10000:
                raise ValueError('Choose a TVmaze index page from 0 to 10000.')
            if args.kind or args.collection != 'international' or args.limit_queries != 40:
                raise ValueError('--tv-page is a separate operation from the curated collections.')
        else:
            plan = seed_plan(args.kind, args.limit_queries, args.collection)
        if not 1 <= args.workers <= 3:
            raise ValueError('Use between one and three seed workers.')
        if args.report:
            report_target(args.report)
        if args.tv_page is not None:
            if args.apply:
                backend = load_seed_server()
                try:
                    report = run_tv_page(args.tv_page, backend.fetch, backend.get_catalog())
                except (URLError, TimeoutError) as error:
                    raise ValueError('TVmaze did not return that page. Check the page number or try again later.') from error
            else:
                report = {'dryRun': True, 'provider': 'TVmaze', 'tvPage': args.tv_page}
        elif not args.apply:
            report = {'dryRun': True, 'collection': args.collection, 'plannedQueries': len(plan),
                      'queries': [{'number': number, 'kind': kind, 'query': query}
                                  for number, (kind, query) in enumerate(plan, 1)]}
        else:
            backend = load_seed_server()
            def progress(event):
                print(json.dumps({'progress': event}, ensure_ascii=False), flush=True)
            report = run_seed(lambda kind, text: backend.search(kind, text, persist=False),
                              backend.get_catalog(), kind=args.kind,
                              limit_queries=args.limit_queries, workers=args.workers,
                              progress=progress, collection=args.collection)
        if args.report:
            write_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get('failedQueries') else 0
    except (ValueError, OSError) as error:
        # These errors concern local CLI parameters, not provider requests.
        print(f'Catalog population: {error}', file=__import__('sys').stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
