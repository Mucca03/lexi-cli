#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

API_URL = "https://lrclib.net/api/get"
USER_AGENT = "lexi-cli/0.1 (terminal lyrics client)"
POLL_INTERVAL = 0.1
METADATA_INTERVAL = 0.5
# Bloque de celdas 2x2 para el texto ampliado. La fuente real es 1.8x
# mediante el escalado fraccional de OSC 66 (n/d).
BIG_TEXT_SCALE = 2
BIG_TEXT_FONT_NUMERATOR = 9
BIG_TEXT_FONT_DENOMINATOR = 10
# Filas en blanco arriba y abajo del área de letras. Se reducen en
# terminales muy bajas para no quitar espacio a la línea activa.
VERTICAL_MARGIN = 2
API_TIMEOUT = 8.0
UNIT_SEPARATOR = "\x1f"
METADATA_FORMAT = (
    "{{artist}}"
    + UNIT_SEPARATOR
    + "{{title}}"
    + UNIT_SEPARATOR
    + "{{status}}"
    + UNIT_SEPARATOR
    + "{{mpris:length}}"
)

# [mm:ss.xx], [mm:ss.xxx], [mm:ss] y la variante [mm:ss:xx].
TIMESTAMP_RE = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
WORD_TIMESTAMP_RE = re.compile(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>")
METADATA_TAG_RE = re.compile(
    r"\[(?:ar|ti|al|by|offset|re|ve|length|au|tool):[^\]]*\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LyricLine:
    time: float
    text: str


@dataclass(frozen=True)
class Lyrics:
    lines: tuple[LyricLine, ...]
    times: tuple[float, ...]

    def active_index(self, position: float) -> Optional[int]:
        """Índice de la última línea cuyo timestamp ya pasó."""
        index = bisect.bisect_right(self.times, position) - 1
        return index if index >= 0 else None


@dataclass(frozen=True)
class LyricsResult:
    lyrics: Optional[Lyrics] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class TrackInfo:
    artist: str
    title: str
    status: str
    duration: Optional[float]

    @property
    def key(self) -> tuple[str, str]:
        return (self.artist, self.title)


def parse_lrc(text: str) -> Lyrics:
    """Convierte el texto LRC en líneas ordenadas por tiempo."""
    parsed: list[LyricLine] = []

    for raw_line in text.lstrip("\ufeff").splitlines():
        matches = list(TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue

        lyric = TIMESTAMP_RE.sub("", raw_line)
        lyric = METADATA_TAG_RE.sub("", lyric)
        lyric = WORD_TIMESTAMP_RE.sub("", lyric).strip()

        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or "0"
            milliseconds = int((fraction + "000")[:3])
            timestamp = minutes * 60 + seconds + milliseconds / 1000
            parsed.append(LyricLine(timestamp, lyric))

    parsed.sort(key=lambda line: line.time)
    return Lyrics(
        lines=tuple(parsed),
        times=tuple(line.time for line in parsed),
    )


def fetch_lyrics(artist: str, title: str) -> LyricsResult:
    """Busca la letra sincronizada en LRCLIB."""
    if not artist or not title:
        return LyricsResult(message="Faltan el artista o el título del tema.")

    query = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return LyricsResult(message="LRCLIB no encontró la letra de este tema.")
        if exc.code == 429:
            return LyricsResult(
                message="LRCLIB limitó las peticiones. Espera un momento."
            )
        return LyricsResult(message=f"LRCLIB respondió con HTTP {exc.code}.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return LyricsResult(message=f"No se pudo conectar con LRCLIB: {reason}")
    except (ValueError, UnicodeDecodeError):
        return LyricsResult(message="LRCLIB devolvió una respuesta inválida.")

    if not isinstance(payload, dict):
        return LyricsResult(message="LRCLIB devolvió una respuesta inesperada.")

    if payload.get("instrumental"):
        return LyricsResult(message="Este tema es instrumental.")

    synced = payload.get("syncedLyrics")
    if not isinstance(synced, str) or not synced.strip():
        return LyricsResult(
            message="LRCLIB no tiene letra sincronizada para este tema."
        )

    lyrics = parse_lrc(synced)
    if not lyrics.lines:
        return LyricsResult(
            message="La letra recibida no tiene líneas sincronizadas válidas."
        )

    return LyricsResult(lyrics=lyrics)


class LyricsFetcher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_id = 0
        self._loading = False
        self._result: Optional[LyricsResult] = None

    def start(self, artist: str, title: str) -> None:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._loading = True
            self._result = None

        thread = threading.Thread(
            target=self._fetch,
            args=(request_id, artist, title),
            daemon=True,
            name="lexi-fetch",
        )
        thread.start()

    def _fetch(self, request_id: int, artist: str, title: str) -> None:
        try:
            result = fetch_lyrics(artist, title)
        except Exception as exc:  # La petición nunca debe tumbar el bucle.
            result = LyricsResult(message=f"Error inesperado al buscar la letra: {exc}")

        with self._lock:
            if request_id == self._request_id:
                self._result = result
                self._loading = False

    def poll(self) -> Optional[LyricsResult]:
        with self._lock:
            result = self._result
            self._result = None
            return result

    @property
    def loading(self) -> bool:
        with self._lock:
            return self._loading

    def reset(self) -> None:
        with self._lock:
            self._request_id += 1
            self._loading = False
            self._result = None


class Player:
    def __init__(self, name: Optional[str] = None) -> None:
        self._command = ["playerctl", "-s"]
        if name:
            self._command.extend(["--player", name])

    def _run(self, *args: str, timeout: float = 2.0) -> str:
        try:
            result = subprocess.run(
                [*self._command, *args],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""

        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def metadata(self) -> Optional[TrackInfo]:
        raw = self._run("metadata", "--format", METADATA_FORMAT)
        if not raw:
            return None

        parts = raw.split(UNIT_SEPARATOR)
        if len(parts) < 4:
            return None

        artist = " ".join(parts[0].split())
        title = " ".join(parts[1].split())
        status = " ".join(parts[2].split())
        length_raw = parts[3].strip()
        if not title:
            return None

        duration = None
        if length_raw:
            try:
                length_us = int(length_raw)
            except ValueError:
                pass
            else:
                if length_us > 0:
                    duration = length_us / 1_000_000

        return TrackInfo(
            artist=artist,
            title=title,
            status=status,
            duration=duration,
        )

    def position(self) -> Optional[float]:
        raw = self._run("position", timeout=1.0)
        if not raw:
            return None

        try:
            value = float(raw.splitlines()[0])
        except (ValueError, IndexError):
            return None

        if not math.isfinite(value):
            return None
        return max(0.0, value)


def display_width(text: str) -> int:
    """Ancho aproximado en celdas, sin depender de wcwidth."""
    width = 0
    for char in text:
        if unicodedata.category(char) in {"Mn", "Me", "Cf"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def kitty_text_sizing_supported() -> bool:
    """Comprueba si la terminal es Kitty >= 0.40 (protocolo OSC 66)."""
    if not os.environ.get("KITTY_WINDOW_ID"):
        return False
    if os.environ.get("TMUX") or os.environ.get("STY"):
        return False

    try:
        result = subprocess.run(
            ["kitty", "--version"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    match = re.search(r"kitty\s+(\d+)\.(\d+)", result.stdout)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (0, 40)


class Screen:
    """Gestiona la pantalla alternativa, el resaltado ANSI y el texto ampliado."""

    def __init__(self, color: bool = True, big_text: bool = True) -> None:
        self.is_tty = sys.stdout.isatty()
        self.ansi = self.is_tty
        self.color = color and self.is_tty and "NO_COLOR" not in os.environ
        self.big_text = big_text and self.ansi and kitty_text_sizing_supported()

    def __enter__(self) -> Screen:
        if self.ansi:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.ansi:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        return False

    def render(self, frame: str) -> None:
        if self.ansi:
            sys.stdout.write("\x1b[H\x1b[2J" + frame)
        else:
            sys.stdout.write(frame + "\n")
        sys.stdout.flush()


def ansi(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def format_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def status_label(status: str) -> str:
    labels = {
        "playing": "Reproduciendo",
        "paused": "Pausado",
        "stopped": "Detenido",
    }
    return labels.get(status.strip().lower(), status.strip() or "Desconocido")


def lyric_rows(lyrics: Lyrics, width: int) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    for line_index, line in enumerate(lyrics.lines):
        text = line.text or "♪"
        wrapped = textwrap.wrap(
            text,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=True,
            drop_whitespace=True,
        )
        if not wrapped:
            wrapped = [""]
        for part_index, part in enumerate(wrapped):
            rows.append((line_index, part_index, part))
    return rows


def style_lyric_row(
    text: str,
    width: int,
    *,
    active: bool,
    screen: Screen,
) -> str:
    raw = text.center(width)

    if active:
        if screen.color:
            return f"\x1b[1;36m{raw}\x1b[0m"
        return ansi(raw, "1", screen.ansi)
    return ansi(raw, "2", screen.ansi)


def wrap_display(text: str, width: int) -> list[str]:
    """Envuelve texto respetando el ancho en celdas de cada carácter."""
    width = max(1, width)
    lines: list[str] = []
    current = ""

    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if display_width(candidate) <= width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        if display_width(word) <= width:
            current = word
            continue

        piece = ""
        for char in word:
            candidate = piece + char
            if display_width(candidate) <= width:
                piece = candidate
            else:
                if piece:
                    lines.append(piece)
                piece = char
        current = piece

    if current:
        lines.append(current)
    return lines or [""]


def sanitize_osc_text(text: str) -> str:
    """Elimina controles que romperían la secuencia OSC 66."""
    return "".join(char for char in text if unicodedata.category(char) != "Cc")


def big_text_row(text: str, width: int, screen: Screen) -> str:
    text = sanitize_osc_text(text)
    scaled_width = display_width(text) * BIG_TEXT_SCALE
    padding = max(0, (width - scaled_width) // 2)
    size = (
        f"s={BIG_TEXT_SCALE}"
        f":n={BIG_TEXT_FONT_NUMERATOR}"
        f":d={BIG_TEXT_FONT_DENOMINATOR}"
        ":v=2:h=2"
    )
    raw = " " * padding + f"\x1b]66;{size};{text}\x07"

    if screen.color:
        return f"\x1b[1;36m{raw}\x1b[0m"
    return ansi(raw, "1", screen.ansi)


def render_normal_active(
    text: str,
    wrap_width: int,
    screen_width: int,
    screen: Screen,
) -> list[str]:
    text = text.strip() or "♪"
    wrapped = textwrap.wrap(
        text,
        width=wrap_width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=True,
        drop_whitespace=True,
    )
    if not wrapped:
        wrapped = [""]
    return [
        style_lyric_row(part, screen_width, active=True, screen=screen)
        for part in wrapped
    ]


def render_big_active(
    text: str,
    width: int,
    max_scaled_width: int,
    screen: Screen,
) -> list[str]:
    text = sanitize_osc_text(text).strip() or "♪"
    max_width = max(1, max_scaled_width // BIG_TEXT_SCALE)
    rows: list[str] = []

    for chunk in wrap_display(text, max_width):
        rows.append(big_text_row(chunk, width, screen))
        # La segunda fila del bloque 2x2 queda libre para el texto ampliado.
        rows.append("")

    return rows


def render_lyrics(
    lyrics: Lyrics,
    position: Optional[float],
    width: int,
    height: int,
    screen: Screen,
) -> list[str]:
    active = lyrics.active_index(position) if position is not None else None
    if active is None:
        return []

    if height <= 0:
        return []

    text_width = max(10, min(width - 4, 100))
    lyric_parts = lyric_rows(lyrics, text_width)
    active_text = lyrics.lines[active].text.strip() or "♪"

    active_block: Optional[list[str]] = None
    if screen.big_text:
        big_block = render_big_active(active_text, width, text_width, screen)
        if len(big_block) <= height:
            active_block = big_block
    if active_block is None:
        active_block = render_normal_active(
            active_text,
            text_width,
            width,
            screen,
        )

    active_block = active_block[:height]
    active_height = len(active_block)
    margin = min(VERTICAL_MARGIN, max(0, (height - active_height) // 4))
    content_height = height - 2 * margin
    active_block = active_block[:content_height]
    active_height = len(active_block)
    target_top = max(0, (content_height - active_height + 1) // 2)
    space_above = target_top
    space_below = content_height - target_top - active_height

    before_rows = [row for row in lyric_parts if row[0] < active]
    after_rows = [row for row in lyric_parts if row[0] > active]

    def pick_context(
        candidates: list[tuple[int, int, str]],
        space: int,
        *,
        from_end: bool,
    ) -> tuple[list[tuple[int, int, str]], bool]:
        if space <= 0 or not candidates:
            return [], False
        if space == 1:
            count = 1
            separator = False
        else:
            count = min(len(candidates), space - 1)
            separator = True
        selected = candidates[-count:] if from_end else candidates[:count]
        return selected, separator

    before_selected, before_separator = pick_context(
        before_rows,
        space_above,
        from_end=True,
    )
    after_selected, after_separator = pick_context(
        after_rows,
        space_below,
        from_end=False,
    )

    def styled(selected: list[tuple[int, int, str]]) -> list[str]:
        return [
            style_lyric_row(text, width, active=False, screen=screen)
            for _, _, text in selected
        ]

    before_block = styled(before_selected)
    after_block = styled(after_selected)

    top_padding = space_above - len(before_block) - (1 if before_separator else 0)
    bottom_padding = space_below - len(after_block) - (1 if after_separator else 0)

    rows = [""] * max(0, top_padding)
    rows.extend(before_block)
    if before_separator:
        rows.append("")
    rows.extend(active_block)
    if after_separator:
        rows.append("")
    rows.extend(after_block)
    rows.extend([""] * max(0, bottom_padding))
    return [""] * margin + rows + [""] * margin


def render_message(
    message: str,
    width: int,
    height: int,
    screen: Screen,
) -> list[str]:
    text_width = max(10, min(width - 4, 80))
    wrapped = textwrap.wrap(message, width=text_width) or [""]
    top_pad = max(0, (height - len(wrapped)) // 2)

    output = [""] * top_pad
    for line in wrapped[:height]:
        raw = line.center(width)
        output.append(ansi(raw, "2", screen.ansi))
    return output


def build_frame(
    info: Optional[TrackInfo],
    position: Optional[float],
    lyrics: Optional[Lyrics],
    message: Optional[str],
    loading: bool,
    screen: Screen,
) -> str:
    size = shutil.get_terminal_size(fallback=(80, 24))
    width = max(20, size.columns)
    height = max(1, size.lines)

    footer_rows = 1 if height >= 2 else 0
    separator_rows = 1 if height >= 4 else 0
    body_height = max(1, height - footer_rows - separator_rows)

    if info is None:
        footer_text = "playerctl no detecta ningún reproductor"
        body_message = "Esperando reproductor MPRIS…"
    else:
        artist = info.artist.strip()
        track = f"♪ {artist} — {info.title}" if artist else f"♪ {info.title}"
        status_parts = [status_label(info.status)]
        if position is not None:
            current = format_time(position)
            if info.duration:
                status_parts.append(f"{current} / {format_time(info.duration)}")
            else:
                status_parts.append(current)
        footer_text = f"{track}  ·  {'  ·  '.join(status_parts)}"
        body_message = None

    footer = ansi(truncate(footer_text, width), "2", screen.ansi)

    if info is None:
        body = render_message(body_message or "", width, body_height, screen)
    elif loading and lyrics is None:
        body = render_message(
            "Buscando letra sincronizada en LRCLIB…",
            width,
            body_height,
            screen,
        )
    elif message and lyrics is None:
        body = render_message(message, width, body_height, screen)
    elif lyrics is not None:
        active = lyrics.active_index(position) if position is not None else None
        if position is None:
            body = render_message(
                "Esperando posición del reproductor…",
                width,
                body_height,
                screen,
            )
        elif active is None:
            first_time = lyrics.lines[0].time if lyrics.lines else None
            if first_time is None:
                body = render_message(
                    "Sin letra disponible.", width, body_height, screen
                )
            else:
                body = render_message(
                    f"La letra comienza en {format_time(first_time)}",
                    width,
                    body_height,
                    screen,
                )
        else:
            body = render_lyrics(lyrics, position, width, body_height, screen)
            if not body:
                body = render_message(
                    "Sin letra disponible.", width, body_height, screen
                )
    else:
        body = render_message("Sin letra disponible.", width, body_height, screen)

    body = body[:body_height]
    body.extend([""] * (body_height - len(body)))

    lines = body
    if separator_rows:
        lines.append("")
    if footer_rows:
        lines.append(footer)

    return "\n".join(lines[:height])


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lexi.py",
        description=("Muestra la letra sincronizada del reproductor MPRIS activo."),
    )
    parser.add_argument(
        "-p",
        "--player",
        metavar="NOMBRE",
        help="reproductor de playerctl a usar (por defecto, el primero)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="desactiva el color; la línea activa sigue en negrita",
    )
    parser.add_argument(
        "--no-big-text",
        action="store_true",
        help="desactiva el texto ampliado de Kitty; usa negrita y color",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="lexi-cli 0.1",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if shutil.which("playerctl") is None:
        print(
            "Error: no se encontró 'playerctl'. Instálalo para usar lexi-cli.",
            file=sys.stderr,
        )
        return 2

    player = Player(args.player)
    fetcher = LyricsFetcher()
    screen = Screen(color=not args.no_color, big_text=not args.no_big_text)

    state_track: Optional[tuple[str, str]] = None
    info: Optional[TrackInfo] = None
    position: Optional[float] = None
    lyrics: Optional[Lyrics] = None
    message: Optional[str] = None
    last_metadata_at = -METADATA_INTERVAL

    def handle_sigterm(signum, frame) -> None:
        raise KeyboardInterrupt

    old_sigterm = signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        with screen:
            next_tick = time.monotonic()

            while True:
                now = time.monotonic()

                if now - last_metadata_at >= METADATA_INTERVAL:
                    info = player.metadata()
                    last_metadata_at = now

                    if info is None:
                        if state_track is not None:
                            state_track = None
                            lyrics = None
                            message = None
                            fetcher.reset()
                    elif info.key != state_track:
                        state_track = info.key
                        lyrics = None
                        message = None

                        if info.artist and info.title:
                            fetcher.start(info.artist, info.title)
                        else:
                            fetcher.reset()
                            message = "Faltan el artista o el título del tema."

                position = player.position() if info is not None else None

                result = fetcher.poll()
                if result is not None:
                    lyrics = result.lyrics
                    message = result.message

                frame = build_frame(
                    info,
                    position,
                    lyrics,
                    message,
                    fetcher.loading,
                    screen,
                )
                screen.render(frame)

                next_tick += POLL_INTERVAL
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_tick = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
