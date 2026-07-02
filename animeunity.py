"""
AnimeUnity - Client Python autonomo
Traduzione fedele dell'addon CloudStream (Kotlin) originale.

Dipendenze:
    pip install requests beautifulsoup4

Uso rapido:
    from animeunity import AnimeUnity
    au = AnimeUnity()

    # Ricerca
    results = au.search("Naruto")
    for r in results:
        print(r)

    # Carica dettagli anime + lista episodi
    anime = au.load(results[0]["url"])
    print(anime)

    # Ottieni link M3U8 del primo episodio
    link = au.get_episode_link(anime["episodes"][0]["url"])
    print(link)  # Passalo a mpv / yt-dlp
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

MAIN_URL = "https://www.animeunity.so"
CDN_URL  = "https://img.animeunity.so"
ANILIST_URL = "https://graphql.anilist.co"

BASE_HEADERS = {
    "Host": urlparse(MAIN_URL).hostname,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
        "Gecko/20100101 Firefox/133.0"
    ),
}

# Sezioni homepage → parametri API (specchio di getDataPerHomeSection)
HOME_SECTIONS: dict[str, dict] = {
    "In Corso":   {"order": "Popolarità", "status": "In Corso",  "dubbed": 0},
    "Popolari":   {"order": "Popolarità",                        "dubbed": 0},
    "I migliori": {"order": "Valutazione",                       "dubbed": 0},
    "In Arrivo":  {"status": "In Uscita",                        "dubbed": 0},
}

# ---------------------------------------------------------------------------
# Data classes (specchio dei DTO Kotlin)
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    id: int
    anime_id: int
    number: str
    link: str
    scws_id: int
    file_name: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        return cls(
            id=d["id"],
            anime_id=d["anime_id"],
            number=str(d.get("number", "")),
            link=d.get("link", ""),
            scws_id=d.get("scws_id", 0),
            file_name=d.get("file_name"),
        )


@dataclass
class Genre:
    id: int
    name: str

    @classmethod
    def from_dict(cls, d: dict) -> "Genre":
        return cls(id=d["id"], name=d["name"])


@dataclass
class Anime:
    id: int
    title: str | None
    title_eng: str | None
    title_it: str | None
    slug: str
    type: str          # "TV" | "Movie" | "OVA" | ...
    status: str
    plot: str
    date: str
    score: str | None
    dub: int           # 1 = doppiato
    episodes_count: int
    episodes_length: int
    image_url: str
    cover: str | None
    anilist_id: int | None
    mal_id: int | None
    genres: list[Genre]
    episodes: list[Episode]

    @classmethod
    def from_dict(cls, d: dict) -> "Anime":
        return cls(
            id=d["id"],
            title=d.get("title"),
            title_eng=d.get("title_eng"),
            title_it=d.get("title_it"),
            slug=d.get("slug", ""),
            type=d.get("type", ""),
            status=d.get("status", ""),
            plot=d.get("plot", ""),
            date=str(d.get("date", "")),
            score=d.get("score"),
            dub=d.get("dub", 0),
            episodes_count=d.get("episodes_count", 0),
            episodes_length=d.get("episodes_length", 0),
            image_url=d.get("imageurl", ""),
            cover=d.get("cover"),
            anilist_id=d.get("anilist_id"),
            mal_id=d.get("mal_id"),
            genres=[Genre.from_dict(g) for g in d.get("genres", [])],
            episodes=[Episode.from_dict(e) for e in d.get("episodes", [])],
        )

    def display_title(self) -> str:
        return (self.title_it or self.title_eng or self.title or "").replace(" (ITA)", "")

    def media_type(self) -> str:
        if self.type == "TV":
            return "Anime"
        if self.type == "Movie" or self.episodes_count == 1:
            return "Movie"
        return "OVA"

    def is_dubbed(self) -> bool:
        t = self.display_title()
        return self.dub == 1 or "(ITA)" in (self.title_it or self.title_eng or self.title or "")


# ---------------------------------------------------------------------------
# VixCloud extractor (specchio di VixCloudExtractor.kt)
# ---------------------------------------------------------------------------

class VixCloudExtractor:
    """
    Estrae il link M3U8 (HLS) dall'iframe VixCloud.

    Flusso identico al Kotlin:
      1. GET sull'embed_url → documento HTML
      2. Cerca il tag <script> che contiene 'masterPlaylist'
      3. Parsa le assegnazioni window.xxx con regex
      4. Ricostruisce l'URL M3U8 con token + expires (+ &h=1 per FHD)
    """

    HEADERS = {
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
            "Gecko/20100101 Firefox/131.0"
        ),
    }

    def get_m3u8(self, embed_url: str, referer: str = MAIN_URL) -> str | None:
        """Restituisce l'URL M3U8 pronto per essere passato a mpv/yt-dlp."""
        try:
            script_obj = self._get_script_object(embed_url)
            if not script_obj:
                return None
            return self._build_playlist_url(script_obj)
        except Exception as exc:
            print(f"[VixCloud] Errore: {exc}")
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_script_object(self, url: str) -> dict | None:
        resp = requests.get(url, headers=self.HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Trova lo script che contiene 'masterPlaylist' (identico al Kotlin)
        target = None
        for tag in soup.find_all("script"):
            if "masterPlaylist" in (tag.string or ""):
                target = tag.string
                break

        if not target:
            print("[VixCloud] Script con masterPlaylist non trovato")
            return None

        # Normalizza newline → tab (identico al Kotlin)
        target = target.replace("\n", "\t")
        raw_json = self._sanitise_script(target)
        return json.loads(raw_json)

    def _sanitise_script(self, script: str) -> str:
        """
        Replica esatta di getSanitisedScript() Kotlin:
        Splitta sulle assegnazioni window.xxx = ...,
        ricostruisce un JSON object da tutte le variabili.
        """
        pattern = re.compile(r"window\.(\w+)\s*=")

        keys  = pattern.findall(script)
        parts = pattern.split(script)[1:]          # drop leading empty string

        # parts alterna: chiave, valore, chiave, valore, ...
        # ma findall+split ci danno già keys e parti di valore intercalate
        # Kotlin usa split+drop(1), noi facciamo lo stesso con re.split
        raw_parts = re.split(r"window\.\w+\s*=", script)[1:]

        json_members = []
        for key, value in zip(keys, raw_parts):
            cleaned = value
            cleaned = cleaned.replace(";", "")
            # Quota le chiavi degli oggetti JS (es. {token: "abc"} → {"token": "abc"})
            cleaned = re.sub(r'([{\[,])\s*(\w+)\s*:', r'\1 "\2":', cleaned)
            # Rimuove trailing comma prima di } o ]
            cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
            cleaned = cleaned.strip()
            json_members.append(f'"{key}": {cleaned}')

        final = "{\n" + ",\n".join(json_members) + "\n}"
        # Sostituisce apici singoli con doppi (JS → JSON)
        final = final.replace("'", '"')
        return final

    def _build_playlist_url(self, obj: dict) -> str:
        """
        Replica di getPlaylistLink() Kotlin:
        Legge masterPlaylist.url + params.token + params.expires,
        aggiunge &h=1 se canPlayFHD è true.
        """
        master    = obj["masterPlaylist"]
        params    = master["params"]
        token     = params["token"]
        expires   = params["expires"]
        base_url  = master["url"]

        qs = f"token={token}&expires={expires}"

        if "?b" in base_url:
            playlist_url = base_url.replace("?b:1", "?b=1") + "&" + qs
        else:
            playlist_url = base_url + "?" + qs

        if obj.get("canPlayFHD"):
            playlist_url += "&h=1"

        return playlist_url


# ---------------------------------------------------------------------------
# Client principale AnimeUnity
# ---------------------------------------------------------------------------

class AnimeUnity:
    """
    Client Python autonomo per AnimeUnity.

    Metodi pubblici
    ---------------
    search(query)           → lista di dict con info di base
    get_homepage(section)   → lista di dict (sezioni: vedi HOME_SECTIONS)
    load(url)               → dict completo con metadata + lista episodi
    get_episode_link(url)   → str M3U8 da passare a mpv / yt-dlp
    get_anilist_poster(id)  → str URL immagine da AniList (fallback)
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self._headers: dict[str, str] = dict(BASE_HEADERS)
        self._authenticated = False

    # ------------------------------------------------------------------
    # Auth — specchio di setupHeadersAndCookies() + resetHeadersAndCookies()
    # ------------------------------------------------------------------

    def _reset_headers(self) -> None:
        """Specchio di resetHeadersAndCookies()"""
        self._headers = dict(BASE_HEADERS)
        self._authenticated = False

    def _setup_auth(self) -> None:
        """
        Specchio di setupHeadersAndCookies():
        1. GET /archivio  → estrae csrf-token dal <meta> + cookie di sessione
        2. Aggiunge X-CSRF-Token, Cookie, X-Requested-With agli header
        """
        resp = self.session.get(
            f"{MAIN_URL}/archivio",
            headers=self._headers,
            timeout=20,
        )
        soup = BeautifulSoup(resp.text, "html.parser")

        csrf = soup.head.find("meta", {"name": "csrf-token"})
        csrf_token = csrf["content"] if csrf else ""

        xsrf    = resp.cookies.get("XSRF-TOKEN", "")
        session = resp.cookies.get("animeunity_session", "")
        cookie_str = f"XSRF-TOKEN={xsrf}; animeunity_session={session}"

        self._headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json;charset=utf-8",
            "X-CSRF-Token": csrf_token,
            "Referer": MAIN_URL,
            "Cookie": cookie_str,
        })
        self._authenticated = True

    def _ensure_auth(self) -> None:
        if not self._authenticated:
            self._reset_headers()
            self._setup_auth()

    # ------------------------------------------------------------------
    # Immagini — specchio di getImage() / getBanner() / getAnilistPoster()
    # ------------------------------------------------------------------

    def _get_image(self, image_url: str | None, anilist_id: int | None) -> str | None:
        """Specchio di getImage()"""
        if image_url:
            filename = image_url.rsplit("/", 1)[-1]
            return f"{CDN_URL}/anime/{filename}"
        if anilist_id:
            return self.get_anilist_poster(anilist_id)
        return None

    def _get_banner(self, image_url: str) -> str:
        """Specchio di getBanner()"""
        if image_url:
            filename  = image_url.rsplit("/", 1)[-1]
            cdn_host  = urlparse(MAIN_URL).hostname.replace("www", "img")
            return f"https://{cdn_host}/anime/{filename}"
        return image_url

    def get_anilist_poster(self, anilist_id: int) -> str | None:
        """Specchio di getAnilistPoster() — fallback via GraphQL AniList"""
        query = """
        query ($id: Int) {
            Media(id: $id, type: ANIME) {
                coverImage { large medium }
            }
        }
        """
        body = {"query": query, "variables": {"id": anilist_id}}
        try:
            resp = requests.post(ANILIST_URL, json=body, timeout=10)
            data = resp.json()
            cover = data["data"]["Media"]["coverImage"]
            return cover.get("large") or cover.get("medium")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Costruzione request body — specchio di RequestData.toRequestBody()
    # ------------------------------------------------------------------

    def _build_request_body(
        self,
        title: str = "",
        order: str | bool = False,
        status: str | bool = False,
        dubbed: int = 1,
        offset: int = 0,
        type_: str | bool = False,
        year: str | bool = False,
        genres: str | bool = False,
        season: str | bool = False,
    ) -> str:
        """
        Replica esatta di RequestData.toJson():
        I campi non impostati vengono serializzati come false (bool JS),
        non come null — è questo che si aspetta l'API.
        """
        payload = {
            "title":   title,
            "type":    type_,
            "year":    year,
            "order":   order,
            "status":  status,
            "genres":  genres,
            "season":  season,
            "dubbed":  dubbed,
            "offset":  offset,
        }
        return json.dumps(payload)

    # ------------------------------------------------------------------
    # Homepage — specchio di getMainPage() + getDataPerHomeSection()
    # ------------------------------------------------------------------

    def get_homepage(
        self,
        section: str = "Popolari",
        page: int = 1,
    ) -> list[dict]:
        """
        Restituisce fino a 30 anime per pagina per la sezione richiesta.
        Sezioni disponibili: "In Corso", "Popolari", "I migliori", "In Arrivo"
        """
        self._ensure_auth()

        cfg    = HOME_SECTIONS.get(section, {})
        offset = (page - 1) * 30
        body   = self._build_request_body(
            order=cfg.get("order", False),
            status=cfg.get("status", False),
            dubbed=cfg.get("dubbed", 1),
            offset=offset,
        )

        resp = self.session.post(
            f"{MAIN_URL}/archivio/get-animes",
            headers=self._headers,
            data=body,
            timeout=20,
        )
        data   = resp.json()
        titles = data.get("records", [])
        return [self._anime_to_dict(Anime.from_dict(a)) for a in titles]

    # ------------------------------------------------------------------
    # Ricerca — specchio di search()
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[dict]:
        """Cerca anime per titolo. Restituisce lista di dict con info di base."""
        self._reset_headers()
        self._setup_auth()

        body = self._build_request_body(title=query, dubbed=0)
        resp = self.session.post(
            f"{MAIN_URL}/archivio/get-animes",
            headers=self._headers,
            data=body,
            timeout=20,
        )
        data   = resp.json()
        titles = data.get("records", [])
        return [self._anime_to_dict(Anime.from_dict(a)) for a in titles]

    # ------------------------------------------------------------------
    # Load — specchio di load()
    # ------------------------------------------------------------------

    def load(self, url: str) -> dict:
        """
        Carica la pagina di un anime e restituisce tutte le informazioni
        (metadata + lista episodi completa).

        Per serie con più di 120 episodi chiama /info_api/ a blocchi di 120
        (identico al Kotlin).
        """
        self._reset_headers()
        self._setup_auth()

        resp = self.session.get(url, headers=self._headers, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        # --- Anime correlati (layout-items[items-json]) ---
        layout_items = soup.find("layout-items")
        related_raw  = layout_items["items-json"] if layout_items else "[]"
        related_list = _parse_attr_json(related_raw)
        related      = [Anime.from_dict(a) for a in related_list]

        # --- Dati anime e primo blocco episodi (video-player attrs) ---
        vp      = soup.find("video-player")
        anime   = Anime.from_dict(_parse_attr_json(vp["anime"]))
        eps_raw = _parse_attr_json(vp["episodes"])
        total_eps   = int(vp["episodes_count"])
        episodes    = [Episode.from_dict(e) for e in eps_raw]

        # --- Episodi aggiuntivi (>120) via /info_api/ ---
        if total_eps > 120:
            chunks = total_eps // 120 + (1 if total_eps % 120 != 0 else 0)
            for i in range(2, chunks + 1):
                end_range = total_eps if i == chunks else i * 120
                start_range = 1 + (i - 1) * 120
                info_url = (
                    f"{MAIN_URL}/info_api/{anime.id}/1"
                    f"?start_range={start_range}&end_range={end_range}"
                )
                info_resp = self.session.get(info_url, timeout=20)
                info_data = info_resp.json()
                episodes.extend(
                    Episode.from_dict(e) for e in info_data.get("episodes", [])
                )

        # --- Build risposta ---
        title   = anime.display_title()
        is_dub  = anime.is_dubbed()

        return {
            "title":       title,
            "url":         url,
            "type":        anime.media_type(),
            "plot":        anime.plot,
            "year":        anime.date[:4] if anime.date else None,
            "score":       anime.score,
            "dubbed":      is_dub,
            "language":    "🇮🇹 Italiano" if is_dub else "🇯🇵 Giapponese",
            "duration_min": anime.episodes_length,
            "status":      anime.status,
            "coming_soon": anime.status == "In uscita prossimamente",
            "genres":      [g.name.capitalize() for g in anime.genres],
            "anilist_id":  anime.anilist_id,
            "mal_id":      anime.mal_id,
            "poster":      self._get_image(anime.image_url, anime.anilist_id),
            "banner":      self._get_banner(anime.cover) if anime.cover else None,
            "related":     [self._anime_to_dict(r) for r in related],
            # Lista episodi: ogni elemento ha url pronto per get_episode_link()
            "episodes": [
                {
                    "number":   ep.number,
                    "id":       ep.id,
                    "url":      f"{url}/{ep.id}",
                    "scws_id":  ep.scws_id,
                }
                for ep in sorted(episodes, key=lambda e: _ep_sort_key(e.number))
            ],
        }

    # ------------------------------------------------------------------
    # loadLinks — specchio di loadLinks()
    # ------------------------------------------------------------------

    def get_episode_link(self, episode_url: str) -> str | None:
        """
        Dato l'URL di un episodio (es. .../anime/123-slug/456),
        restituisce il link M3U8 da passare a mpv / yt-dlp.

        Flusso identico a loadLinks() Kotlin:
          1. GET sull'URL episodio → estrae embed_url da video-player
          2. Passa embed_url a VixCloudExtractor
        """
        resp = self.session.get(episode_url, headers=self._headers, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        vp        = soup.find("video-player")
        embed_url = vp["embed_url"] if vp else None

        if not embed_url:
            print(f"[AnimeUnity] embed_url non trovato in {episode_url}")
            return None

        return VixCloudExtractor().get_m3u8(embed_url, referer=MAIN_URL)

    # ------------------------------------------------------------------
    # Helper interni
    # ------------------------------------------------------------------

    def _anime_to_dict(self, anime: Anime) -> dict:
        """Converte un Anime dataclass in dict serializzabile per l'output."""
        return {
            "id":     anime.id,
            "title":  anime.display_title(),
            "url":    f"{MAIN_URL}/anime/{anime.id}-{anime.slug}",
            "type":   anime.media_type(),
            "dubbed": anime.is_dubbed(),
            "poster": self._get_image(anime.image_url, anime.anilist_id),
            "score":  anime.score,
            "status": anime.status,
        }


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def _parse_attr_json(raw: str) -> Any:
    """
    Parsa il JSON estratto da un attributo HTML di un tag Vue/custom.

    BeautifulSoup decodifica già le entità HTML standard (&quot; → ", &amp; → &)
    quando leggi un attributo con tag["attr"]. NON chiamare unescape() dopo,
    altrimenti si applica una doppia decodifica che corrompe il JSON.

    Se json.loads() fallisce comunque (raro: alcuni server usano entità miste
    o escape JS come \\u003c), prova a pulire i residui più comuni.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Fallback: rimuovi escape JS residui che non sono JSON standard
    # (es. \/ → /, \' → ' nel caso di apici singoli in mezzo ai valori)
    cleaned = raw.replace("\\/", "/").replace("\\'", "'")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Impossibile parsare JSON dall'attributo HTML. "
            f"Errore: {e}. "
            f"Primi 200 chars: {raw[:200]!r}"
        ) from e


def _ep_sort_key(number: str):
    """Ordina episodi numericamente (gestisce decimali tipo '5.5')."""
    try:
        return float(number)
    except ValueError:
        return float("inf")


# ---------------------------------------------------------------------------
# CLI minimale — uso da riga di comando
# ---------------------------------------------------------------------------

def _cli() -> None:
    import sys

    au = AnimeUnity()

    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUSO:")
        print("  python animeunity.py search <query>")
        print("  python animeunity.py home [sezione]")
        print("  python animeunity.py load <url_anime>     # mostra episodi e chiede quale riprodurre")
        print("  python animeunity.py play <url_episodio>  # estrae direttamente il link M3U8")
        print()
        print("Sezioni home: 'In Corso', 'Popolari', 'I migliori', 'In Arrivo'")
        return

    cmd  = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "search":
        query   = " ".join(args) or "one piece"
        results = au.search(query)
        print(f"\n🔍 Risultati per '{query}':\n")
        for i, r in enumerate(results):
            dub = "🇮🇹" if r["dubbed"] else "🇯🇵"
            print(f"  [{i:2d}] {dub} {r['title']:40s}  ({r['type']})  ⭐ {r['score'] or '—'}")
            print(f"        {r['url']}")
        return

    if cmd == "home":
        section = args[0] if args else "Popolari"
        results = au.get_homepage(section)
        print(f"\n🏠 Homepage — {section}:\n")
        for i, r in enumerate(results):
            dub = "🇮🇹" if r["dubbed"] else "🇯🇵"
            print(f"  [{i:2d}] {dub} {r['title']:40s}  ({r['type']})  ⭐ {r['score'] or '—'}")
        return

    if cmd == "load":
        if not args:
            print("Specifica un URL anime")
            return
        _cmd_load(au, args[0])
        return

    if cmd == "play":
        if not args:
            print("Specifica l'URL di un episodio")
            return
        _cmd_play(au, args[0])
        return

    print(f"Comando sconosciuto: {cmd}")


# ---------------------------------------------------------------------------
# Funzioni CLI — load interattivo e play
# ---------------------------------------------------------------------------

def _cmd_load(au: "AnimeUnity", url: str) -> None:
    """
    Mostra i dettagli dell'anime, elenca tutti gli episodi e chiede
    quale riprodurre. Supporta anche range (es. 5-10) e 'tutti'.
    """
    print(f"\n⏳ Caricamento {url} ...")
    info = au.load(url)
    eps  = info["episodes"]

    # ── Header ──────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"📺  {info['title']}  ({info['year'] or '—'})")
    print(f"{'─'*60}")
    print(f"  Tipo     : {info['type']}  |  {info['language']}")
    print(f"  Score    : {info['score'] or '—'}  |  Status: {info['status']}")
    print(f"  Generi   : {', '.join(info['genres']) or '—'}")
    print(f"  Trama    : {(info['plot'] or '—')[:120]}{'…' if len(info['plot'] or '') > 120 else ''}")
    print(f"  Poster   : {info['poster'] or '—'}")
    print(f"{'─'*60}\n")

    if not eps:
        print("  ⚠️  Nessun episodio disponibile.")
        return

    # ── Lista episodi ────────────────────────────────────────────────────────
    _print_episode_list(eps)

    # ── Selezione interattiva ────────────────────────────────────────────────
    print("\nScegli episodio/i da riprodurre:")
    print("  • Numero singolo   →  5")
    print("  • Range            →  5-10")
    print("  • Tutti            →  tutti  (o 'all')")
    print("  • Annulla          →  q / invio vuoto\n")

    while True:
        try:
            raw = input("Episodio: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAnnullato.")
            return

        if raw in ("", "q", "quit", "exit"):
            return

        selected = _parse_episode_selection(raw, eps)
        if selected is None:
            print(f"  ❌ Input non valido. Esempi: '5', '5-10', 'tutti'")
            continue
        if not selected:
            print(f"  ❌ Nessun episodio trovato per '{raw}'")
            continue
        break

    # ── Riproduzione ─────────────────────────────────────────────────────────
    for ep in selected:
        _cmd_play(au, ep["url"], label=f"Ep. {ep['number']}")


def _print_episode_list(eps: list[dict]) -> None:
    """Stampa la lista episodi in colonne da 10 per riga."""
    total = len(eps)
    print(f"  Episodi disponibili ({total} totali):\n")
    cols  = 10
    width = 6   # larghezza di ogni cella
    for row_start in range(0, total, cols):
        row = eps[row_start:row_start + cols]
        print("  " + "".join(f"[{e['number']:>4}]" for e in row))
    print()


def _parse_episode_selection(raw: str, eps: list[dict]) -> list[dict] | None:
    """
    Interpreta la stringa di selezione dell'utente e restituisce
    la lista di episodi corrispondenti.

    Formati supportati:
      "tutti" / "all"  → tutti gli episodi
      "5"              → episodio numero 5
      "5-10"           → episodi 5, 6, 7, 8, 9, 10 (per numero display)
    """
    if raw in ("tutti", "all"):
        return eps

    # Range  (es. "5-10")
    range_match = re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", raw)
    if range_match:
        start = float(range_match.group(1))
        end   = float(range_match.group(2))
        result = [e for e in eps if start <= _ep_sort_key(e["number"]) <= end]
        return result if result is not None else []

    # Numero singolo (es. "5" o "5.5")
    try:
        target = float(raw)
        result = [e for e in eps if _ep_sort_key(e["number"]) == target]
        return result
    except ValueError:
        return None


def _cmd_play(au: "AnimeUnity", episode_url: str, label: str = "") -> None:
    """Estrae il link M3U8 e lo stampa pronto per mpv / yt-dlp."""
    tag = f"[{label}] " if label else ""
    print(f"\n⏳ {tag}Estrazione link M3U8 ...")
    link = au.get_episode_link(episode_url)
    if link:
        print(f"✅ {tag}M3U8 pronto:\n")
        print(f"   {link}\n")
        print(f"   ▶  mpv \"{link}\"")
        print(f"   ▶  yt-dlp \"{link}\"")
    else:
        print(f"❌ {tag}Link non trovato per {episode_url}")


if __name__ == "__main__":
    _cli()
