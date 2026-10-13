"""Search over the stop database.

Made to behave like the search of a map app: it forgives missing umlauts,
abbreviations and a few wrong letters, it finds a stop while its name is
still being typed, it puts the main station of a town before the bus stop
around the corner, and it prefers stops near the part of the map being
looked at, unless the words typed clearly mean a place elsewhere.

How a stop is ranked for a query:

1. Every word typed must match the stop: one of its name's words (exactly,
   as its beginning, inside it, as what its abbreviation stands for, or with
   a few letters wrong), the city it is in, or "Bahnhof" for a stop with
   trains. Each kind of match has a score; the worse the match, the lower.
2. That text score is scaled by how important the stop is (the number the
   map uses) and, if the caller says where the map is, by how near it is.

The timetable service's own search runs next to this one (services/stops.py):
it knows the towns of stops whose names here lack them, and has typo
handling of its own.
"""
import bisect
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict

from gtfs import store

# Districts that are one city. Stop names there often lack the city
# ("Pragfriedhof", "Durlach Turmberg", "Feudenheim, Feldstraße"), and the
# district is the first number of the stop id (de:08111:... is Stuttgart).
CITIES = {"08111": "Stuttgart", "08121": "Heilbronn", "08211": "Baden-Baden", "08212": "Karlsruhe", "08221": "Heidelberg",
          "08222": "Mannheim", "08231": "Pforzheim", "08311": "Freiburg", "08421": "Ulm"}
# All districts of Baden-Württemberg, to tell two stops of the same name apart
DISTRICTS = {
    **CITIES,
    "08115": "Kreis Böblingen", "08116": "Kreis Esslingen", "08117": "Kreis Göppingen", "08118": "Kreis Ludwigsburg",
    "08119": "Rems-Murr-Kreis", "08125": "Kreis Heilbronn", "08126": "Hohenlohekreis", "08127": "Kreis Schwäbisch Hall",
    "08128": "Main-Tauber-Kreis", "08135": "Kreis Heidenheim", "08136": "Ostalbkreis", "08215": "Kreis Karlsruhe",
    "08216": "Kreis Rastatt", "08225": "Neckar-Odenwald-Kreis", "08226": "Rhein-Neckar-Kreis", "08235": "Kreis Calw",
    "08236": "Enzkreis", "08237": "Kreis Freudenstadt", "08315": "Breisgau-Hochschwarzwald", "08316": "Kreis Emmendingen",
    "08317": "Ortenaukreis", "08325": "Kreis Rottweil", "08326": "Schwarzwald-Baar-Kreis", "08327": "Kreis Tuttlingen",
    "08335": "Kreis Konstanz", "08336": "Kreis Lörrach", "08337": "Kreis Waldshut", "08415": "Kreis Reutlingen",
    "08416": "Kreis Tübingen", "08417": "Zollernalbkreis", "08425": "Alb-Donau-Kreis", "08426": "Kreis Biberach",
    "08435": "Bodenseekreis", "08436": "Kreis Ravensburg", "08437": "Kreis Sigmaringen",
}
# Names in the file shorten some towns to their licence plate code ("KA
# Marktplatz", "FN Hafenbahnhof", "Rohrbach (HD)")
PLATE_CODES = {"KA": "karlsruhe", "MA": "mannheim", "HD": "heidelberg", "HN": "heilbronn", "PF": "pforzheim", "UL": "ulm",
               "FN": "friedrichshafen", "FDS": "freudenstadt", "GD": "gmund", "RV": "ravensburg", "RT": "reutlingen"}
# different spellings of the same word, in names and in what people type
SAME_WORD = {"hbf": "hauptbahnhof", "hauptbf": "hauptbahnhof", "bf": "bahnhof", "bhf": "bahnhof", "bahnhf": "bahnhof",
             "str": "strasse", "pl": "platz"}
# words that say little about which stop is meant; a name is not much
# "longer than asked for" because of them
MINOR_WORDS = {"bahnhof", "hauptbahnhof", "zob", "busbahnhof", "bus", "bstg", "gleis", "pos", "steig"}

# score of a typed word against a word of a name
EXACT = 1.0
BEGINNING = 0.9          # "stutt" for Stuttgart, the usual case while typing
BEGINNING_EARLIER = 0.84  # ... in a word that is not the one being typed
BEGINNING_OF_LONGER = 0.72  # "ulm" in Ulmenstraße, when "Ulm" itself is a word of some name
ABBREVIATION = 0.8       # "sindelfingen" for "Sindelf."
CITY = 0.95              # the city a stop is in, though not in its name
TRAIN_STATION = 0.95     # "Bahnhof" for a stop with trains that isn't called so
INSIDE = 0.7             # "friedhof" in Pragfriedhof
MISSPELT = 0.82          # minus MISSPELT_STEP for every wrong letter
MISSPELT_STEP = 0.12

