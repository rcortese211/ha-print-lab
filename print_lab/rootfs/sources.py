"""Turn a link into the model files behind it, and pick files by what the person asked for.

  resolve(url)            -> (title, [Candidate]) for a direct file, a zip, a Printables model,
                             a Thingiverse thing, or any page that links model files
  parse_selection(text)   -> [(phrase, count)] from "2 small key caps and 2 large", "1 x2, 3"
  match(selection, cands) -> ([(Candidate, count)], problems)

Standard library only.
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable

PRINTABLE = (".stl", ".3mf", ".obj")
CAD = (".step", ".stp")
MODEL_EXT = PRINTABLE + CAD
MAX_DOWNLOAD = 200 * 1024 * 1024
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
PRINTABLES_API = "https://api.printables.com/graphql/"


class SourceError(Exception):
    pass


@dataclass
class Candidate:
    name: str                               # file name as the site shows it
    size: int | None = None
    get: Callable[[], bytes] = field(default=lambda: b"", repr=False)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]


# --------------------------------------------------------------------------- fetching
def _request(url, data=None, headers=None, timeout=60, limit=MAX_DOWNLOAD) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = bytearray()
            while chunk := r.read(1 << 16):
                out += chunk
                if len(out) > limit:
                    raise SourceError("That download is larger than 200 MB")
            return bytes(out)
    except urllib.error.HTTPError as err:
        raise SourceError(f"{urllib.parse.urlsplit(url).hostname} answered HTTP {err.code}") from err
    except (urllib.error.URLError, OSError) as err:
        raise SourceError(f"Couldn't reach {urllib.parse.urlsplit(url).hostname} "
                          f"({getattr(err, 'reason', err)})") from err


def _zip_candidates(blob: bytes) -> list[Candidate]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as err:
        raise SourceError("The download isn't a readable zip") from err
    out = []
    for info in zf.infolist():
        name = os.path.basename(info.filename)
        if info.is_dir() or not name.lower().endswith(MODEL_EXT) or name.startswith("."):
            continue
        if info.file_size > MAX_DOWNLOAD:
            continue
        out.append(Candidate(name, info.file_size, lambda i=info: zf.read(i)))
    return out


def _printables(model_id: str) -> tuple[str, list[Candidate]]:
    def gql(op, query, variables):
        body = json.dumps({"operationName": op, "query": query, "variables": variables}).encode()
        r = json.loads(_request(PRINTABLES_API, body, {"Content-Type": "application/json"}, timeout=30,
                                limit=5_000_000))
        if r.get("errors"):
            raise SourceError(f"Printables: {r['errors'][0].get('message', 'error')}")
        return r.get("data") or {}

    data = gql("ModelFiles", """
        query ModelFiles($id: ID!) {
          model: print(id: $id) { id name stls { id name fileSize folder } }
        }""", {"id": model_id})
    model = data.get("model")
    if not model:
        raise SourceError("Printables doesn't have that model (or it's private)")

    def getter(file_id):
        def get():
            d = gql("GetDownloadLink", """
                mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!,
                                         $source: DownloadSourceEnum!) {
                  getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: $source) {
                    ok errors { field messages } output { link }
                  }
                }""", {"id": file_id, "modelId": model_id, "fileType": "stl", "source": "model_detail"})
            link = ((d.get("getDownloadLink") or {}).get("output") or {}).get("link")
            if not link:
                raise SourceError("Printables wouldn't give a download link for that file")
            return _request(link)
        return get

    files = [Candidate(f["name"], f.get("fileSize"), getter(f["id"]))
             for f in model.get("stls") or [] if str(f.get("name", "")).lower().endswith(MODEL_EXT)]
    return model.get("name") or f"Printables {model_id}", files


THINGIVERSE_TOKEN = os.environ.get("THINGIVERSE_TOKEN", "")   # set by the bot from its config


def _thingiverse(thing_id: str) -> tuple[str, list[Candidate]]:
    """Thingiverse through its official API with the owner's app token; without one, the
    thing's download-all zip (which Thingiverse doesn't always hand to scripts)."""
    if THINGIVERSE_TOKEN:
        auth = {"Authorization": f"Bearer {THINGIVERSE_TOKEN}"}
        try:
            thing = json.loads(_request(f"https://api.thingiverse.com/things/{thing_id}", headers=auth,
                                        timeout=30, limit=5_000_000))
            files = json.loads(_request(f"https://api.thingiverse.com/things/{thing_id}/files", headers=auth,
                                        timeout=30, limit=5_000_000))
        except ValueError as err:
            raise SourceError("Thingiverse's API answered with something unreadable") from err
        if not isinstance(files, list):
            raise SourceError(f"Thingiverse's API said: {str(files)[:120]}")

        def getter(f):
            def get():
                url = f.get("download_url") or f.get("direct_url") or f.get("public_url")
                if not url:
                    raise SourceError(f"Thingiverse gave no link for {f.get('name')}")
                return _request(url, headers=auth if "api.thingiverse.com" in url else None)
            return get

        title = thing.get("name") if isinstance(thing, dict) else None
        return title or f"Thingiverse {thing_id}", [
            Candidate(f["name"], f.get("size"), getter(f)) for f in files
            if str(f.get("name", "")).lower().endswith(MODEL_EXT)]
    blob = _request(f"https://www.thingiverse.com/thing:{thing_id}/zip")
    if blob[:2] != b"PK":
        print(f"[sources] thingiverse {thing_id}/zip gave {len(blob)} bytes: {blob[:80]!r}", flush=True)
        raise SourceError("Thingiverse didn't hand over a zip. Give the bot a Thingiverse app token "
                          "(free, thingiverse.com/developers) and it'll use their API instead")
    return f"Thingiverse {thing_id}", _zip_candidates(blob)


def resolve(url: str) -> tuple[str, list[Candidate]]:
    """The model files behind a link (CAD duplicates of printable files are dropped)."""
    parts = urllib.parse.urlsplit(url)
    host, path = (parts.hostname or "").lower(), urllib.parse.unquote(parts.path)
    pm = re.search(r"/model/(\d+)", path) if host.endswith("printables.com") else None
    if pm:
        title, files = _printables(pm.group(1))
    elif "thingiverse.com" in host and (m := re.search(r"thing:(\d+)", url)):
        title, files = _thingiverse(m.group(1))
    elif path.lower().endswith(MODEL_EXT):
        name = os.path.basename(path)
        title, files = name, [Candidate(name, None, lambda: _request(url))]
    elif path.lower().endswith(".zip"):
        title, files = os.path.basename(path), _zip_candidates(_request(url))
    else:
        page = html.unescape(_request(url, limit=5_000_000).decode(errors="ignore"))
        links = []
        for m in re.finditer(r"""https?://[^\s"'<>]+?\.(?:3mf|stl|obj|step|stp|zip)(?:\?[^\s"'<>]*)?""", page, re.I):
            if m.group(0) not in links:
                links.append(m.group(0))
        if not links:
            raise SourceError("I couldn't find any model files behind that link")
        files = []
        for link in links[:20]:
            name = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(link).path))
            if name.lower().endswith(".zip"):
                files += _zip_candidates(_request(link))
            else:
                files.append(Candidate(name, None, lambda u=link: _request(u)))
        title = parts.hostname or url
    printable_stems = {c.stem.lower() for c in files if c.name.lower().endswith(PRINTABLE)}
    files = [c for c in files if c.name.lower().endswith(PRINTABLE) or c.stem.lower() not in printable_stems]
    if not files:
        raise SourceError("There are no STL / 3MF / OBJ / STEP files behind that link")
    return title, files


