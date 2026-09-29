"""Configuration for the whole system: web app and agent, one file.

Everything lives in a single ``meerpic.toml``, and every setting in it can be
overridden by an environment variable of the same name. Precedence, highest
first::

    constructor arg  >  environment  >  .env  >  meerpic.toml  >  default

The file is optional: the defaults describe the common case, an iCloud library
synced by rclone into ``~/Pictures/iCloud``. The shape is meercal's, on
purpose; the two sit side by side and read the same way.

Path resolution: ``$MEERPIC_CONFIG``, else ``meerpic.toml`` at the repository
root (``/app/meerpic.toml`` in both images). Setting ``MEERPIC_CONFIG`` to the
empty string means "environment only" and skips both files, which is how the
test suite keeps a developer's own configuration out of a test run.

Paths and ``~``
---------------
Every path in here is meant to be *the same path on the host and in the
containers*: docker-compose.yml mounts the picture folder and the cache at the
absolute path they have on the host. That is what lets the desktop shell drag
the original file off your disk by the path the server reports, with no
translation table in between. The one thing that differs inside a container is
``$HOME``, so ``~`` is expanded against ``MEERPIC_HOST_HOME`` when that is set
(compose sets it to yours), and against the process's own home otherwise.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import tomllib
from pydantic import field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "meerpic.toml"
EXAMPLE_CONFIG_PATH = BASE_DIR / "meerpic.example.toml"

# Section/key in the TOML -> field name on Settings. The environment variable
# for a field is its own name upper-cased, which is what makes DATABASE_URL and
# SERVER_PASSWORD line up with `database.url` and `server.password`.
_FIELD_MAP: dict[tuple[str, str], str] = {
    ("database", "url"): "database_url",
    ("server", "secret_key"): "secret_key",
    ("server", "password"): "server_password",
    ("server", "timezone"): "timezone",
    ("server", "trusted_proxies"): "trusted_proxies",
    ("library", "roots"): "library_roots",
    ("library", "delete"): "delete_enabled",
    ("cache", "dir"): "cache_dir",
    ("agent", "scan_interval"): "agent_scan_interval",
    ("agent", "workers"): "agent_workers",
    ("agent", "exiftool"): "exiftool",
    ("agent", "ffmpeg"): "ffmpeg",
    ("agent", "ffprobe"): "ffprobe",
    ("video", "previews"): "video_previews",
    ("video", "height"): "video_height",
    ("video", "crf"): "video_crf",
    ("video", "preset"): "video_preset",
    ("search", "model"): "search_model",
    ("search", "min_score"): "search_min_score",
    ("search", "similar_min_score"): "similar_min_score",
    ("sync", "enabled"): "sync_enabled",
    ("sync", "rclone"): "rclone",
    ("sync", "remote"): "sync_remote",
    ("sync", "root"): "sync_root",
    ("sync", "args"): "sync_args",
    ("sync", "interval"): "sync_interval",
    ("sync", "albums"): "sync_albums",
    ("nsfw", "enabled"): "nsfw_enabled",
    ("nsfw", "threshold"): "nsfw_threshold",
    ("nsfw", "blur"): "nsfw_blur",
    ("nsfw", "model"): "nsfw_model",
    ("nsfw", "clear_above"): "nsfw_clear_above",
    ("nsfw", "demote_above"): "nsfw_demote_above",
    ("faces", "enabled"): "faces_enabled",
    ("faces", "model"): "faces_model",
    ("faces", "min_score"): "faces_min_score",
    ("faces", "detect_score"): "faces_detect_score",
    ("ocr", "enabled"): "ocr_enabled",
    ("ocr", "videos"): "ocr_videos",
    ("map", "tile_url"): "map_tile_url",
    ("map", "attribution"): "map_attribution",
    ("map", "max_zoom"): "map_max_zoom",
    ("share", "strip_location"): "share_strip_location",
    ("share", "jpeg_quality"): "share_jpeg_quality",
    ("share", "max_edge"): "share_max_edge",
}


def expand(path: str | os.PathLike) -> Path:
    """A configured path, with ``~`` meaning the *host's* home.

    See the module docstring: inside a container ``$HOME`` is the image's own,
    and the mounts are made at the host's paths, so a ``~`` expanded the usual
    way would point at a directory that holds nothing.
    """
    text = os.fspath(path)
    if text == "~" or text.startswith("~/"):
        home = os.environ.get("MEERPIC_HOST_HOME", "").strip()
        if home:
            return Path(home + text[1:])
    return Path(os.path.expanduser(text))


class TomlSource(PydanticBaseSettingsSource):
    """meerpic.toml, flattened onto the field names above.

    Unknown sections and unknown keys are ignored rather than rejected: this
    same file is read by two processes that each only care about half of it,
    and a future version's key must not stop an older binary from starting.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path | None):
        super().__init__(settings_cls)
        self._data = self._load(path)

    @staticmethod
    def _load(path: Path | None) -> dict[str, Any]:
        if path is None or not path.is_file():
            return {}
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        out: dict[str, Any] = {}
        for (section, key), field in _FIELD_MAP.items():
            if section in raw and isinstance(raw[section], dict) and key in raw[section]:
                out[field] = raw[section][key]
        return out

    def get_field_value(self, field, field_name):  # the pydantic hook
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- database ---
    # 5434, not 5432 or 5433: meerail and meercal already have those, and the
    # three are expected to run side by side.
    database_url: str = "postgresql+psycopg://meerpic:meerpic@127.0.0.1:5434/meerpic"

    # --- server ---
    secret_key: str = "dev-insecure-secret-change-me"
    server_password: str = ""
    # The zone a photo with no zone of its own is read in. EXIF before 2016 has
    # no offset field, and most cameras never fill it, so "14:32" on a photo is
    # a wall clock somewhere; this says where. "system" is the machine's own.
    timezone: str = "system"
    trusted_proxies: list[str] = []

    # --- library ---
    # name -> folder. The name is what a photo row stores beside its relative
    # path, so a folder can move without the database noticing anything but a
    # changed line here. Several are fine: the phone's library and a folder of
    # camera imports are both photos.
    library_roots: dict[str, str] = {"icloud": "~/Pictures/iCloud"}
    # Whether there is a Delete button. It deletes here only: the files move
    # to .meerpic-trash in their library folder, and a photo from iCloud stays
    # there and on the phone (rclone can read iCloud Photos, not change them),
    # with Sync told never to download it again. See core/deletion.py.
    delete_enabled: bool = True

    # Thumbnails, video previews and the search model. Everything in it can be
    # rebuilt from the library, which is the whole test for what goes in here.
    cache_dir: str = "~/.cache/meerpic"

    # --- agent ---
    # How often the library is walked for new files. A walk of 25,000 files is
    # a few hundred milliseconds of stat() calls, so this can be short.
    agent_scan_interval: int = 60
    # Processes for the CPU-bound stages (decoding HEIC, drawing thumbnails).
    # 0 means "all but two cores", which leaves the machine usable during the
    # first index of a large library.
    agent_workers: int = 0
    exiftool: str = "exiftool"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    # --- video ---
    # Whether every video gets a browser-playable copy ahead of time ("all"),
    # or only when it is first opened ("on-demand"). iPhones record HEVC, which
    # no browser on Linux plays; see agent/video.py.
    video_previews: Literal["all", "on-demand"] = "all"
    video_height: int = 720
    video_crf: int = 23
    video_preset: str = "veryfast"

    # --- search ---
    # The model that turns photos and search words into vectors. See
    # core/embed.py for the ones that are known to work and what they cost.
    search_model: str = "google/siglip2-base-patch16-224"
    # Below this similarity a match is noise. 0 means the model's own default,
    # which is the right answer unless you have measured otherwise.
    search_min_score: float = 0.0
    similar_min_score: float = 0.0

    # --- sync ---
    sync_enabled: bool = True
    rclone: str = "rclone"
    sync_remote: str = "iclouddrive:PrimarySync/All Photos"
    # Which of library.roots the remote is copied into.
    sync_root: str = "icloud"
    # Extra rclone arguments, appended as given.
    sync_args: list[str] = []
    # Minutes between automatic syncs. 0 means only when asked: the button,
    # `make sync`, or the API. iCloud rate-limits a listing of every photo you
    # own, so this is off by default rather than a guess at a safe number.
    sync_interval: int = 0
    # After every sync, read favourites, hidden photos, Recently Deleted and
    # the album list from iCloud (rclone lsjson --metadata; see
    # agent/albums.py). A minute of listing, and nothing is downloaded.
    sync_albums: bool = True

    # --- nsfw ---
    # A dedicated classifier (a ViT fine-tuned for exactly this, ONNX, 88 MB)
    # scores every photo, pictures that apps saved first. `is:nsfw` lists
    # everything above the threshold, most certain first.
    nsfw_enabled: bool = True
    nsfw_threshold: float = 0.7
    # Tiles above the threshold are blurred in the grid until the pointer is on
    # them, so scrolling through the library with someone beside you is safe.
    nsfw_blur: bool = True
    nsfw_model: str = "AdamCodd/vit-base-nsfw-detector"
    # A photo you mark safe teaches the flag about its lookalikes, by the
    # search vectors (cosine similarity). A flagged photo at least this
    # similar to one marked safe is not flagged either: an edit and its
    # original are about 0.97 apart, unrelated photos about 0.55 and almost
    # never above 0.82. 0 turns it off.
    nsfw_clear_above: float = 0.92
    # At least this similar, and less than clear_above: still flagged, but
    # listed after everything else in `is:nsfw`. 0 turns it off.
    nsfw_demote_above: float = 0.85

    # --- faces ---
    # Every photo is searched for faces (SCRFD), and each face turned into a
    # vector (ArcFace) that is close to the vectors of the same person's other
    # faces. Clicking a face in the info panel lists the photos with a face
    # close to it (`face:`). See agent/faces.py.
    faces_enabled: bool = True
    # A Hugging Face repository with detection/model.onnx and
    # recognition/model.onnx: InsightFace's buffalo_l, as Immich exports it.
    faces_model: str = "immich-app/buffalo_l"
    # How alike two faces must be (cosine similarity, 0 to 1) to count as the
    # same person. Lower finds more of them, and sooner a stranger. Measured
    # on a family library: strangers stay under 0.25, siblings in one photo
    # reach 0.33, and one child across grimaces and face paint starts at 0.30.
    faces_min_score: float = 0.35
    # How sure the detector must be that something is a face at all.
    faces_detect_score: float = 0.7

    # --- ocr ---
    # The text in every photo (PP-OCRv6: signs, letters, receipts,
    # screenshots), for `text:`. See agent/ocr.py.
    ocr_enabled: bool = True
    # Videos too, on ten frames spread over each clip.
    ocr_videos: bool = True

    # --- map ---
    # The only thing the browser fetches from anywhere but this server. Point
    # it at your own tile server if that matters to you; empty turns the map
    # off entirely.
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_attribution: str = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    map_max_zoom: int = 19

    # --- share ---
    # What "Download JPEG" and a drag out of the window hand over. A HEIC is
    # converted, because the person the mail is for may not be on an Apple
    # device; the original is always one click further.
    share_strip_location: bool = False
    share_jpeg_quality: int = 90
    share_max_edge: int = 0            # 0 = full size

    @field_validator("library_roots")
    @classmethod
    def _roots(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("library.roots is empty: name at least one folder of photos")
        for name in v:
            if not name or "/" in name or len(name) > 64:
                raise ValueError(f"library root name {name!r} must be 1-64 characters without '/'")
        return v

    @field_validator("video_height")
    @classmethod
    def _height(cls, v: int) -> int:
        if v < 144 or v > 2160:
            raise ValueError("video.height must be between 144 and 2160")
        return v - (v % 2)

    @model_validator(mode="after")
    def _sync_root_exists(self):
        if self.sync_enabled and self.sync_root not in self.library_roots:
            have = ", ".join(sorted(self.library_roots))
            raise ValueError(
                f"sync.root is {self.sync_root!r}, which is not one of library.roots ({have})"
            )
        return self

    # --- resolved paths ---

    def root_path(self, name: str) -> Path | None:
        raw = self.library_roots.get(name)
        return expand(raw) if raw else None

    @property
    def roots(self) -> dict[str, Path]:
        return {name: expand(path) for name, path in self.library_roots.items()}

    @property
    def cache_path(self) -> Path:
        return expand(self.cache_dir)

    @property
    def models_path(self) -> Path:
        return self.cache_path / "models"

    @property
    def sync_parent(self) -> str:
        """The remote folder the albums are in: the parent of ``sync.remote``.

        ``iclouddrive:PrimarySync/All Photos`` -> ``iclouddrive:PrimarySync``.
        rclone's iCloud Photos backend lists every album as a sibling of
        "All Photos", which is the whole reason this works.
        """
        remote = self.sync_remote.rstrip("/")
        head, sep, _ = remote.rpartition("/")
        if sep:
            return head
        name, colon, _ = remote.partition(":")
        return f"{name}{colon}"

    @property
    def workers(self) -> int:
        if self.agent_workers > 0:
            return self.agent_workers
        return max(1, (os.cpu_count() or 2) - 2)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (init_settings, env_settings, dotenv_settings, TomlSource(settings_cls, config_path()))


def config_path() -> Path | None:
    """Which file to read, or None for "environment only".

    ``MEERPIC_CONFIG=""`` is not the same as unset: it is how a test run says
    "ignore whatever this developer has on their machine".
    """
    env = os.environ.get("MEERPIC_CONFIG")
    if env is not None:
        return Path(env) if env.strip() else None
    return DEFAULT_CONFIG_PATH


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    # ValidationError is a ValueError. Either way it is a broken config, which
    # should say so rather than traceback.
    except ValueError as exc:
        path = config_path()
        where = f" in {path}" if path else ""
        raise SystemExit(f"meerpic: bad configuration{where}\n\n{exc}") from exc