# how the text score is scaled: [base, importance, nearness]
WEIGHTS_ANYWHERE = (0.55, 0.45, 0.0)
WEIGHTS_NEAR = (0.42, 0.28, 0.30)
TOP_IMPORTANCE = 48.0    # about the highest importance there is (Mannheim Hbf)
MIN_RADIUS_KM = 3.0      # "near" is never less than this around the map's centre
# Beyond that radius nearness fades over this many kilometres (or the radius,
# if that is more): a stop in the next quarter of town is still "here"
# compared with one in another city, also on a map zoomed in on one station.
FADE_KM = 10.0
# A word that begins this many names is a place ("Tübingen", "Durlach"); it
# says where a stop is, not which one, like the city left out of other names
PLACE_WORD_MIN_NAMES = 8
WORTH_SHOWING = 0.4      # a result scoring under this share of the best one is noise
# nobody types more than this for a stop; anything longer is cut, so that
# pasted nonsense can't keep the search busy
MAX_WORDS = 8
MAX_WORD_LENGTH = 40


def fold(text: str) -> str:
    """Lower case, without umlauts, accents and punctuation:
    'Mühlhausen (Stgt.)' -> 'muhlhausen stgt'."""
    text = text.lower().replace("ß", "ss")
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def without_e(word: str) -> str:
    """'muehlhausen' -> 'muhlhausen': how an umlaut is typed on a keyboard
    that has none. Names get the same treatment, so both ways meet."""
    return word.replace("ae", "a").replace("oe", "o").replace("ue", "u")


def same_word(word: str) -> str:
    """One spelling for a word: 'hbf' -> 'hauptbahnhof', 'bismarckstr' ->
    'bismarckstrasse'."""
    if word in SAME_WORD:
        return SAME_WORD[word]
    if len(word) > 5 and word.endswith("str"):
        return word + "asse"
    return word


def words_of(text: str) -> list:
    return [same_word(word) for word in fold(text).split()]


