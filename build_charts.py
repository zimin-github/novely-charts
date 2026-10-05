#!/usr/bin/env python3
"""Builds Novely's book charts into a single static JSON file.

Why this exists
---------------
Novely's Discover shelves used to call Open Library directly from the phone.
Open Library is an Internet Archive host and is unreachable from a number of
networks — including whole countries — so those readers saw an empty screen
through no fault of their own. Running the fetch on a build server instead
fixes that: the server reaches Open Library, and every reader only ever talks
to one static file that any CDN can serve.

What this file may and may not republish
----------------------------------------
Everything in `charts/ru.json` is published on the open web, so this script is
a *republisher*, not just a client. That draws a line through the middle of it:

* **Open Library** — catalogue data is released by the Internet Archive into
  the public domain (CC0), and cover images are referenced by URL from
  covers.openlibrary.org rather than copied. Republishing a list of works with
  their titles, authors and cover URLs is what that data is for.
* **Google Books** — is not republishable. Its API terms do not grant the right
  to store its content and serve it onward from somebody else's domain, and a
  nightly job writing Google's titles, descriptions, cover URLs and volume ids
  into a public file is exactly that: the API used as an uncontrolled bulk
  backend. It is also impossible to attribute properly from a static file,
  because the volume's Google Books link — which their branding terms require
  next to their data — was not even carried through.

  So Google Books was removed from this builder entirely. The app still calls
  Google Books live, per reader action, with Google's attribution and link on
  screen; nothing Google returns is cached on a server or published.

See REPUBLISHING.md for the full account of what this feed contains and on what
basis.

Output: charts/ru.json — see SCHEMA_VERSION for the contract the app expects.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA_VERSION = 1

# Open Library asks API clients to identify the application and leave an address
# a human can write to; an anonymous User-Agent is what gets an IP blocked. The
# address is an environment variable so a fork identifies its own maintainer.
CONTACT_EMAIL = os.environ.get("NOVELY_CONTACT_EMAIL", "app.zimin@gmail.com")
USER_AGENT = (
    "NovelyCharts/2.0 (+https://github.com/zimin-github/novely-charts; "
    f"{CONTACT_EMAIL})"
)

OUTPUT = pathlib.Path(__file__).parent / "charts" / "ru.json"
BOOKS_PER_SHELF = 24

# Minimum gap between two requests to the same host, and the cache's lifetime.
#
# The old script fired its requests as fast as the runner could open sockets,
# including a thread pool against an unauthenticated endpoint, and the seeds
# repeat heavily across genres — "Атомные привычки" is in three of them — so the
# same query was asked several times per run. One request a second, answered
# from a local cache where possible, is a fraction of the traffic for the same
# output.
MIN_REQUEST_INTERVAL = 1.0
CACHE_DIR = pathlib.Path(__file__).parent / ".cache"
CACHE_TTL = 12 * 60 * 60

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
    "scienceFiction": {
        "title": "Фантастика",
        "openlibrary": "science_fiction",
        "seeds": [
            ("Задача трёх тел", "Лю Цысинь"), ("Дюна", "Герберт"),
            ("Пикник на обочине", "Стругацкие"), ("Марсианин", "Вейер"),
            ("451 градус по Фаренгейту", "Брэдбери"), ("Основание", "Азимов"),
            ("Солярис", "Лем"), ("Мы", "Замятин"),
            ("Автостопом по галактике", "Адамс"), ("Ложная слепота", "Уоттс"),
        ],
    },
    "thriller": {
        "title": "Триллеры",
        "openlibrary": "thrillers",
        "seeds": [
            ("Молчание ягнят", "Харрис"), ("Исчезнувшая", "Флинн"),
            ("Мизери", "Кинг"), ("Код да Винчи", "Браун"),
            ("Девушка в поезде", "Хокинс"), ("Тайная история", "Тартт"),
            ("Институт", "Кинг"), ("Безмолвный пациент", "Микаэлидес"),
            ("Ангелы и демоны", "Браун"), ("Метро 2033", "Глуховский"),
        ],
    },
    "horror": {
        "title": "Ужасы",
        "openlibrary": "horror",
        "seeds": [
            ("Сияние", "Кинг"), ("Оно", "Кинг"),
            ("Дракула", "Стокер"), ("Франкенштейн", "Шелли"),
            ("Кладбище домашних животных", "Кинг"), ("Призрак дома на холме", "Джексон"),
            ("Зов Ктулху", "Лавкрафт"), ("Кэрри", "Кинг"),
            ("Вий", "Гоголь"), ("Ребёнок Розмари", "Левин"),
        ],
    },
    "historical": {
        "title": "Исторические",
        "openlibrary": "historical_fiction",
        "seeds": [
            ("Война и мир", "Толстой"), ("Унесённые ветром", "Митчелл"),
            ("Столпы Земли", "Фоллетт"), ("Тихий Дон", "Шолохов"),
            ("Имя розы", "Эко"), ("Книжный вор", "Зусак"),
            ("Соловей", "Ханна"), ("Пётр Первый", "Толстой"),
            ("Овод", "Войнич"), ("Айвенго", "Скотт"),
        ],
    },
    "youngAdult": {
        "title": "Young Adult",
        "openlibrary": "young_adult_fiction",
        "seeds": [
            ("Голодные игры", "Коллинз"), ("Виноваты звёзды", "Грин"),
            ("Дивергент", "Рот"), ("Хорошо быть тихоней", "Чбоски"),
            ("Бегущий в лабиринте", "Дашнер"), ("Сумерки", "Майер"),
            ("Шестёрка воронов", "Бардуго"), ("Тень и кость", "Бардуго"),
            ("Пятая волна", "Янси"), ("Гарри Поттер и философский камень", "Роулинг"),
        ],
    },
    "biography": {
        "title": "Биографии",
        "openlibrary": "biography",
        "seeds": [
            ("Стив Джобс", "Айзексон"), ("Дневник Анны Франк", "Франк"),
            ("Becoming. Моя история", "Обама"), ("Илон Маск", "Айзексон"),
            ("Я — Малала", "Юсафзай"), ("Автобиография", "Кристи"),
            ("Моя краткая история", "Хокинг"), ("Эйнштейн. Его жизнь и вселенная", "Айзексон"),
            ("Леонардо да Винчи", "Айзексон"), ("Мемуары", "Сахаров"),
        ],
    },
    "psychology": {
        "title": "Психология",
        "openlibrary": "psychology",
        "seeds": [
            ("Тело помнит всё", "ван дер Колк"), ("Игры, в которые играют люди", "Берн"),
            ("Психология влияния", "Чалдини"), ("Думай медленно решай быстро", "Канеман"),
            ("Дар психотерапии", "Ялом"), ("Хочу и буду", "Лабковский"),
            ("Гибкое сознание", "Дуэк"), ("Человек в поисках смысла", "Франкл"),
            ("Не рычите на собаку", "Прайор"), ("Токсичные родители", "Форвард"),
        ],
    },
    "business": {
        "title": "Бизнес",
        "openlibrary": "business",
        "seeds": [
            ("От хорошего к великому", "Коллинз"), ("Богатый папа, бедный папа", "Кийосаки"),
            ("Атомные привычки", "Клир"), ("Бизнес с нуля", "Рис"),
            ("Клиенты на всю жизнь", "Сьюэлл"), ("Джедайские техники", "Дорофеев"),
            ("Как завоёвывать друзей", "Карнеги"), ("Чёрный лебедь", "Талеб"),
            ("Стратегия голубого океана", "Ким"), ("Спроси маму", "Фитцпатрик"),
        ],
    },
}

JUNK_MARKERS = (
    "фанфик", "рабочая тетрадь", "раскраска", "краткое содержание",
    "краткое изложение", "путеводитель по", "читательский дневник",
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


_last_request_at: dict[str, float] = {}


def _throttle(host: str) -> None:
    """Keeps at least `MIN_REQUEST_INTERVAL` between requests to one host."""
    previous = _last_request_at.get(host)
    if previous is not None:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - previous)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.monotonic()


def _cache_path(url: str) -> pathlib.Path:
    return CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")


def _cached(url: str):
    path = _cache_path(url)
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store(url: str, payload) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _retry_after_seconds(error: urllib.error.HTTPError, fallback: float) -> float:
    """`Retry-After` is either seconds or an HTTP date; both are honoured.

    A rate-limited client that keeps knocking at its own pace is how an IP gets
    blocked, so when the server says how long to wait, that is the wait.
    """
    header = (error.headers.get("Retry-After") or "").strip()
    if not header:
        return fallback
    try:
        return max(float(header), 1.0)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return max((parsedate_to_datetime(header).timestamp() - time.time()), 1.0)
    except (TypeError, ValueError, OverflowError):
        return fallback


def get_json(url: str, timeout: int = 30, retries: int = 4):
    """Fetches JSON: cached, throttled, and backing off when told to.

    Three things, none of which this did before. The cache means a seed that
    appears in three genres is fetched once. The throttle means the whole run is
    a steady trickle rather than a burst. And a 429 is answered with the pause
    the server asked for instead of a delay this script picked.
    """
    cached = _cached(url)
    if cached is not None:
        return cached

    host = urllib.parse.urlparse(url).netloc
    delay = 5.0
    for attempt in range(retries):
        _throttle(host)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            _store(url, payload)
            return payload
        except urllib.error.HTTPError as error:
            rate_limited = error.code in (429, 403, 503)
            if not rate_limited or attempt == retries - 1:
                raise
            wait = _retry_after_seconds(error, delay)
            log(f"  {error.code} — waiting {wait:.0f}s before retrying")
            time.sleep(wait)
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


# --- Google Books: deliberately absent ---------------------------------------
#
# `google_search`, `to_book`, `google_chart`, `resolve_seed` and the
# `GOOGLE_SUBJECTS` table used to live here. They fetched Google Books volumes
# and wrote their titles, authors, descriptions, published dates, ISBNs, page
# counts and cover URLs into `charts/ru.json`, which is then served publicly
# from GitHub Pages — a nightly bulk copy of one service's catalogue
# republished from another domain, with no link back to the volume on Google
# Books and no attribution of any kind in the payload.
#
# Nothing here calls Google Books any more, and `GOOGLE_BOOKS_KEY` is no longer
# read. The app calls Google Books live, when a reader searches, and shows
# Google's attribution and link beside the results. See REPUBLISHING.md.

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


def resolve_seed(seed: tuple[str, str]) -> dict | None:
    """Finds a curated seed's Russian edition in Open Library.

    This used to go to Google Books, which answered a title-and-author query
    better — but a seed resolved through Google is a Google Books volume, and
    the result of this function is written into a public file. Open Library's
    data can be republished; Google's cannot. Where a seed does not resolve it
    is dropped, so the shelf comes back shorter rather than carrying something
    this feed has no right to serve.
    """
    title, author = seed
    queries = [
        f'title:"{title}" author:"{author}" language:rus',
        f"{title} {author} language:rus",
    ]
    for query in queries:
        for doc in openlibrary_docs(query, limit=5):
            book = openlibrary_to_book(doc)
            if book and is_publishable(book):
                return book
    return None


# --- Build ------------------------------------------------------------------

def build_shelf(slug: str, config: dict) -> dict:
    log(f"{slug}: live chart…")
    books = live_chart(slug, config)
    source = "openlibrary"

    # Open Library ranks by reading activity, which is the shelf worth having
    # when it answers — but it is an Internet Archive host and for months at a
    # time it has not answered at all. The middle tier used to be Google's
    # subject index; it is gone (see the note above), because this file is
    # published and Google's data may not be. What is left is Novely's own
    # curated list, resolved through Open Library, and the app labels it as
    # Novely's own rather than as anybody's chart.
    if len(books) < 6:
        log(f"{slug}: live chart returned {len(books)}, falling back to curated seeds")
        # Sequential, not a thread pool: `get_json` throttles per host, and
        # firing these in parallel against an unauthenticated endpoint is what
        # earned the rate limiting in the first place.
        resolved = [resolve_seed(seed) for seed in config["seeds"]]
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
    built = {slug: build_shelf(slug, config) for slug, config in GENRES.items()}
    # A shelf with nothing in it is left out of the feed rather than published
    # empty, and `newReleases` and `nonFiction` are left out every time: Open
    # Library holds no Russian editions for their seeds to resolve against, so
    # both shelves resolve to nothing. See README, "Чего в фиде нет".
    #
    # The app fills an omitted always-visible shelf itself, live through Google
    # Books, and credits it as Novely's own selection — see
    # `fillShelvesMissingFromFeed` in GoogleBooksService. Publishing the key
    # with an empty list instead would be worse than leaving it out: the app
    # would read the shelf as present-and-empty and `DiscoverView` hides a
    # genre with no books, so «Новые книги» would simply disappear.
    shelves = {slug: shelf for slug, shelf in built.items() if shelf["books"]}
    for slug in built.keys() - shelves.keys():
        log(f"{slug}: no publishable books, leaving the shelf out of the feed")

    if not shelves:
        log("ERROR: every shelf is empty — refusing to publish an empty feed")
        return 1

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # States in the file itself where its contents came from and on what
        # basis, so the question can be answered by whoever opens the feed
        # rather than only by reading this repository.
        "attribution": {
            "dataSource": "Open Library (Internet Archive)",
            "dataSourceURL": "https://openlibrary.org/",
            "dataLicence": "CC0 — https://openlibrary.org/developers/api",
            "coverImages": "Referenced by URL from covers.openlibrary.org; not copied.",
            "curatedShelves": (
                "Shelves marked \"curated\" are Novely's own editorial lists; "
                "their editions are resolved through Open Library."
            ),
            "googleBooks": (
                "Not present. Google Books is called live by the app, per reader "
                "action, with Google's attribution on screen; none of its data is "
                "cached or republished here."
            ),
            "contact": CONTACT_EMAIL,
        },
        "shelves": shelves,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(shelf["books"]) for shelf in shelves.values())
    log(f"wrote {OUTPUT} — {total} books across {len(shelves)} shelves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
