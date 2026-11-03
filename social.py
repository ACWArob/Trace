"""Small server-backed social domain with explicit publishing and private authentication."""
from contextlib import contextmanager
from hashlib import scrypt, sha256
from pathlib import Path
from urllib.parse import urlparse
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from catalog_store import database_path

TYPES = {'movie', 'tv', 'anime', 'book', 'album', 'memory'}


class SocialError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text(value, limit):
    return str(value or '').strip()[:limit]


def image_url(value):
    value = text(value, 600000)
    if re.fullmatch(r'/assets/[\w.-]+', value) or re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+', value):
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        return ''
    return value[:2000] if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else ''


def genres(value):
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            continue
        label = ' '.join(item.split())[:40]
        if label and label.casefold() not in seen:
            seen.add(label.casefold()); out.append(label)
    return out[:8]


class SocialStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL, bio TEXT NOT NULL DEFAULT '',
                    salt BLOB NOT NULL, password_hash BLOB NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS follows (
                    follower TEXT NOT NULL REFERENCES users(id), target TEXT NOT NULL REFERENCES users(id),
                    PRIMARY KEY(follower,target), CHECK(follower<>target)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    item TEXT NOT NULL, body TEXT NOT NULL, rating REAL NOT NULL,
                    spoiler INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, deleted_at REAL
                );
                CREATE INDEX IF NOT EXISTS reviews_created ON reviews(created_at DESC,id DESC);
                CREATE INDEX IF NOT EXISTS reviews_user ON reviews(user_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS likes (
                    review_id TEXT NOT NULL REFERENCES reviews(id), user_id TEXT NOT NULL REFERENCES users(id),
                    PRIMARY KEY(review_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id),
                    user_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL,
                    created_at REAL NOT NULL, deleted_at REAL
                );
                CREATE INDEX IF NOT EXISTS comments_review ON comments(review_id,created_at);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _profile(self, db, row, viewer=None):
        if not row:
            raise SocialError('Profile not found.', 404)
        uid = row['id']
        return {'id': uid, 'username': row['username'], 'displayName': row['display_name'], 'bio': row['bio'],
                'followers': db.execute('SELECT count(*) FROM follows WHERE target=?', (uid,)).fetchone()[0],
                'following': db.execute('SELECT count(*) FROM follows WHERE follower=?', (uid,)).fetchone()[0],
                'reviews': db.execute('SELECT count(*) FROM reviews WHERE user_id=? AND deleted_at IS NULL', (uid,)).fetchone()[0],
                'isFollowing': bool(viewer and db.execute('SELECT 1 FROM follows WHERE follower=? AND target=?', (viewer, uid)).fetchone())}

    def _new_session(self, db, user_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        db.execute('DELETE FROM sessions WHERE expires_at<?', (time.time(),))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (sha256(token.encode()).hexdigest(), user_id, csrf, time.time() + 30 * 86400))
        return token, csrf

    def register(self, payload):
        username = text(payload.get('username'), 25).lower()
        password = payload.get('password')
        if not re.fullmatch(r'[a-z0-9_]{3,24}', username):
            raise SocialError('Use 3–24 letters, numbers, or underscores for your username.')
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise SocialError('Choose a password with 10–128 characters.')
        salt = secrets.token_bytes(16)
        digest = scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        uid = secrets.token_hex(16)
        try:
            with self.db() as db:
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)', (uid, username, text(payload.get('displayName'), 60) or username, '', salt, digest, time.time()))
                token, csrf = self._new_session(db, uid)
                user = self._profile(db, db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise SocialError('That username is already in use.', 409) from exc
        return {'user': user, 'csrf': csrf}, token

    def login(self, payload):
        username, password = text(payload.get('username'), 24).lower(), payload.get('password')
        if not isinstance(password, str) or len(password) > 128:
            raise SocialError('Username or password is incorrect.', 401)
        with self.db() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            salt = row['salt'] if row else bytes(16)
            digest = scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
            if not row or not hmac.compare_digest(digest, row['password_hash']):
                raise SocialError('Username or password is incorrect.', 401)
            token, csrf = self._new_session(db, row['id'])
            return {'user': self._profile(db, row), 'csrf': csrf}, token

    def session(self, token):
        if not token:
            return {'user': None, 'csrf': ''}
        with self.db() as db:
            row = db.execute('SELECT user_id,csrf FROM sessions WHERE token_hash=? AND expires_at>?',
                             (sha256(token.encode()).hexdigest(), time.time())).fetchone()
            if not row:
                return {'user': None, 'csrf': ''}
            user = db.execute('SELECT * FROM users WHERE id=?', (row['user_id'],)).fetchone()
            return {'user': self._profile(db, user), 'csrf': row['csrf']}

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (sha256(token.encode()).hexdigest(),))

    def profile(self, username, viewer=None):
        with self.db() as db:
            return self._profile(db, db.execute('SELECT * FROM users WHERE username=?', (text(username, 24),)).fetchone(), viewer)

    def update_profile(self, user_id, payload):
        name, bio = text(payload.get('displayName'), 60), text(payload.get('bio'), 500)
        if not name:
            raise SocialError('Add a display name.')
        with self.db() as db:
            db.execute('UPDATE users SET display_name=?,bio=? WHERE id=?', (name, bio, user_id))
            return self._profile(db, db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone(), user_id)

    def people(self, query, viewer=None):
        query = text(query, 40).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        with self.db() as db:
            rows = db.execute("SELECT * FROM users WHERE username LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\' ORDER BY username LIMIT 20", ('%' + query + '%', '%' + query + '%')).fetchall()
            return [self._profile(db, row, viewer) for row in rows]

    def follow(self, user_id, username, active):
        with self.db() as db:
            target = db.execute('SELECT * FROM users WHERE username=?', (text(username, 24),)).fetchone()
            if not target:
                raise SocialError('Profile not found.', 404)
            if target['id'] == user_id:
                raise SocialError('This is your own profile.')
            if active:
                db.execute('INSERT OR IGNORE INTO follows VALUES(?,?)', (user_id, target['id']))
            else:
                db.execute('DELETE FROM follows WHERE follower=? AND target=?', (user_id, target['id']))
            return self._profile(db, target, user_id)

    def publish(self, user_id, payload):
        item = payload.get('item')
        if not isinstance(item, dict) or not isinstance(item.get('type'), str) or item.get('type') not in TYPES or not text(item.get('title'), 300):
            raise SocialError('Choose a title from your shelf.')
        snapshot = {key: text(item.get(key), limit) for key, limit in [('type', 20), ('title', 300), ('creator', 300), ('year', 4), ('source', 100), ('imdbId', 20)]}
        snapshot.update(image=image_url(item.get('image')), sourceUrl=image_url(item.get('sourceUrl')) if str(item.get('sourceUrl', '')).startswith(('http://', 'https://')) else '', genres=genres(item.get('genres')))
        try:
            rating = float(payload.get('rating', 0))
        except (TypeError, ValueError) as exc:
            raise SocialError('Choose a rating between zero and five.') from exc
        if not math.isfinite(rating) or not 0 <= rating <= 5 or rating * 2 != int(rating * 2):
            raise SocialError('Choose a half-star rating between zero and five.')
        body = text(payload.get('text'), 10000)
        if not body and not rating:
            raise SocialError('Write a review or choose a rating.')
        now, review_id = time.time(), text(payload.get('id'), 40) or secrets.token_hex(16)
        with self.db() as db:
            old = db.execute('SELECT user_id FROM reviews WHERE id=?', (review_id,)).fetchone()
            if old and old['user_id'] != user_id:
                raise SocialError('You can only edit your own review.', 403)
            if old:
                db.execute('UPDATE reviews SET item=?,body=?,rating=?,spoiler=?,updated_at=? WHERE id=?',
                           (json.dumps(snapshot), body, rating, bool(payload.get('spoiler')), now, review_id))
            else:
                db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,NULL)',
                           (review_id, user_id, json.dumps(snapshot), body, rating, bool(payload.get('spoiler')), now, now))
        return self.review(review_id, user_id)

    def _review(self, db, row, viewer=None):
        author = db.execute('SELECT id,username,display_name FROM users WHERE id=?', (row['user_id'],)).fetchone()
        comments = db.execute('''SELECT c.id,c.body,c.created_at,c.user_id,u.username,u.display_name
            FROM comments c JOIN users u ON u.id=c.user_id WHERE c.review_id=? AND c.deleted_at IS NULL
            ORDER BY c.created_at DESC,c.id DESC LIMIT 5''', (row['id'],)).fetchall()
        return {'id': row['id'], 'author': {'id': author['id'], 'username': author['username'], 'displayName': author['display_name']},
                'item': json.loads(row['item']), 'text': row['body'], 'rating': row['rating'], 'spoiler': bool(row['spoiler']),
                'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'own': row['user_id'] == viewer,
                'likes': db.execute('SELECT count(*) FROM likes WHERE review_id=?', (row['id'],)).fetchone()[0],
                'liked': bool(viewer and db.execute('SELECT 1 FROM likes WHERE review_id=? AND user_id=?', (row['id'], viewer)).fetchone()),
                'commentCount': db.execute('SELECT count(*) FROM comments WHERE review_id=? AND deleted_at IS NULL', (row['id'],)).fetchone()[0],
                'comments': [{'id': c['id'], 'text': c['body'], 'username': c['username'], 'displayName': c['display_name'], 'own': c['user_id'] == viewer} for c in reversed(comments)]}

    def review(self, review_id, viewer=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM reviews WHERE id=? AND deleted_at IS NULL', (review_id,)).fetchone()
            if not row:
                raise SocialError('Review not found.', 404)
            return self._review(db, row, viewer)

    def feed(self, viewer=None, scope='everyone', username='', page=0):
        clauses, args = ['r.deleted_at IS NULL'], []
        if username:
            clauses.append('u.username=?'); args.append(text(username, 24))
        elif scope == 'following':
            if not viewer:
                return {'items': [], 'hasMore': False, 'page': 0}
            clauses.append('(r.user_id=? OR r.user_id IN (SELECT target FROM follows WHERE follower=?))'); args.extend([viewer, viewer])
        page = max(0, min(10000, int(page)))
        with self.db() as db:
            rows = db.execute('SELECT r.* FROM reviews r JOIN users u ON u.id=r.user_id WHERE ' + ' AND '.join(clauses) + ' ORDER BY r.created_at DESC,r.id DESC LIMIT 21 OFFSET ?', args + [page * 20]).fetchall()
            return {'items': [self._review(db, row, viewer) for row in rows[:20]], 'page': page, 'hasMore': len(rows) > 20}

    def like(self, user_id, review_id, active):
        self.review(review_id, user_id)
        with self.db() as db:
            if active:
                db.execute('INSERT OR IGNORE INTO likes VALUES(?,?)', (review_id, user_id))
            else:
                db.execute('DELETE FROM likes WHERE review_id=? AND user_id=?', (review_id, user_id))
        return self.review(review_id, user_id)

    def comment(self, user_id, review_id, body):
        body = text(body, 1000)
        if not body:
            raise SocialError('Write a comment first.')
        self.review(review_id, user_id)
        with self.db() as db:
            db.execute('INSERT INTO comments VALUES(?,?,?,?,?,NULL)', (secrets.token_hex(16), review_id, user_id, body, time.time()))
        return self.review(review_id, user_id)

    def set_hidden(self, user_id, review_id, hidden):
        with self.db() as db:
            row = db.execute('SELECT user_id FROM reviews WHERE id=?', (review_id,)).fetchone()
            if not row:
                raise SocialError('Review not found.', 404)
            if row['user_id'] != user_id:
                raise SocialError('You can only change your own review.', 403)
            db.execute('UPDATE reviews SET deleted_at=? WHERE id=?', (time.time() if hidden else None, review_id))
        return {'id': review_id, 'hidden': hidden}

    def community_rating(self, item):
        with self.db() as db:
            rows = db.execute('''SELECT user_id,rating FROM reviews WHERE deleted_at IS NULL
                AND json_extract(item,'$.type')=? AND lower(json_extract(item,'$.title'))=lower(?)
                AND json_extract(item,'$.year')=? ORDER BY updated_at DESC,id DESC''',
                (item.get('type'), text(item.get('title'), 300), text(item.get('year'), 4))).fetchall()
        latest = {}
        for row in rows:
            if row['user_id'] not in latest:
                latest[row['user_id']] = row['rating']
        values = [value for value in latest.values() if value > 0]
        return {'source': 'Trace', 'value': sum(values) / len(values), 'max': 5, 'count': len(values), 'url': '#social'} if values else None
