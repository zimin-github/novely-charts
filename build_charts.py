#!/usr/bin/env python3
"""Builds Novely's book charts into a single static JSON file.

Why this exists
---------------
Novely's Discover shelves used to call Open Library directly from the phone.
Open Library is an Internet Archive host and is unreachable from a number of
networks — including whole countries — so those readers saw an empty screen
through no fault of their own. Google Books, the other candidate, has no chart
endpoint at all and ignores `langRestrict=ru` on subject queries.

Running the fetch on a build server instead of on the phone fixes both:
the server reaches Open Library, and every reader only ever talks to one static
file that any CDN can serve.

Output: charts/ru.json — see SCHEMA_VERSION for the contract the app expects.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SCHEMA_VERSION = 1
USER_AGENT = "NovelyCharts/1.0 (+https://github.com/zimin-github/novely-charts)"
GOOGLE_KEY = os.environ.get("GOOGLE_BOOKS_KEY", "")
OUTPUT = pathlib.Path(__file__).parent / "charts" / "ru.json"
BOOKS_PER_SHELF = 24

# Genre slugs must match DiscoverGenre.rawValue in the app.
GENRES = {
    "popular": {
        "title": "Популярное",
        "openlibrary": "__popular__",
        "seeds": [
            ("Мастер и Маргарита", "Булгаков"), ("Тень горы", "Робертс"),
            ("Сто лет одиночества", "Маркес"), ("Атомные привычки", "Клир"),
            ("Ведьмак", "Сапковский"), ("Хоббит", "Толкин"),
            ("Преступление и наказание", "Достоевский"), ("1984", "Оруэлл"),
            ("Маленькая жизнь", "Янагихара"), ("Заводной апельсин", "Бёрджесс"),
            ("Дом, в котором", "Петросян"), ("Цветы для Элджернона", "Киз"),
        ],
    },
    "newReleases": {
        # No live source. Open Library's Russian 2025-2026 entries are almost
        # entirely self-published single editions — filtering them by edition
        # count leaves one book out of twenty-four — so a hand-picked list of
        # actual recent releases is the more honest shelf here.
        "title": "Новые книги",
        "openlibrary": None,
        "seeds": [
            ("Тоннель", "Вагнер"), ("Уроки химии", "Гармус"),
            ("Йеллоуфейс", "Куанг"), ("Тревожные люди", "Бакман"),
            ("До самого рая", "Янагихара"), ("Обитель", "Прилепин"),
            ("Завтра, и завтра, и завтра", "Зевин"), ("Вавилон", "Куанг"),
            ("Прекрасный мир, где же ты", "Руни"), ("Петровы в гриппе", "Сальников"),
        ],
    },
    "fantasy": {
        "title": "Фэнтези",
        "openlibrary": "fantasy",
        "seeds": [
            ("Ведьмак", "Сапковский"), ("Хоббит", "Толкин"),
            ("Властелин колец", "Толкин"), ("Имя ветра", "Ротфусс"),
            ("Американские боги", "Гейман"), ("Ночной дозор", "Лукьяненко"),
            ("Игра престолов", "Мартин"), ("Задача трёх тел", "Лю Цысинь"),
            ("Пикник на обочине", "Стругацкие"), ("Хроники Амбера", "Желязны"),
        ],
    },
    "mystery": {
        "title": "Детективы",
        "openlibrary": "mystery_and_detective_stories",
        "seeds": [
            ("Собака Баскервилей", "Дойл"), ("Девушка с татуировкой дракона", "Ларссон"),
            ("Тайная история", "Тартт"), ("Молчание ягнят", "Харрис"),
            ("Шерлок Холмс", "Дойл"), ("Убийство Роджера Экройда", "Кристи"),
            ("Убийство в Восточном экспрессе", "Кристи"), ("Исчезнувшая", "Флинн"),
            ("И не осталось никого", "Кристи"), ("Женщина в окне", "Финн"),
        ],
    },
    "romance": {
        "title": "Романтика",
        "openlibrary": "romance",
        "seeds": [
            ("Гордость и предубеждение", "Остин"), ("Джейн Эйр", "Бронте"),
            ("Виноваты звёзды", "Грин"), ("Унесённые ветром", "Митчелл"),
            ("Дневник Бриджит Джонс", "Филдинг"), ("Нормальные люди", "Руни"),
            ("Поющие в терновнике", "Маккалоу"), ("До встречи с тобой", "Мойес"),
            ("Театр", "Моэм"), ("Анна Каренина", "Толстой"),
        ],
    },
    "classics": {
        "title": "Классика",
        "openlibrary": "classic_literature",
        "seeds": [
            ("Евгений Онегин", "Пушкин"), ("Мёртвые души", "Гоголь"),
            ("Отцы и дети", "Тургенев"), ("Идиот", "Достоевский"),
            ("Вишнёвый сад", "Чехов"), ("Старик и море", "Хемингуэй"),
            ("Великий Гэтсби", "Фицджеральд"), ("Анна Каренина", "Толстой"),
            ("Война и мир", "Толстой"), ("Герой нашего времени", "Лермонтов"),
        ],
    },
    "nonFiction": {
        "title": "Нон-фикшн",
        "openlibrary": "self-help",
        "seeds": [
            ("Атомные привычки", "Клир"), ("Sapiens", "Харари"),
            ("Думай медленно решай быстро", "Канеман"), ("Тонкое искусство пофигизма", "Мэнсон"),
            ("Психология влияния", "Чалдини"), ("Хочу и буду", "Лабковский"),
            ("Богатый папа, бедный папа", "Кийосаки"), ("Гибкое сознание", "Дуэк"),
            ("Человек в поисках смысла", "Франкл"), ("Сила воли", "Макгонигал"),
        ],
    },
}

JUNK_MARKERS = (
    "фанфик", "рабочая тетрадь", "раскраска", "краткое содержание",
    "краткое изложение", "путеводитель по", "читательский дневник",
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def get_json(url: str, timeout: int = 30, retries: int = 4):
    """Fetches JSON, backing off on rate limits.

    Unauthenticated Google Books calls from a CI runner share an IP with every
    other project on the platform, so a burst of requests reliably earns a 429.
    Retrying with a widening delay turns that from a failed build into a slower
    one; an API key removes it almost entirely.
    """
    delay = 2.0
    for attempt in range(retries):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            rate_limited = error.code in (429, 403)
            if not rate_limited or attempt == retries - 1:
                raise
            log(f"  {error.code} — retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def has_cyrillic(text: str) -> bool:
    return any(0x0400 <= ord(character) <= 0x04FF for character in text or "")


def is_publishable(book: dict) -> bool:
    """The app drops anything without a real cover, so filter here instead of
    shipping entries that would render as an empty slot on the shelf."""
    title = book.get("title", "")
    if not title or not has_cyrillic(title) or len(title) > 100:
        return False
    if not book.get("authors") or not book.get("coverURL"):
        return False
    lowered = title.lower()
    return not any(marker in lowered for marker in JUNK_MARKERS)


# --- Google Books -----------------------------------------------------------

def google_search(query: str, limit: int = 5, order: str = "relevance") -> list[dict]:
    params = {
        "q": query,
        "maxResults": limit,
        "orderBy": order,
        "printType": "books",
        "projection": "full",
        "langRestrict": "ru",
    }
    if GOOGLE_KEY:
        params["key"] = GOOGLE_KEY
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    try:
        return get_json(url).get("items") or []
    except Exception as error:  # noqa: BLE001 - a dead source must not fail the build
        log(f"  google error for {query!r}: {error}")
        return []


def to_book(item: dict) -> dict | None:
    info = item.get("volumeInfo") or {}
    language = info.get("language")
    if language not in (None, "ru"):
        return None
    images = info.get("imageLinks") or {}
    # Deliberately NOT rewriting zoom=1 -> zoom=2: Google answers that with a
    # broken 300x48 sliver for a sizeable share of volumes.
    cover = (
        images.get("extraLarge") or images.get("large") or images.get("medium")
        or images.get("thumbnail") or images.get("smallThumbnail") or ""
    ).replace("http://", "https://")
    if not cover:
        return None
    identifiers = info.get("industryIdentifiers") or []
    isbn = ""
    for wanted in ("ISBN_13", "ISBN_10"):
        match = next((i["identifier"] for i in identifiers if i.get("type") == wanted), None)
        if match:
            isbn = match
            break
    return {
        "id": item.get("id", ""),
        "title": info.get("title", ""),
        "authors": info.get("authors") or [],
        "publishedDate": info.get("publishedDate", ""),
        "description": info.get("description", ""),
        "isbn": isbn,
        "coverURL": cover,
        "totalPages": info.get("pageCount") or 0,
    }


def resolve_seed(seed: tuple[str, str]) -> dict | None:
    title, author = seed
    for query in (f'intitle:"{title}" inauthor:"{author}"', f"{title} {author}"):
        for item in google_search(query):
            book = to_book(item)
            if book and is_publishable(book):
                return book
    return None


# --- Open Library -----------------------------------------------------------

# Open Library indexes a *work* under its original title, so a Russian
# translation is filed under "Harry Potter and the Philosopher's Stone" with an
# English title on the work record. Asking only for work-level fields therefore
# returned sixty results and zero usable ones for every genre. The Russian
# edition has to be requested alongside and read from there.
OPENLIBRARY_FIELDS = ",".join([
    "key", "title", "author_name", "author_alternative_name", "cover_i",
    "first_publish_year", "number_of_pages_median", "isbn",
    "editions", "editions.key", "editions.title", "editions.language",
    "editions.cover_i", "editions.isbn",
])


def openlibrary_docs(query: str, sort: str | None = None, limit: int = 60) -> list[dict]:
    params = {
        "q": query,
        "lang": "ru",
        "limit": limit,
        "fields": OPENLIBRARY_FIELDS,
    }
    if sort:
        params["sort"] = sort
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    try:
        return get_json(url).get("docs") or []
    except Exception as error:  # noqa: BLE001
        log(f"  openlibrary error: {error}")
        return []


def openlibrary_to_book(doc: dict) -> dict | None:
    """Prefers the Russian edition's own title and cover over the work's."""
    editions = [
        edition for edition in ((doc.get("editions") or {}).get("docs") or [])
        if "rus" in (edition.get("language") or [])
    ]
    localized = next(
        (edition for edition in editions if has_cyrillic(edition.get("title") or "")),
        None,
    )

    title = (localized or {}).get("title") or doc.get("title") or ""
    if not has_cyrillic(title):
        return None

    cover_id = (localized or {}).get("cover_i") or doc.get("cover_i")
    isbns = (localized or {}).get("isbn") or doc.get("isbn") or []
    isbn = next((i for i in isbns if len(i) == 13), next(iter(isbns), ""))

    if cover_id:
        cover = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
    elif isbn:
        cover = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
    else:
        return None

    return {
        "id": (localized or {}).get("key") or doc.get("key", ""),
        "title": title,
        "authors": localized_authors(doc),
        "publishedDate": str(doc.get("first_publish_year") or ""),
        "description": "",
        "isbn": isbn,
        "coverURL": cover,
        "totalPages": doc.get("number_of_pages_median") or 0,
    }


# Cyrillic letters that exist in Ukrainian, Belarusian or Serbian but not in
# Russian. Open Library files every Cyrillic spelling of an author under the
# same alternative-names list, so taking the first Cyrillic one credited
# "Джоан Роулінг" — the Ukrainian form — on a Russian shelf.
NON_RUSSIAN_CYRILLIC = set("іїєґўњљџѓќѕђћ")


def is_russian_text(value: str) -> bool:
    return has_cyrillic(value) and not (set(value.lower()) & NON_RUSSIAN_CYRILLIC)


# Authors who recur across these shelves and whom Open Library carries only
# under their Latin name. A Russian shelf crediting "Suzanne Collins" beside
# "Стивен Кинг" looks half-translated.
KNOWN_AUTHORS = {
    "j. k. rowling": "Джоан Роулинг",
    "joanne rowling": "Джоан Роулинг",
    "colleen hoover": "Колин Гувер",
    "jane austen": "Джейн Остин",
    "suzanne collins": "Сьюзен Коллинз",
    "stephenie meyer": "Стефани Майер",
    "stephen king": "Стивен Кинг",
    "dan brown": "Дэн Браун",
    "agatha christie": "Агата Кристи",
    "j. r. r. tolkien": "Джон Толкин",
    "george orwell": "Джордж Оруэлл",
    "harper lee": "Харпер Ли",
    "f. scott fitzgerald": "Фрэнсис Скотт Фицджеральд",
    "ernest hemingway": "Эрнест Хемингуэй",
    "gabriel garcía márquez": "Габриэль Гарсиа Маркес",
    "haruki murakami": "Харуки Мураками",
    "arthur conan doyle": "Артур Конан Дойл",
    "george r. r. martin": "Джордж Мартин",
    "neil gaiman": "Нил Гейман",
    "andrzej sapkowski": "Анджей Сапковский",
    "paulo coelho": "Пауло Коэльо",
    "khaled hosseini": "Халед Хоссейни",
    "john green": "Джон Грин",
    "rick riordan": "Рик Риордан",
    "c. s. lewis": "Клайв Стейплз Льюис",
    "ray bradbury": "Рэй Брэдбери",
    "hanya yanagihara": "Ханья Янагихара",
    "delia owens": "Делия Оуэнс",
    "markus zusak": "Маркус Зусак",
}


def localized_authors(doc: dict) -> list[str]:
    """Russian spelling of the author when the index carries one, since the
    primary name on a translated work is usually the original Latin form."""
    names = doc.get("author_name") or []
    if len(names) == 1:
        known = KNOWN_AUTHORS.get(names[0].strip().lower())
        if known:
            return [known]
    alternatives = [n for n in (doc.get("author_alternative_name") or []) if is_russian_text(n)]

    if len(names) == 1:
        if is_russian_text(names[0]):
            return names
        if alternatives:
            # Shortest reasonable form: the list holds everything from
            # "Кинг" to "Кинг, Стивен Эдвин, 1947-".
            return [min(alternatives, key=len)]
    return names


def live_chart(slug: str, config: dict) -> list[dict]:
    subject = config["openlibrary"]
    if subject is None:
        return []
    if subject == "__popular__":
        # The monthly trending feed is global and overwhelmingly English, and
        # intersecting it with Russian editions left about one usable book.
        # Sorting Russian-language works by reading activity gives an actual
        # popular list for this audience.
        docs = openlibrary_docs("language:rus", sort="readinglog")
    else:
        docs = openlibrary_docs(f"subject:{subject} language:rus", sort="readinglog")

    books = [openlibrary_to_book(doc) for doc in docs]
    return [book for book in books if book and is_publishable(book)]


# --- Build ------------------------------------------------------------------

def build_shelf(slug: str, config: dict) -> dict:
    log(f"{slug}: live chart…")
    books = live_chart(slug, config)
    source = "openlibrary"

    if len(books) < 6:
        log(f"{slug}: live chart returned {len(books)}, falling back to curated seeds")
        # Two at a time, not six. Six parallel workers against an
        # unauthenticated endpoint is what triggers the rate limiting in the
        # first place, and these seeds are not worth building fast.
        with ThreadPoolExecutor(max_workers=2) as pool:
            resolved = list(pool.map(resolve_seed, config["seeds"]))
        books = [book for book in resolved if book]
        source = "curated"

    # Dedupe on title + first author's surname, matching the app's own rule.
    seen, unique = set(), []
    for book in books:
        surname = (book["authors"][0].split()[-1].lower() if book["authors"] else "")
        key = f"{book['title'].lower()}|{surname}"
        if key not in seen:
            seen.add(key)
            unique.append(book)

    log(f"{slug}: {len(unique)} books from {source}")
    return {"title": config["title"], "source": source, "books": unique[:BOOKS_PER_SHELF]}


def main() -> int:
    shelves = {slug: build_shelf(slug, config) for slug, config in GENRES.items()}

    if all(not shelf["books"] for shelf in shelves.values()):
        log("ERROR: every shelf is empty — refusing to publish an empty feed")
        return 1

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "shelves": shelves,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(shelf["books"]) for shelf in shelves.values())
    log(f"wrote {OUTPUT} — {total} books across {len(shelves)} shelves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
