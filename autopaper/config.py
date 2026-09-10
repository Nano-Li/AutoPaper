from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the local configuration is missing or invalid."""


OPENALEX_BASIC_PAGING_LIMIT = 10_000


@dataclass(frozen=True)
class Journal:
    name: str
    issn: str


@dataclass(frozen=True)
class OpenAlexSettings:
    api_key: str
    base_url: str
    network_route: str
    proxy_url: str
    timeout_seconds: float
    min_interval_seconds: float
    max_retries: int
    per_page: int
    max_pages: int


@dataclass(frozen=True)
class SearchSettings:
    query: str
    from_year: int
    to_year: int
    types: tuple[str, ...]
    require_abstract: bool
    exclude_retracted: bool
    output_dir: Path
    journals: tuple[Journal, ...]


@dataclass(frozen=True)
class Settings:
    openalex: OpenAlexSettings
    search: SearchSettings


def _required(table: dict[str, Any], key: str, section: str) -> Any:
    value = table.get(key)
    if value is None or value == "":
        raise ConfigError(f"Missing required setting: [{section}] {key}")
    return value


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Config file does not exist: {config_path}")

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    openalex = data.get("openalex", {})
    search = data.get("search", {})

    api_key = str(_required(openalex, "api_key", "openalex")).strip()
    if api_key == "replace-me":
        raise ConfigError("Replace the placeholder OpenAlex API key in the local config")

    route = str(openalex.get("network_route", "direct")).lower()
    if route not in {"direct", "proxy"}:
        raise ConfigError("[openalex] network_route must be 'direct' or 'proxy'")

    per_page = int(openalex.get("per_page", 100))
    if not 1 <= per_page <= 100:
        raise ConfigError("[openalex] per_page must be between 1 and 100")
    max_pages = int(openalex.get("max_pages", 100))
    if max_pages < 1:
        raise ConfigError("[openalex] max_pages must be at least 1")
    if per_page * max_pages > OPENALEX_BASIC_PAGING_LIMIT:
        max_allowed_pages = OPENALEX_BASIC_PAGING_LIMIT // per_page
        raise ConfigError(
            "[openalex] basic paging cannot exceed 10,000 results; "
            f"with per_page={per_page}, max_pages must be at most {max_allowed_pages}"
        )

    from_year = int(_required(search, "from_year", "search"))
    to_year = int(_required(search, "to_year", "search"))
    if from_year > to_year:
        raise ConfigError("[search] from_year cannot be later than to_year")

    journals_data = search.get("journals", [])
    journals = tuple(
        Journal(
            name=str(_required(item, "name", "search.journals")).strip(),
            issn=str(_required(item, "issn", "search.journals")).strip(),
        )
        for item in journals_data
    )
    if not journals:
        raise ConfigError("At least one [[search.journals]] entry is required")

    types = tuple(str(value).strip() for value in search.get("types", ["article"]))
    if not types:
        raise ConfigError("[search] types cannot be empty")

    return Settings(
        openalex=OpenAlexSettings(
            api_key=api_key,
            base_url=str(openalex.get("base_url", "https://api.openalex.org")).rstrip("/"),
            network_route=route,
            proxy_url=str(openalex.get("proxy_url", "http://127.0.0.1:7890")),
            timeout_seconds=float(openalex.get("timeout_seconds", 30)),
            min_interval_seconds=max(0.0, float(openalex.get("min_interval_seconds", 1.1))),
            max_retries=max(0, int(openalex.get("max_retries", 2))),
            per_page=per_page,
            max_pages=max_pages,
        ),
        search=SearchSettings(
            query=str(_required(search, "query", "search")).strip(),
            from_year=from_year,
            to_year=to_year,
            types=types,
            require_abstract=bool(search.get("require_abstract", False)),
            exclude_retracted=bool(search.get("exclude_retracted", True)),
            output_dir=Path(str(search.get("output_dir", "runs"))),
            journals=journals,
        ),
    )