# --------------------------------------------------------------------------- picking
STOP = {"of", "the", "a", "an", "and", "copies", "copy", "pcs", "pc", "pieces", "piece", "each",
        "please", "print", "me", "x", "one", "ones", "version", "file", "files", "stl", "3mf"}


def parse_selection(text: str) -> list[tuple[str, int]]:
    """'2 small key caps and 2 large' -> [('small key caps', 2), ('large', 2)];
    '1 x2, 3' -> [('1', 2), ('3', 1)]."""
    text = re.sub(r"\b(and|plus)\b|&|;|\+", ",", text, flags=re.I)
    out = []
    for part in text.split(","):
        # "2 small 2 large" without separators: split before each "<number> <word>"
        for piece in re.split(r"\s+(?=\d{1,2}\s*[x×*]?\s+[A-Za-z])", part.strip()):
            piece = piece.strip(" .!?")
            if not piece:
                continue
            if m := re.fullmatch(r"(.+?)\s*[x×*]\s*(\d{1,2})", piece, re.I):       # small x2, 1 x2
                out.append((m.group(1).strip(), int(m.group(2))))
            elif m := re.fullmatch(r"(\d{1,2})\s*[x×*]?\s+(.+)", piece, re.I):     # 2 small, 2x small
                out.append((m.group(2).strip(), int(m.group(1))))
            elif m := re.fullmatch(r"(\d{1,2})\s*[x×*]\s*", piece, re.I):          # "2x" alone
                out.append(("", int(m.group(1))))
            else:
                out.append((piece, 1))
    return [(p, max(1, c)) for p, c in out]


