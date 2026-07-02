"""
StreamingCommunity — Client Python autonomo
Traduzione fedele dell'addon CloudStream (Kotlin) originale.

Il sito usa Inertia.js: ogni pagina HTML contiene un attributo
#app[data-page] con il JSON completo dei dati (props). Le chiamate
AJAX successive mandano header X-Inertia e ricevono JSON direttamente.

Dipendenze:
    pip install requests beautifulsoup4

Uso rapido:
    from streamingcommunity import StreamingCommunity
    sc = StreamingCommunity()

    results = sc.search("Breaking Bad")
    info    = sc.load(results[0]["url"])
    link    = sc.get_episode_link(info["episodes"][0])   # serie TV
    link    = sc.get_movie_link(info)                    # film

CLI:
    python streamingcommunity.py search <query>
    python streamingcommunity.py load <url>
    python streamingcommunity.py home
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

MAIN_URL = "https://streamingunity.dog/"   # trailing slash come nel Kotlin
LANG     = "it"
BASE_URL = MAIN_URL + LANG                  # https://streamingunity.dog/it

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"

# Slider da richiedere alla homepage (identici al Kotlin, batch da 6)
SLIDER_DEFINITIONS = [
    {"name": "top10",    "genre": None},
    {"name": "trending", "genre": None},
    {"name": "latest",   "genre": None},
    {"name": "upcoming", "genre": None},
    {"name": "genre",    "genre": "Animation"},
    {"name": "genre",    "genre": "Adventure"},
    {"name": "genre",    "genre": "Action"},
    {"name": "genre",    "genre": "Comedy"},
    {"name": "genre",    "genre": "Crime"},
    {"name": "genre",    "genre": "Documentary"},
    {"name": "genre",    "genre": "Drama"},
    {"name": "genre",    "genre": "Family"},
    {"name": "genre",    "genre": "Science Fiction"},
    {"name": "genre",    "genre": "Fantasy"},
    {"name": "genre",    "genre": "Horror"},
    {"name": "genre",    "genre": "Reality"},
    {"name": "genre",    "genre": "Romance"},
    {"name": "genre",    "genre": "Thriller"},
]

# ---------------------------------------------------------------------------
# Data classes — specchio dei DTO Kotlin
# ---------------------------------------------------------------------------

@dataclass
class PosterImage:
    filename: str
    type: str       # "poster" | "background" | "cover"

    @classmethod
    def from_dict(cls, d: dict) -> "PosterImage":
        return cls(filename=d.get("filename", ""), type=d.get("type", ""))


@dataclass
class Genre:
    id: int
    name: str
    type: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Genre":
        return cls(id=d["id"], name=d["name"], type=d.get("type", ""))


@dataclass
class MainActor:
    id: int
    name: str

    @classmethod
    def from_dict(cls, d: dict) -> "MainActor":
        return cls(id=d["id"], name=d["name"])


@dataclass
class Trailer:
    id: int
    name: str | None
    youtube_id: str | None
    title_id: int | None

    @classmethod
    def from_dict(cls, d: dict) -> "Trailer":
        return cls(
            id=d["id"],
            name=d.get("name"),
            youtube_id=d.get("youtube_id"),
            title_id=d.get("title_id"),
        )

    def youtube_url(self) -> str | None:
        if not self.youtube_id:
            return None
        return f"https://www.youtube.com/watch?v={self.youtube_id}"


@dataclass
class Episode:
    id: int
    number: int
    name: str
    plot: str | None
    duration: int | None
    scws_id: int
    season_id: int
    images: list[PosterImage]

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        return cls(
            id=d["id"],
            number=d["number"],
            name=d.get("name", ""),
            plot=d.get("plot"),
            duration=d.get("duration"),
            scws_id=d.get("scws_id", 0),
            season_id=d.get("season_id", 0),
            images=[PosterImage.from_dict(i) for i in d.get("images", [])],
        )

    def cover(self) -> str | None:
        for img in self.images:
            if img.type == "cover":
                return img.filename
        return None


@dataclass
class Season:
    id: int
    number: int
    name: str | None
    plot: str | None
    release_date: str | None
    title_id: int
    episodes: list[Episode]

    @classmethod
    def from_dict(cls, d: dict) -> "Season":
        return cls(
            id=d["id"],
            number=d["number"],
            name=d.get("name"),
            plot=d.get("plot"),
            release_date=d.get("release_date"),
            title_id=d.get("title_id", 0),
            episodes=[Episode.from_dict(e) for e in d.get("episodes") or []],
        )


@dataclass
class Title:
    """Titolo minimale usato nelle liste (ricerca, slider)."""
    id: int
    name: str
    slug: str
    type: str   # "movie" | "tv"
    images: list[PosterImage]

    @classmethod
    def from_dict(cls, d: dict) -> "Title":
        return cls(
            id=d["id"],
            name=d["name"],
            slug=d["slug"],
            type=d.get("type", ""),
            images=[PosterImage.from_dict(i) for i in d.get("images", [])],
        )

    def poster(self) -> str | None:
        for img in self.images:
            if img.type == "poster":
                return img.filename
        return None


@dataclass
class TitleProp:
    """Titolo completo usato nella pagina dettaglio (props.title)."""
    id: int
    name: str
    slug: str
    plot: str | None
    quality: str | None
    type: str | None
    score: str | None
    release_date: str | None
    status: str | None
    age: int | None
    runtime: int | None
    tmdb_id: int | None
    imdb_id: str | None
    seasons_count: int | None
    scws_id: int | None
    trailers: list[Trailer]
    seasons: list[Season]
    images: list[PosterImage]
    genres: list[Genre]
    main_actors: list[MainActor]

    @classmethod
    def from_dict(cls, d: dict) -> "TitleProp":
        return cls(
            id=d["id"],
            name=d["name"],
            slug=d["slug"],
            plot=d.get("plot"),
            quality=d.get("quality"),
            type=d.get("type"),
            score=d.get("score"),
            release_date=d.get("release_date"),
            status=d.get("status"),
            age=d.get("age"),
            runtime=d.get("runtime"),
            tmdb_id=d.get("tmdb_id"),
            imdb_id=d.get("imdb_id"),
            seasons_count=d.get("seasons_count"),
            scws_id=d.get("scws_id"),
            trailers=[Trailer.from_dict(t) for t in d.get("trailers") or []],
            seasons=[Season.from_dict(s) for s in d.get("seasons") or []],
            images=[PosterImage.from_dict(i) for i in d.get("images", [])],
            genres=[Genre.from_dict(g) for g in d.get("genres", [])],
            main_actors=[MainActor.from_dict(a) for a in d.get("main_actors") or []],
        )

    def background(self) -> str | None:
        for img in self.images:
            if img.type == "background":
                return img.filename
        return None

    def poster_image(self) -> str | None:
        for img in self.images:
            if img.type == "poster":
                return img.filename
        return None


@dataclass
class Slider:
    name: str
    label: str
    titles: list[Title]

    @classmethod
    def from_dict(cls, d: dict) -> "Slider":
        return cls(
            name=d.get("name", ""),
            label=d.get("label", ""),
            titles=[Title.from_dict(t) for t in d.get("titles", [])],
        )


@dataclass
class Props:
    scws_url: str
    cdn_url: str
    title: TitleProp | None
    loaded_season: Season | None
    sliders: list[Slider]
    genres: list[Genre]
    titles: list[Title]   # solo nelle ricerche

    @classmethod
    def from_dict(cls, d: dict) -> "Props":
        ls = d.get("loadedSeason")
        return cls(
            scws_url=d.get("scws_url", ""),
            cdn_url=d.get("cdn_url", ""),
            title=TitleProp.from_dict(d["title"]) if d.get("title") else None,
            loaded_season=Season.from_dict(ls) if ls else None,
            sliders=[Slider.from_dict(s) for s in d.get("sliders") or []],
            genres=[Genre.from_dict(g) for g in d.get("genres") or []],
            titles=[Title.from_dict(t) for t in d.get("titles") or []],
        )


@dataclass
class InertiaResponse:
    props: Props
    url: str
    version: str

    @classmethod
    def from_dict(cls, d: dict) -> "InertiaResponse":
        return cls(
            props=Props.from_dict(d["props"]),
            url=d.get("url", ""),
            version=d.get("version", ""),
        )


# ---------------------------------------------------------------------------
# VixCloud extractor — identico ad AnimeUnity, con Cloudflare bypass (session)
# ---------------------------------------------------------------------------

class VixCloudExtractor:
    """
    Estrae il link M3U8 dall'iframe VixCloud.
    Specchio di VixCloudExtractor.kt (versione StreamingCommunity).

    La differenza rispetto ad AnimeUnity: il Kotlin usa CloudflareKiller
    (WebView) per bypassare Cloudflare sull'iframe. Qui usiamo la stessa
    session autenticata con i cookie già ottenuti, che nella pratica è
    sufficiente perché i cookie di sessione del sito principale vengono
    accettati anche da VixCloud.
    """

    HEADERS = {
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "User-Agent": UA,
    }

    def __init__(self, session: requests.Session | None = None):
        # Accetta la session esterna per portare i cookie di autenticazione
        self.session = session or requests.Session()

    def get_m3u8(self, embed_url: str, referer: str = MAIN_URL) -> str | None:
        try:
            script_obj = self._get_script_object(embed_url)
            if not script_obj:
                return None
            return self._build_playlist_url(script_obj)
        except Exception as exc:
            print(f"[VixCloud] Errore: {exc}")
            return None

    def _get_script_object(self, url: str) -> dict | None:
        h = dict(self.HEADERS)
        resp = self.session.get(url, headers=h, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        target = None
        for tag in soup.find_all("script"):
            if "masterPlaylist" in (tag.string or ""):
                target = tag.string
                break

        if not target:
            print("[VixCloud] Script con masterPlaylist non trovato")
            return None

        target = target.replace("\n", "\t")
        raw_json = self._sanitise_script(target)
        return json.loads(raw_json)

    def _sanitise_script(self, script: str) -> str:
        """Replica esatta di getSanitisedScript() Kotlin."""
        keys      = re.findall(r"window\.(\w+)\s*=", script)
        raw_parts = re.split(r"window\.\w+\s*=", script)[1:]

        members = []
        for key, value in zip(keys, raw_parts):
            cleaned = value
            cleaned = cleaned.replace(";", "")
            cleaned = re.sub(r'([{\[,])\s*(\w+)\s*:', r'\1 "\2":', cleaned)
            cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
            cleaned = cleaned.strip()
            members.append(f'"{key}": {cleaned}')

        final = "{\n" + ",\n".join(members) + "\n}"
        final = final.replace("'", '"')
        return final

    def _build_playlist_url(self, obj: dict) -> str:
        master   = obj["masterPlaylist"]
        params   = master["params"]
        token    = params["token"]
        expires  = params["expires"]
        base_url = master["url"]

        qs = f"token={token}&expires={expires}"

        if "?b" in base_url:
            url = base_url.replace("?b:1", "?b=1") + "&" + qs
        else:
            url = base_url + "?" + qs

        if obj.get("canPlayFHD"):
            url += "&h=1"

        return url


# ---------------------------------------------------------------------------
# Client principale StreamingCommunity
# ---------------------------------------------------------------------------

class StreamingCommunity:
    """
    Client Python autonomo per StreamingCommunity.

    Metodi pubblici
    ---------------
    search(query, page)      → lista di dict
    get_homepage()           → dict {section_name: [titoli]}
    load(url)                → dict completo con metadata
    get_episode_link(ep)     → str M3U8  (per serie TV)
    get_movie_link(info)     → str M3U8  (per film)
    """

    def __init__(self, lang: str = LANG) -> None:
        self.lang        = lang
        self.base_url    = MAIN_URL + lang          # es. https://streamingunity.dog/it
        self.session     = requests.Session()
        self.session.headers.update({"User-Agent": UA})

        # Stato auth — specchio dei companion object Kotlin
        self._inertia_version   = ""
        self._xsrf_token        = ""   # decodedXsrfToken (già URL-decoded)
        self._authenticated     = False

    # ------------------------------------------------------------------
    # Auth — specchio di setupHeaders()
    # ------------------------------------------------------------------

    def _setup_auth(self) -> None:
        """
        Specchio di setupHeaders():
        1. GET /archive  → raccoglie cookie + estrae inertiaVersion da #app[data-page]
        2. GET /sanctum/csrf-cookie  → ottiene XSRF-TOKEN aggiornato
        """
        # Step 1: pagina archivio
        r1 = self.session.get(
            f"{self.base_url}/archive",
            headers={"Referer": self.base_url + "/"},
            timeout=20,
        )
        soup = BeautifulSoup(r1.text, "html.parser")
        app_tag = soup.select_one("#app")
        if app_tag:
            data_page = app_tag.get("data-page", "")
            # Estrae version dal JSON inline (identico al substringAfter Kotlin)
            m = re.search(r'"version":"([^"]+)"', data_page)
            if m:
                self._inertia_version = m.group(1)

        # Step 2: csrf-cookie (aggiorna XSRF-TOKEN)
        self.session.get(
            f"{MAIN_URL}sanctum/csrf-cookie",
            headers={
                "Referer": self.base_url + "/",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20,
        )

        # Decodifica XSRF-TOKEN (identico a URLDecoder.decode nel Kotlin)
        raw_xsrf = self.session.cookies.get("XSRF-TOKEN", "")
        self._xsrf_token = unquote(raw_xsrf)
        self._authenticated = True

    def _ensure_auth(self) -> None:
        if not self._authenticated:
            self._setup_auth()

    # ------------------------------------------------------------------
    # Header helpers — specchio di getSliderFetchHeaders()
    # ------------------------------------------------------------------

    def _inertia_headers(self) -> dict:
        """Header per richieste Inertia.js standard (GET pagine)."""
        return {
            "X-Inertia": "true",
            "X-Inertia-Version": self._inertia_version,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }

    def _slider_headers(self) -> dict:
        """Header per POST /api/sliders/fetch — specchio di getSliderFetchHeaders()."""
        return {
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": self._xsrf_token,
            "Referer": self.base_url + "/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": MAIN_URL.rstrip("/"),
        }

    # ------------------------------------------------------------------
    # Parsing Inertia — specchio di parseInertiaPayload() / parseBrowseTitles()
    # ------------------------------------------------------------------

    def _is_html(self, payload: str) -> bool:
        t = payload.lstrip()
        return t.startswith("<") or "<!DOCTYPE" in t[:100].upper()

    def _extract_inertia_json(self, html: str) -> str | None:
        """
        Specchio di extractInertiaPageJson():
        Legge #app[data-page] dalla risposta HTML (fallback quando
        il server manda HTML invece di JSON).
        BS4 decodifica già le entità HTML nell'attributo.
        """
        soup = BeautifulSoup(html, "html.parser")
        app  = soup.select_one("#app")
        if not app:
            return None
        return app.get("data-page")

    def _parse_inertia(self, payload: str) -> InertiaResponse | None:
        """Specchio di parseInertiaPayload()."""
        if not payload or not payload.strip():
            return None
        if self._is_html(payload):
            # Fallback: estrai JSON embedded nell'HTML
            extracted = self._extract_inertia_json(payload)
            if not extracted:
                return None
            payload = extracted
        try:
            return InertiaResponse.from_dict(json.loads(payload))
        except Exception as e:
            print(f"[SC] Errore parsing Inertia JSON: {e}")
            return None

    def _parse_titles_from_response(self, payload: str) -> list[Title]:
        """Specchio di parseBrowseTitles() — usato in search."""
        result = self._parse_inertia(payload)
        if not result:
            return []
        return result.props.titles

    # ------------------------------------------------------------------
    # CDN helper
    # ------------------------------------------------------------------

    def _cdn_url(self) -> str:
        """Ricava il dominio CDN dal base_url (es. cdn.streamingunity.dog)."""
        host = urlparse(self.base_url).hostname or ""
        return f"https://cdn.{host}"

    def _poster_url(self, filename: str | None) -> str | None:
        if not filename:
            return None
        return f"{self._cdn_url()}/images/{filename}"

    def _title_to_dict(self, title: Title) -> dict:
        return {
            "id":     title.id,
            "name":   title.name,
            "slug":   title.slug,
            "type":   title.type,
            "url":    f"{self.base_url}/titles/{title.id}-{title.slug}",
            "poster": self._poster_url(title.poster()),
        }

    # ------------------------------------------------------------------
    # Homepage — specchio di getMainPage() + fetchSliderSectionsInBatches()
    # ------------------------------------------------------------------

    def get_homepage(self) -> dict[str, list[dict]]:
        """
        Restituisce tutte le sezioni homepage come dict {label: [titoli]}.
        Fa le stesse chiamate POST a /api/sliders/fetch in batch da 6
        identiche al Kotlin.
        """
        self._ensure_auth()

        sections: dict[str, list[dict]] = {}
        batch_size = 6

        for i in range(0, len(SLIDER_DEFINITIONS), batch_size):
            batch   = SLIDER_DEFINITIONS[i:i + batch_size]
            payload = json.dumps({"sliders": batch})
            resp    = self.session.post(
                f"{MAIN_URL}api/sliders/fetch?lang={self.lang}",
                data=payload,
                headers=self._slider_headers(),
                timeout=20,
            )
            raw = resp.text.strip()

            # Gestione errori identica a parseSliderFetchSections()
            if not raw or self._is_html(raw):
                print(f"[SC] Slider batch {i//batch_size+1}: risposta HTML inattesa")
                continue
            if raw.startswith("{") or '"message"' in raw:
                print(f"[SC] Slider batch {i//batch_size+1}: errore server: {raw[:200]}")
                continue

            try:
                sliders = [Slider.from_dict(s) for s in json.loads(raw)]
            except Exception as e:
                print(f"[SC] Slider batch {i//batch_size+1}: parsing fallito: {e}")
                continue

            for slider in sliders:
                label = slider.label or slider.name
                items = [self._title_to_dict(t) for t in slider.titles
                         if t.type in ("movie", "tv")]
                if items:
                    sections[label] = items

        return sections

    # ------------------------------------------------------------------
    # Ricerca — specchio di search()
    # ------------------------------------------------------------------

    def search(self, query: str, page: int = 1) -> list[dict]:
        """
        Cerca titoli per nome. Restituisce lista di dict.

        La ricerca NON usa Inertia.js: risponde con una paginazione
        Laravel standard {current_page, data:[...], last_page, ...}.
        I titoli sono in data[], non in props.titles.
        """
        self._ensure_auth()

        params: dict[str, str] = {"q": query}
        if page > 1:
            params["page"] = str(page)

        resp = self.session.get(
            f"{self.base_url}/search",
            params=params,
            headers=self._inertia_headers(),
            timeout=20,
        )

        try:
            d = resp.json()
        except Exception as e:
            print(f"[SC] search: risposta non JSON: {e}")
            return []

        # Risposta Laravel paginata: {current_page, data:[...], last_page}
        if "data" in d and isinstance(d["data"], list):
            raw_titles = d["data"]
        # Fallback Inertia (nel caso il sito cambi in futuro)
        elif "props" in d:
            return self._parse_titles_from_response(resp.text) and \
                   [self._title_to_dict(t) for t in self._parse_titles_from_response(resp.text)
                    if t.type in ("movie", "tv")]
        else:
            print(f"[SC] search: struttura risposta sconosciuta: {list(d.keys())}")
            return []

        results = []
        for item in raw_titles:
            if item.get("type") not in ("movie", "tv"):
                continue
            t = Title.from_dict(item)
            results.append(self._title_to_dict(t))
        return results

    def search_pages(self, query: str) -> tuple[list[dict], int]:
        """
        Come search() ma restituisce anche il numero totale di pagine.
        Utile per iterare: results, total_pages = sc.search_pages("query")
        """
        self._ensure_auth()
        resp = self.session.get(
            f"{self.base_url}/search",
            params={"q": query},
            headers=self._inertia_headers(),
            timeout=20,
        )
        try:
            d = resp.json()
        except Exception:
            return [], 0

        last_page = d.get("last_page", 1)
        raw_titles = d.get("data", [])
        results = [self._title_to_dict(Title.from_dict(item))
                   for item in raw_titles
                   if item.get("type") in ("movie", "tv")]
        return results, last_page

    # ------------------------------------------------------------------
    # Load — specchio di load() + getEpisodes()
    # ------------------------------------------------------------------

    def load(self, url: str) -> dict:
        """
        Carica la pagina di un titolo (film o serie) e restituisce
        tutti i dati inclusi gli episodi per ogni stagione.

        Per le serie, itera ogni stagione facendo GET su
        /titles/{id}-{slug}/season-{n} con header Inertia
        (identico al Kotlin).
        """
        self._ensure_auth()
        actual_url = self._fix_url(url)

        resp   = self.session.get(actual_url, headers=self._inertia_headers(), timeout=20)
        result = self._parse_inertia(resp.text)
        if not result:
            raise ValueError(f"Impossibile parsare la risposta per {actual_url}")

        props = result.props
        title = props.title
        if not title:
            raise ValueError("props.title assente nella risposta")

        cdn         = props.cdn_url or self._cdn_url()
        year        = (title.release_date or "")[:4] or None
        genres      = [g.name.capitalize() for g in title.genres]
        trailers    = [t.youtube_url() for t in title.trailers if t.youtube_url()]
        poster      = self._poster_url(title.poster_image())
        background  = self._poster_url(title.background())
        related     = [self._title_to_dict(t)
                       for s in (props.sliders[:1] if props.sliders else [])
                       for t in s.titles if t.type in ("movie", "tv")]

        base = {
            "id":          title.id,
            "name":        title.name,
            "slug":        title.slug,
            "url":         actual_url,
            "type":        title.type,      # "movie" | "tv"
            "plot":        title.plot,
            "year":        year,
            "score":       title.score,
            "quality":     title.quality,
            "status":      title.status,
            "age_rating":  f"{title.age}+" if title.age else None,
            "runtime":     title.runtime,
            "genres":      genres,
            "actors":      [a.name for a in title.main_actors],
            "tmdb_id":     title.tmdb_id,
            "imdb_id":     title.imdb_id,
            "trailers":    trailers,
            "poster":      poster,
            "background":  background,
            "related":     related,
            "scws_url":    props.scws_url,
            "cdn_url":     cdn,
        }

        if title.type == "tv":
            base["seasons"]  = self._get_all_episodes(props, title, cdn)
            base["episodes"] = [ep for s in base["seasons"] for ep in s["episodes"]]
        else:
            # Film: URL iframe diretto, identico al Kotlin
            base["iframe_url"] = (
                f"{self.base_url}/iframe/{title.id}?canPlayFHD=1"
            )

        return base

    def _get_all_episodes(
        self,
        props: Props,
        title: TitleProp,
        cdn: str,
    ) -> list[dict]:
        """
        Specchio di getEpisodes():
        Per la stagione già caricata usa props.loadedSeason.
        Per le altre fa GET su /titles/{id}-{slug}/season-{n}.
        """
        seasons_out = []

        for season in title.seasons:
            if props.loaded_season and season.id == props.loaded_season.id:
                episodes = props.loaded_season.episodes or []
            else:
                # Fetch stagione aggiuntiva con header Inertia
                if not self._inertia_version:
                    self._setup_auth()
                season_url = (
                    f"{self.base_url}/titles/{title.id}-{title.slug}"
                    f"/season-{season.number}"
                )
                r      = self.session.get(season_url, headers=self._inertia_headers(), timeout=20)
                result = self._parse_inertia(r.text)
                episodes = result.props.loaded_season.episodes if result and result.props.loaded_season else []

            eps_out = []
            for ep in (episodes or []):
                cover_url = f"{cdn}/images/{ep.cover()}" if ep.cover() else None
                eps_out.append({
                    "id":            ep.id,
                    "number":        ep.number,
                    "name":          ep.name,
                    "plot":          ep.plot,
                    "duration":      ep.duration,
                    "scws_id":       ep.scws_id,
                    "season_number": season.number,
                    "cover":         cover_url,
                    # URL iframe pronto per get_episode_link()
                    "iframe_url": (
                        f"{self.base_url}/iframe/{title.id}"
                        f"?episode_id={ep.id}&canPlayFHD=1"
                    ),
                })
            seasons_out.append({
                "id":     season.id,
                "number": season.number,
                "name":   season.name,
                "episodes": eps_out,
            })

        return seasons_out

    # ------------------------------------------------------------------
    # loadLinks — specchio di loadLinks()
    # ------------------------------------------------------------------

    def get_episode_link(self, episode: dict) -> str | None:
        """
        Dato un dict episodio (da load()["episodes"]),
        restituisce il link M3U8.

        Flusso identico a loadLinks() Kotlin:
          GET iframe_url → estrae <iframe src> → VixCloudExtractor
        """
        return self._extract_vixcloud(episode["iframe_url"])

    def get_movie_link(self, info: dict) -> str | None:
        """
        Dato il dict restituito da load() per un film,
        restituisce il link M3U8.
        """
        return self._extract_vixcloud(info["iframe_url"])

    def _extract_vixcloud(self, iframe_url: str) -> str | None:
        """
        Specchio della parte VixCloud di loadLinks():
          GET iframe_url → cerca <iframe> → passa src a VixCloudExtractor
        """
        resp  = self.session.get(iframe_url, timeout=20)
        soup  = BeautifulSoup(resp.text, "html.parser")
        iframe = soup.find("iframe")
        if not iframe:
            print(f"[SC] Nessun <iframe> trovato in {iframe_url}")
            return None
        src = iframe.get("src", "")
        if not src:
            print(f"[SC] <iframe> senza src in {iframe_url}")
            return None

        referer = self.base_url.rstrip(self.lang)  # https://streamingunity.dog/
        return VixCloudExtractor(session=self.session).get_m3u8(src, referer=referer)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _fix_url(self, url: str) -> str:
        """
        Specchio di getActualUrl():
        Se l'URL ha un host diverso dal base_url corrente, lo corregge.
        Gestisce sia URL con /it/ che senza prefisso lingua.
        """
        parsed    = urlparse(url)
        expected  = urlparse(self.base_url)

        if parsed.hostname == expected.hostname:
            return url

        # Sostituisce l'host preservando il path
        path = parsed.path
        if f"/{self.lang}/" not in path and not path.startswith(f"/{self.lang}"):
            corrected_host = expected.hostname + f"/{self.lang}"
        else:
            corrected_host = expected.hostname

        fixed = url.replace(parsed.hostname, corrected_host)
        return fixed


# ---------------------------------------------------------------------------
# CLI interattiva — identica per stile ad animeunity.py
# ---------------------------------------------------------------------------

def _cli() -> None:
    import sys

    sc = StreamingCommunity()

    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUSO:")
        print("  python streamingcommunity.py search <query>")
        print("  python streamingcommunity.py home")
        print("  python streamingcommunity.py load <url>   # interattivo")
        print("  python streamingcommunity.py play <iframe_url>")
        return

    cmd  = sys.argv[1]
    args = sys.argv[2:]

    # ── search ──────────────────────────────────────────────────────────────
    if cmd == "search":
        query = " ".join(args) or "breaking bad"
        print(f"\n⏳ Ricerca '{query}' ...")
        results = sc.search(query)
        if not results:
            print("  Nessun risultato.")
            return
        print(f"\n🔍 Risultati per '{query}':\n")
        for i, r in enumerate(results):
            icon = "🎬" if r["type"] == "movie" else "📺"
            print(f"  [{i:2d}] {icon} {r['name']}")
            print(f"        {r['url']}")
        return

    # ── home ────────────────────────────────────────────────────────────────
    if cmd == "home":
        print("\n⏳ Caricamento homepage ...")
        sections = sc.get_homepage()
        for label, titles in sections.items():
            print(f"\n── {label} ({'film' if titles[0]['type']=='movie' else 'serie'}) ──")
            for t in titles[:5]:
                icon = "🎬" if t["type"] == "movie" else "📺"
                print(f"   {icon} {t['name']}")
            if len(titles) > 5:
                print(f"   … e altri {len(titles)-5}")
        return

    # ── load ────────────────────────────────────────────────────────────────
    if cmd == "load":
        if not args:
            print("Specifica un URL titolo")
            return
        _cmd_load(sc, args[0])
        return

    # ── play diretto ────────────────────────────────────────────────────────
    if cmd == "play":
        if not args:
            print("Specifica un iframe_url")
            return
        _cmd_play_url(sc, args[0])
        return

    print(f"Comando sconosciuto: {cmd}")


def _cmd_load(sc: StreamingCommunity, url: str) -> None:
    print(f"\n⏳ Caricamento {url} ...")
    info = sc.load(url)

    print(f"\n{'─'*60}")
    icon = "🎬" if info["type"] == "movie" else "📺"
    print(f"{icon}  {info['name']}  ({info['year'] or '—'})")
    print(f"{'─'*60}")
    print(f"  Tipo      : {info['type'].upper()}")
    print(f"  Score     : {info['score'] or '—'}  |  Qualità: {info['quality'] or '—'}")
    print(f"  Generi    : {', '.join(info['genres']) or '—'}")
    print(f"  Durata    : {info['runtime']} min" if info["runtime"] else "  Durata    : —")
    print(f"  Attori    : {', '.join(info['actors'][:5]) or '—'}")
    print(f"  Trama     : {(info['plot'] or '—')[:120]}{'…' if len(info['plot'] or '')>120 else ''}")
    if info.get("trailers"):
        print(f"  Trailer   : {info['trailers'][0]}")
    print(f"  Poster    : {info['poster'] or '—'}")
    print(f"{'─'*60}\n")

    if info["type"] == "movie":
        print("  È un film. Estraggo il link M3U8 ...\n")
        _cmd_play_url(sc, info["iframe_url"], label=info["name"])
    else:
        _select_episode(sc, info)


def _select_episode(sc: StreamingCommunity, info: dict) -> None:
    seasons  = info.get("seasons", [])
    episodes = info.get("episodes", [])

    if not episodes:
        print("  ⚠️  Nessun episodio disponibile.")
        return

    # Mostra stagioni e conta episodi
    print(f"  Stagioni: {len(seasons)}")
    for s in seasons:
        print(f"    S{s['number']:02d}  —  {len(s['episodes'])} episodi"
              + (f"  ({s['name']})" if s["name"] else ""))
    print()

    # Lista piatta di tutti gli episodi
    _print_episode_list(episodes)

    print("Scegli episodio/i da riprodurre:")
    print("  • Numero singolo      →  5")
    print("  • SxE  (stagione×ep)  →  2x5   (stagione 2, episodio 5)")
    print("  • Range per ep        →  5-10")
    print("  • Tutti               →  tutti  (o 'all')")
    print("  • Annulla             →  q\n")

    while True:
        try:
            raw = input("Episodio: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAnnullato.")
            return

        if raw in ("", "q", "quit", "exit"):
            return

        selected = _parse_selection(raw, episodes)
        if selected is None:
            print("  ❌ Formato non valido. Esempi: '5', '2x5', '5-10', 'tutti'")
            continue
        if not selected:
            print(f"  ❌ Nessun episodio trovato per '{raw}'")
            continue
        break

    for ep in selected:
        label = f"S{ep['season_number']:02d}E{ep['number']:02d} — {ep['name']}"
        print(f"\n⏳ {label} — estrazione link ...")
        link = sc.get_episode_link(ep)
        _print_link(link, label)


def _print_episode_list(episodes: list[dict]) -> None:
    print(f"  Episodi disponibili ({len(episodes)} totali):\n")
    # Raggruppa per stagione
    from itertools import groupby
    key = lambda e: e["season_number"]
    for season_n, eps in groupby(sorted(episodes, key=key), key=key):
        eps = list(eps)
        nums = "".join(f"[{e['number']:>3}]" for e in eps[:20])
        extra = f"  … +{len(eps)-20}" if len(eps) > 20 else ""
        print(f"  S{season_n:02d}: {nums}{extra}")
    print()


def _parse_selection(raw: str, episodes: list[dict]) -> list[dict] | None:
    """
    Formati supportati:
      "tutti" / "all"   → tutti
      "2x5"             → stagione 2, episodio 5
      "5-10"            → episodi 5..10 (numero ep, qualsiasi stagione)
      "5"               → episodio numero 5
    """
    if raw in ("tutti", "all"):
        return episodes

    # SxE  (es. 2x5)
    m = re.fullmatch(r"(\d+)[x×](\d+)", raw)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        return [ep for ep in episodes
                if ep["season_number"] == s and ep["number"] == e]

    # Range  (es. 5-10)
    m = re.fullmatch(r"(\d+)-(\d+)", raw)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        return [ep for ep in episodes if start <= ep["number"] <= end]

    # Singolo numero
    if raw.isdigit():
        n = int(raw)
        return [ep for ep in episodes if ep["number"] == n]

    return None


def _cmd_play_url(sc: StreamingCommunity, iframe_url: str, label: str = "") -> None:
    link = sc._extract_vixcloud(iframe_url)
    _print_link(link, label or iframe_url)


def _print_link(link: str | None, label: str = "") -> None:
    tag = f"[{label}] " if label else ""
    if link:
        print(f"✅ {tag}M3U8 pronto:\n")
        print(f"   {link}\n")
        print(f"   ▶  mpv \"{link}\"")
        print(f"   ▶  yt-dlp \"{link}\"")
    else:
        print(f"❌ {tag}Link non trovato")


if __name__ == "__main__":
    _cli()