def distance(a: str, b: str, limit: int) -> int:
    """How many letters have to be changed, added, dropped or swapped with a
    neighbour to turn `a` into `b`; `limit + 1` as soon as it is more."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    before, row = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        new = [i]
        for j, cb in enumerate(b, 1):
            cost = min(row[j] + 1, new[j - 1] + 1, row[j - 1] + (ca != cb))
            if before is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cost = min(cost, before[j - 2] + 1)   # two neighbours swapped
            new.append(cost)
        if min(new) > limit:
            return limit + 1
        before, row = row, new
    return row[-1]


def wrong_letters_allowed(word: str) -> int:
    """Short words must be right; the longer a word, the more may be wrong."""
    return 0 if len(word) < 4 else 1 if len(word) < 6 else 2 if len(word) < 9 else 3


def trigrams(word: str) -> list:
    padded = f"  {word} "
    return [padded[i:i + 3] for i in range(len(padded) - 2)]


def kilometres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return 111.0 * math.hypot(lat1 - lat2, (lon1 - lon2) * math.cos(math.radians(lat1)))


def display_name(name: str, stop_id: str) -> str:
    """The name with its city, where the file leaves it out:
    'Pragfriedhof' -> 'Stuttgart, Pragfriedhof'."""
    city = CITIES.get(stop_id.split(":")[1] if stop_id.count(":") >= 2 else "")
    if not city or fold(name).startswith(fold(city)):
        return name
    first, _, rest = name.partition(" ")
    if PLATE_CODES.get(first) == fold(city) and rest:
        name = rest   # "KA Marktplatz" -> "Karlsruhe, Marktplatz"
    return f"{city}, {name}"


def final_score(text: float, importance: float, lat, lon, near: tuple | None, radius_km: float) -> float:
    """How well the words matched (`text`, 0-1), scaled by how important the
    stop is and, with `near`, by how close it is to where the map is."""
    base, by_importance, by_nearness = WEIGHTS_NEAR if near else WEIGHTS_ANYWHERE
    important = min(1.0, math.log1p(max(importance, 0.0)) / math.log1p(TOP_IMPORTANCE))
    nearness = 0.0
    if near and lat is not None and lon is not None:
        radius = max(radius_km, MIN_RADIUS_KM)
        beyond = kilometres(near[0], near[1], lat, lon) - radius
        nearness = 1.0 if beyond <= 0 else 1.0 / (1.0 + beyond / max(radius, FADE_KM))
    return round(text * (base + by_importance * important + by_nearness * nearness), 4)


class StopIndex:
    """All stations, prepared for searching. Built once per stop database."""

    def __init__(self, rows: list):
        self.stops = []
        self.by_id = {}
        self.word_stops = defaultdict(list)   # word -> [(stop number, word's position in the name)]
        self.short_forms = set()              # words that are abbreviations ("sindelf" for "Sindelf.")
        self.city_stops = defaultdict(list)   # city, as typed -> stop numbers
        firsts = Counter(words[0] for words in (words_of(row["name"]) for row in rows) if words)
        self.place_words = {word for word, count in firsts.items() if count >= PLACE_WORD_MIN_NAMES}
        self.place_words.update(word for city in CITIES.values() for word in words_of(city))
        for row in rows:
            self.add(row)
        self.words = sorted(self.word_stops)
        self.remembered = {}                  # (typed word, still typing) -> its matching words
        self.trigram_words = defaultdict(list)
        for number, word in enumerate(self.words):
            for trigram in set(trigrams(word)):
                self.trigram_words[trigram].append(number)

    def add(self, row: dict) -> None:
        number = len(self.stops)
        words, weights = [], []
        in_brackets = False
        for raw in re.findall(r"\(|\)|[^\s,/()\-]+", row["name"]):
            if raw in "()":
                in_brackets = raw == "("
                continue
            for word in fold(raw).split():
                plate = PLATE_CODES.get(raw)
                word = plate or same_word(word)
                if raw.endswith(".") and len(word) >= 3 and word not in SAME_WORD.values() and not plate:
                    self.short_forms.add(word)
                # Words that don't say which stop is meant weigh little: those in
                # brackets ("(oben)", "(U)"), those every other stop has, and
                # the place a name starts with ("Tübingen Hauptbahnhof")
                is_place = not words and word in self.place_words
                words.append(word)
                weights.append(0.3 if in_brackets or is_place or word in MINOR_WORDS else 1.0)
        has_trains = bool(row.get("rail_lines") or row.get("sbahn_lines"))
        if has_trains and "bahnhof" not in words:
            # a stop with trains is a "Bahnhof" whatever it is called
            # ("Ludwigsburg", "Bad Cannstatt", "Ulm Hauptbahnhof")
            words.append("bahnhof")
            weights.append(0.0)    # ... and its name is no longer for that
        for position, word in enumerate(words):
            for form in {word, without_e(word)}:
                self.word_stops[form].append((number, position))
        district = row["id"].split(":")[1] if row["id"].count(":") >= 2 else ""
        if district in CITIES:
            for word in words_of(CITIES[district]):
                self.city_stops[word].append(number)
        self.by_id[row["id"]] = number
        self.stops.append({**row, "words": words, "weights": weights, "trains_only_word": has_trains and weights[-1] == 0.0})

    # --- one typed word ------------------------------------------------------

    def matching_words(self, token: str, still_typing: bool) -> dict:
        """{word of some name: score} for one typed word. Remembered: while
        someone types, the words before the last one come again with every key."""
        key = (token, still_typing)
        found = self.remembered.get(key)
        if found is None:
            found = self.find_matching_words(token, still_typing)
            # (several searches run at once: never read back what another may just have cleared)
            if len(self.remembered) > 5000:
                self.remembered.clear()
            self.remembered[key] = found
        return found

    def find_matching_words(self, token: str, still_typing: bool) -> dict:
        found = {}

        def offer(word, score):
            if score > found.get(word, 0):
                found[word] = score

        forms = {token, without_e(token)}
        is_a_word = any(form in self.word_stops for form in forms)
        for form in forms:
            if form in self.word_stops:
                offer(form, EXACT)
            if len(form) >= 2:
                start = bisect.bisect_left(self.words, form)
                for word in self.words[start:start + 4000]:
                    if not word.startswith(form):
                        break
                    if word != form:
                        offer(word, BEGINNING_OF_LONGER if is_a_word else BEGINNING if still_typing else BEGINNING_EARLIER)
        # spelt right, or the beginning of something: no need to guess at typos
        good = bool(found)
        for form in forms:
            for length in range(3, len(form)):
                if form[:length] in self.short_forms:
                    offer(form[:length], ABBREVIATION)
        if len(token) >= 4:
            for form in {token, without_e(token)}:
                for word in self.words:
                    if form in word and not word.startswith(form):
                        offer(word, INSIDE)
        if not good:
            for word, wrong in self.misspelt(token, still_typing).items():
                offer(word, MISSPELT - MISSPELT_STEP * wrong)
        return found

    def misspelt(self, token: str, still_typing: bool) -> dict:
        """{word: letters wrong} for words `token` may be a misspelling of."""
        limit = wrong_letters_allowed(token)
        if not limit:
            return {}
        found = {}
        for form in {token, without_e(token)}:
            mine = trigrams(form)
            if still_typing:
                mine = mine[:-1]   # the word may go on: its end says nothing
            shared = Counter()
            for trigram in set(mine):
                for number in self.trigram_words.get(trigram, ()):
                    shared[number] += 1
            # one wrong letter spoils at most three trigrams
            needed = max(1, len(set(mine)) - 3 * limit)
            for number, count in shared.items():
                if count < needed:
                    continue
                word = self.words[number]
                wrong = distance(form, word, limit)
                if still_typing and wrong > 1 and len(word) > len(form):
                    # misspelt and not finished: against the beginning of the word
                    wrong = min(wrong, 1 + min(distance(form, word[:len(form) + extra], limit) for extra in (-1, 0, 1)))
                if 0 < wrong <= limit and wrong < found.get(word, limit + 1):
                    found[word] = wrong
        return found

    # --- a whole query ---------------------------------------------------------

    def search(self, query: str, near: tuple | None = None, radius_km: float = 10.0, limit: int = 10) -> list:
        """Up to `limit` stops for `query`, best first, as dicts with the
        stop's row plus `score` and `text` (how well the words matched, 0-1).
        `near` is (lat, lon) of where the map is, `radius_km` how far it shows."""
        tokens = [token[:MAX_WORD_LENGTH] for token in words_of(query)[:MAX_WORDS]]
        if not tokens or len("".join(tokens)) < 2:
            return []
        ends_open = not query.endswith(" ")   # the last word may still be growing
        matched = None                          # stop number -> [score per typed word, {positions matched}]
        for place, token in enumerate(tokens):
            still_typing = ends_open and place == len(tokens) - 1
            here = {}
            for word, score in self.matching_words(token, still_typing).items():
                for number, position in self.word_stops[word]:
                    stop = self.stops[number]
                    if stop["trains_only_word"] and position == len(stop["words"]) - 1:
                        score_here = min(score, TRAIN_STATION)
                    else:
                        score_here = score
                    if score_here > here.get(number, (0, None))[0]:
                        here[number] = (score_here, position)
            for city, numbers in self.city_stops.items():
                score = CITY if city in (token, without_e(token)) else (
                    CITY - 0.1 if still_typing and len(token) >= 3 and city.startswith(token) else 0)
                if score:
                    for number in numbers:
                        if score > here.get(number, (0, None))[0]:
                            here[number] = (score, None)
            if matched is None:
                matched = {number: ([score], {position}) for number, (score, position) in here.items()}
            else:
                matched = {number: (scores + [here[number][0]], positions | {here[number][1]})
                           for number, (scores, positions) in matched.items() if number in here}
            if not matched:
                return []

        results = []
        for number, (scores, positions) in matched.items():
            stop = self.stops[number]
            # A name that is just what was typed beats one that goes on
            # ("Bruchsal" / "Bruchsal Tunnelstraße"): every word of the name that
            # was asked for counts in full, every other one by its weight.
            # ("Bahnhof" for a stop with trains counts like a word of its name:
            # being the station is what was asked for.)
            asked_for = len({p for p in positions if p is not None})
            left_over = sum(weight for p, weight in enumerate(stop["weights"]) if p not in positions)
            text = sum(scores) / len(scores) * (0.85 + 0.15 * (asked_for / (asked_for + left_over) if asked_for else 0.0))
            results.append({**stop, "text": round(text, 4),
                            "score": final_score(text, stop["importance"], stop["lat"], stop["lon"], near, radius_km)})
        results.sort(key=lambda r: (-r["score"], -r["importance"], r["id"]))
        return [r for r in results[:limit] if r["score"] >= WORTH_SHOWING * results[0]["score"]]


_current = {"stamp": None, "index": None}
_lock = threading.Lock()


def current() -> StopIndex:
    """The index of the stop database as it is now; built on first use and
    again after a new import. Raises store.StopsNotImported without one."""
    stamp = store.file_stamp()
    with _lock:
        if _current["stamp"] != stamp:
            _current["index"] = StopIndex(store.stations_for_search())
            _current["stamp"] = stamp
        return _current["index"]