def _words(text: str) -> list[str]:
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w and w not in STOP]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]


def match(selection, cands: list[Candidate]):
    """[(Candidate, count)] for each phrase, plus a list of phrases that didn't pick one file."""
    picked, problems = [], []
    for phrase, count in selection:
        if phrase.isdigit():
            i = int(phrase) - 1
            if 0 <= i < len(cands):
                picked.append((cands[i], count))
            else:
                problems.append(f"there's no file {phrase}")
            continue
        words = _words(phrase)
        if not words:
            if len(cands) == 1:
                picked.append((cands[0], count))
            else:
                problems.append(f"which file is '{phrase or count}'?")
            continue
        scored = []
        for c in cands:
            hay = re.sub(r"[^a-z0-9]+", " ", c.stem.lower())
            score = sum(1 for w in words if w in hay)
            exact = hay.split() == words or hay.replace(" ", "") == "".join(words)
            scored.append((score, exact, c.name.lower().endswith(PRINTABLE), c))
        scored.sort(key=lambda s: s[:3], reverse=True)
        best = scored[0]
        if best[0] == 0:
            problems.append(f"nothing matches '{phrase}'")
        elif len(scored) > 1 and scored[1][:3] == best[:3]:
            ties = [s[3].name for s in scored if s[:3] == best[:3]]
            problems.append(f"'{phrase}' could be {' or '.join(ties[:4])}")
        else:
            picked.append((best[3], count))
    return picked, problems


# --------------------------------------------------------------------------- "all ..." filters
FILLER = {"files", "file", "ones", "one", "that", "which", "say", "says", "with", "named", "called",
          "containing", "contain", "contains", "have", "has", "of", "the", "them", "in", "name",
          "names", "their", "please", "print", "me", "those", "these"}
ALL_RE = re.compile(r"^\s*(?:(\d{1,2})\s*(?:x|×|copies of|of)?\s+)?(?:all|every|everything)\b(.*)$", re.I | re.S)


def select_all(text: str, cands: list[Candidate]):
    """'all 19x', 'every file that says 19x but not the ones without the x', 'all except base',
    '2 of each of all small' -> ([(Candidate, count)], problems), or None if text isn't an 'all'."""
    m = ALL_RE.match(text)
    each = re.match(r"^\s*(\d{1,2})\s*[x×]?\s*(?:copies\s+)?(?:of\s+)?each\b(?:\s+of)?(.*)$", text, re.I | re.S)
    if not m and not each:
        return None
    count, rest = (int(m.group(1) or 1), m.group(2)) if m else (int(each.group(1)), each.group(2))
    if e := re.search(r"(?:\b(\d{1,2})\s*(?:of\s+)?each\b|\b[x×](\d{1,2})\s*(?:each)?\s*$|\beach\s*[x×]\s*(\d{1,2}))", rest, re.I):
        count = int(next(g for g in e.groups() if g))
        rest = rest[:e.start()] + rest[e.end():]
    split = re.search(r"\b(?:but not|except|excluding|minus|not|but)\b", rest, re.I)
    inc_text, exc_text = (rest[:split.start()], rest[split.end():]) if split else (rest, "")

    def terms(t):
        return [w for w in re.split(r"[^a-z0-9]+", t.lower()) if w and w not in FILLER]

    include, exclude = terms(inc_text), []
    # "not the ones without the x" means the x is required; "except base" means base is excluded
    for clause in re.split(r",", exc_text):
        if re.search(r"\b(without|lacking|missing|no)\b", clause, re.I):
            include += [w for w in terms(re.split(r"\b(?:without|lacking|missing|no)\b", clause, 1, flags=re.I)[1])
                        if w not in ("without", "lacking", "missing", "no")]
        else:
            exclude += terms(clause)

    def name(c):
        return re.sub(r"[^a-z0-9]+", " ", c.name.lower())

    hits = [c for c in cands
            if all(t in name(c) for t in include) and not any(t in name(c).split() or t in name(c) for t in exclude)]
    printable = [c for c in hits if c.name.lower().endswith(PRINTABLE)]
    hits = printable or hits
    if not hits:
        what = " ".join(include) or "anything"
        return [], [f"no files match '{what}'" + (f" without '{' '.join(exclude)}'" if exclude else "")]
    return [(c, count) for c in hits], []


def select(text: str, cands: list[Candidate]):
    """Pick files from a reply: an 'all ...' filter, or phrases with counts."""
    result = select_all(text, cands)
    if result is not None:
        return result
    return match(parse_selection(text), cands)
