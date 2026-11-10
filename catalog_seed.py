"""Populate Trace's public browse catalog from a fixed, bounded starter plan.

No private library is read. The default command is a dry run. --apply uses the
existing attributed metadata adapters and their persistent query cache.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import json
import os
from pathlib import Path
from catalog_store import VISIBLE_CATALOG


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
COLLECTIONS = {'international': INTERNATIONAL_QUERIES, 'classic': STARTER_QUERIES}


def seed_plan(kind=None, limit_queries=40, collection='international'):
    if collection not in COLLECTIONS:
        raise ValueError('Choose the international or classic starter collection.')
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
            result = search(media_kind, text)
            if not isinstance(result, dict) or not isinstance(result.get('items'), list):
                raise ValueError('Invalid public catalog result.')
            saved = catalog.save(media_kind, text, result)
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
    parser.add_argument('--limit-queries', type=int, default=40)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--report', help='Write a new public aggregate .json report.')
    args = parser.parse_args(argv)
    try:
        plan = seed_plan(args.kind, args.limit_queries, args.collection)
        if not 1 <= args.workers <= 3:
            raise ValueError('Use between one and three seed workers.')
        if args.report:
            report_target(args.report)
        if not args.apply:
            report = {'dryRun': True, 'collection': args.collection, 'plannedQueries': len(plan),
                      'queries': [{'number': number, 'kind': kind, 'query': query}
                                  for number, (kind, query) in enumerate(plan, 1)]}
        else:
            backend = load_seed_server()
            def progress(event):
                print(json.dumps({'progress': event}, ensure_ascii=False), flush=True)
            report = run_seed(backend.search, backend.get_catalog(), kind=args.kind,
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
