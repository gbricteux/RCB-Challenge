from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from Levenshtein import ratio


SHEET_ID = "1HO78kc9nxhosHUhIg1P_A2ulW9Me8UnsGa41yO7Ns9M"
MEMBERS_GID = "367978600"
DEFAULT_TIMEOUT = 30
ULTRATIMING_PAGE_SIZE = 100


@dataclass(frozen=True)
class Member:
    last_name: str
    first_name: str
    birth_year: str
    sex: str = ""
    name: str = ""

    @property
    def keys(self) -> tuple[str, ...]:
        return (
            normalize_name(f"{self.last_name} {self.first_name}"),
            normalize_name(f"{self.first_name} {self.last_name}"),
        )


@dataclass(frozen=True)
class RaceResult:
    position: str
    source_name: str
    finish_time: str
    category: str
    team: str
    gender: str = ""
    birth_year: str = ""


def normalize_header(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value).strip().casefold()


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-zA-Z0-9 ]", " ", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def names_match(source_name: str, member: Member) -> bool:
    normalized_source = normalize_name(source_name)
    return any(
        ratio(normalized_source, candidate_name) > 0.95
        for candidate_name in member.keys
    )


def sheet_csv_url(sheet_id: str = SHEET_ID, gid: str = MEMBERS_GID) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def load_members(csv_text: str) -> list[Member]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("The members CSV has no header row")

    headers = {normalize_header(name): name for name in reader.fieldnames if name}
    required = {"nom": "last name", "prenom": "first name", "annee naissance": "birth year"}
    missing = [label for key, label in required.items() if key not in headers]
    if missing:
        raise ValueError(f"The members CSV is missing columns: {', '.join(missing)}")

    members = []
    for row in reader:
        last_name = row[headers["nom"]].strip()
        first_name = row[headers["prenom"]].strip()
        birth_year = row[headers["annee naissance"]].strip()
        sex = row.get(headers.get("sexe", ""), "").strip()
        name = row.get(headers.get("prenom nom", ""), "").strip()
        if last_name and first_name:
            members.append(Member(last_name, first_name, birth_year, sex, name))
    return members


def _cell_text(cell) -> str:
    return " ".join(cell.stripped_strings)


def extract_race_year(html: str) -> int | None:
    year_match = re.search(r"(?:19|20)\d{2}", BeautifulSoup(html, "html.parser").get_text(" "))
    return int(year_match.group()) if year_match else None


def parse_race_results(html: str) -> list[RaceResult]:
    soup = BeautifulSoup(html, "html.parser")
    table = next(
        (
            candidate
            for candidate in soup.find_all("table")
            if {"data", "table"}.issubset(set(candidate.get("class", [])))
            or "data-table" in candidate.get("class", [])
        ),
        None,
    )
    if table is None:
        raise ValueError('Could not find a table with class="data table"')

    header_row = None
    headers = []
    for candidate in table.find_all("tr"):
        candidate_headers = [normalize_header(_cell_text(cell)) for cell in candidate.find_all(["th", "td"], recursive=False)]
        if {"pos", "nom", "temps"}.issubset(candidate_headers):
            header_row = candidate
            headers = candidate_headers
            break
    if header_row is None:
        raise ValueError("The race table has no header row")

    def column(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError as error:
            raise ValueError(f"The race table is missing the '{name}' column") from error

    position_index = column("pos")
    name_index = column("nom")
    time_index = column("temps")
    team_index = headers.index("equipe") if "equipe" in headers else None
    category_index = headers.index("cat") if "cat" in headers else None
    results = []
    for row in table.find_all("tr"):
        if row is header_row:
            continue
        cells = row.find_all("td", recursive=False)
        required_index = max(position_index, name_index, time_index, category_index or 0)
        if len(cells) <= required_index:
            continue
        category = _cell_text(cells[category_index]) if category_index is not None else ""
        team = _cell_text(cells[team_index]) if team_index is not None else ""
        results.append(
            RaceResult(
                position=_cell_text(cells[position_index]).rstrip("."),
                source_name=_cell_text(cells[name_index]),
                finish_time=_cell_text(cells[time_index]),
                category=category,
                team=team,
            )
        )
    return results


def acn_results_api_url(url: str) -> tuple[str, int | None]:
    match = re.search(r"#/events/[^/]+/ctx/([^/]+)/.*/home/([^/?#]+)", url)
    if not match:
        raise ValueError("Could not extract ctx and raceId from the ACN Timing URL")
    ctx, race_id = match.groups()
    race_year_match = re.search(r"(?:19|20)\d{2}", ctx)
    race_year = int(race_year_match.group()) if race_year_match else None
    return f"https://results.chronorace.be/api/results/table/search/{ctx}/{race_id}", race_year


def clean_api_cell(value: object) -> str:
    cell = BeautifulSoup(str(value or ""), "html.parser")
    bold = cell.find("b")
    if bold is not None:
        return " ".join(bold.stripped_strings)
    return cell.get_text(" ", strip=True)


def fetch_acn_results(url: str) -> tuple[list[RaceResult], int | None]:
    api_url, race_year = acn_results_api_url(url)
    response = requests.get(api_url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    columns = payload.get("TableDefinition", {}).get("Columns", [])
    column_names = [column.get("Name", "") for column in columns]

    def index_containing(fragment: str) -> int:
        try:
            return next(index for index, name in enumerate(column_names) if fragment in name)
        except StopIteration as error:
            raise ValueError(f"The results API is missing the {fragment} column") from error

    position_index = index_containing("_Pos")
    name_index = index_containing("#NAME")
    try:
        time_index = index_containing("#TIME")
    except ValueError:
        time_index = index_containing("#NETTO")
    category_index = index_containing("#CAT")
    team_index = next((index for index, name in enumerate(column_names) if "#TEAM" in name), None)

    rows = payload.get("Groups", [{}])[0].get("SlaveRows", [])
    results = []
    for row in rows:
        required_index = max(position_index, name_index, time_index, category_index)
        if len(row) <= required_index:
            continue
        results.append(
            RaceResult(
                position=clean_api_cell(row[position_index]).rstrip("."),
                source_name=clean_api_cell(row[name_index]),
                finish_time=clean_api_cell(row[time_index]),
                category=clean_api_cell(row[category_index]),
                team=clean_api_cell(row[team_index]) if team_index is not None else "",
            )
        )
    return results, race_year


def fetch_api_results(url: str) -> tuple[list[RaceResult], int | None]:
    """Backward-compatible name for the ACN Timing results fetcher."""
    return fetch_acn_results(url)


def ultratiming_api_url(url: str, build_id: str, page: int = 1) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    path_parts = path.split("/")
    try:
        event_index = path_parts.index("evenement")
        trial_index = path_parts.index("epreuve")
        event_slug = path_parts[event_index + 1]
        trial_slug = path_parts[trial_index + 1]
    except (ValueError, IndexError) as error:
        raise ValueError("Could not extract event and trial slugs from the Ultratiming URL") from error

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"eventSlug": event_slug, "trialSlug": trial_slug})
    if page > 1:
        query["page"] = str(page)
    else:
        query.pop("page", None)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"/_next/data/{build_id}{path}.json",
            "",
            urlencode(query),
            "",
        )
    )


def format_seconds(seconds: object) -> str:
    try:
        total_seconds = int(seconds)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid Ultratiming final time: {seconds!r}") from error
    if total_seconds < 0:
        raise ValueError(f"Invalid Ultratiming final time: {seconds!r}")
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"


def fetch_ultratiming_results(url: str) -> tuple[list[RaceResult], int | None]:
    page_response = requests.get(url, timeout=DEFAULT_TIMEOUT)
    page_response.raise_for_status()
    build_match = re.search(r'"buildId"\s*:\s*"([^"]+)"', page_response.text)
    if build_match is None:
        raise ValueError("Could not find the Next.js build ID in the Ultratiming page")

    results: list[RaceResult] = []
    page = 1
    while True:
        response = requests.get(
            ultratiming_api_url(url, build_match.group(1), page),
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        members = payload.get("pageProps", {}).get("resultsResult", {}).get("hydra:member")
        if not isinstance(members, list):
            raise ValueError("The Ultratiming response has no results collection")

        for participation in members:
            user = participation.get("user") or {}
            first_name = str(user.get("firstName") or "").strip()
            last_name = str(user.get("lastName") or "").strip()
            if not first_name or not last_name:
                continue
            cleaned_first_name = normalize_name(first_name)
            cleaned_last_name = normalize_name(last_name)
            birth_date = str(user.get("birthDate") or "")
            birth_year_match = re.match(r"(?:19|20)\d{2}", birth_date)
            results.append(
                RaceResult(
                    position=str(participation.get("rank", "")),
                    source_name=f"{cleaned_last_name} {cleaned_first_name}",
                    finish_time=format_seconds(participation.get("finalTime")),
                    category=str(participation.get("category") or participation.get("gender") or ""),
                    team=str(participation.get("club") or "").strip(),
                    gender=str(participation.get("gender") or user.get("gender") or ""),
                    birth_year=birth_year_match.group() if birth_year_match else "",
                )
            )

        if len(members) < ULTRATIMING_PAGE_SIZE:
            break
        page += 1

    race_year_match = re.search(r"(?:19|20)\d{2}", url)
    race_year = int(race_year_match.group()) if race_year_match else None
    return results, race_year


def category_matches(member: Member, category: str, race_year: int | None = None) -> bool:
    category = normalize_header(category)
    category_sex: str
    minimum_age: int | None = None
    maximum_age: int | None = None
    senior_category = False
    if category in {"msr", "vsr"}:
        category_sex = "m" if category == "msr" else "f"
    else:
        under_age_match = re.fullmatch(r"u(\d+)([mf])", category)
        master_match = re.fullmatch(r"m(\d+)([mf])", category)
        senior_match = re.fullmatch(r"se([mf])", category)
        legacy_age_match = re.fullmatch(r"([mfv])(\d+)", category)
        legacy_senior_match = re.fullmatch(r"se([hf])", category)
        if under_age_match:
            maximum_age = int(under_age_match.group(1))
            category_sex = under_age_match.group(2)
        elif master_match:
            minimum_age = int(master_match.group(1))
            category_sex = master_match.group(2)
        elif senior_match:
            category_sex = senior_match.group(1)
            senior_category = True
        elif legacy_age_match:
            minimum_age = int(legacy_age_match.group(2))
            category_sex = {"m": "m", "f": "f", "v": "f"}[legacy_age_match.group(1)]
        elif legacy_senior_match:
            category_sex = {"h": "m", "f": "f"}[legacy_senior_match.group(1)]
            maximum_age = 30
        else:
            return False
    member_sex = normalize_header(member.sex)
    member_sex = {"homme": "m", "h": "m", "male": "m", "monsieur": "m", "femme": "f", "female": "f", "dame": "f", "d": "f"}.get(member_sex, member_sex[:1])
    if member_sex != category_sex:
        return False
    if minimum_age is None and maximum_age is None:
        return True
    if race_year is None or not member.birth_year.isdigit():
        return False
    age = race_year - int(member.birth_year)
    if senior_category:
        return age >= 18
    if maximum_age is not None:
        return age < maximum_age
    return minimum_age is not None and age >= minimum_age


def ultratiming_member_matches(member: Member, result: RaceResult) -> bool:
    if not result.gender or not result.birth_year:
        return False
    member_sex = normalize_header(member.sex)
    member_sex = {
        "homme": "m",
        "h": "m",
        "male": "m",
        "monsieur": "m",
        "femme": "f",
        "female": "f",
        "dame": "f",
        "d": "f",
    }.get(member_sex, member_sex[:1])
    result_sex = normalize_header(result.gender)[:1]
    return member_sex == result_sex and member.birth_year == result.birth_year


def match_members(
    members: Iterable[Member], results: Iterable[RaceResult], race_year: int | None = None
) -> list[dict[str, str]]:
    members_by_name: dict[str, list[Member]] = {}
    for member in members:
        for key in member.keys:
            members_by_name.setdefault(key, []).append(member)

    matched = []
    for result in results:
        normalized_result_name = normalize_name(result.source_name)
        candidates = members_by_name.get(normalized_result_name, [])
        if not candidates:
            candidates = [
                roster_member
                for member_group in members_by_name.values()
                for roster_member in member_group
                if names_match(result.source_name, roster_member)
            ]
        member = next(
            (
                candidate
                for candidate in candidates
                if category_matches(candidate, result.category, race_year)
                or ultratiming_member_matches(candidate, result)
            ),
            None,
        )
        if member is not None:
            matched.append(
                {
                    "position": result.position,
                    "name": member.name,
                    "time": result.finish_time,
                    "team": result.team,
                    "race_name": result.source_name,
                    "birth_year": member.birth_year,
                    "category": result.category
                }
            )
        elif "rcb" in result.team.casefold() and not "rcbt" in result.team.casefold():
            name_parts = result.source_name.split()
            if len(name_parts) > 1:
                first_name = " ".join(name_parts[1:])
                last_name = name_parts[0]
            else:
                first_name = result.source_name
                last_name = ""
            print(f"Racer \"{first_name} {last_name}\" ran with team \"{result.team}\" but is not in list of members")
    return matched


def fetch_text(url: str) -> str:
    response = requests.get(url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    text = response.text
    if "data-table" not in text and "class=\"data table\"" not in text:
        if "<div id=\"app\">" in text or "type=\"module\"" in text:
            raise ValueError(
                "The ACN Timing URL returned the JavaScript app shell, not the rendered results table. "
                "Use a saved rendered HTML file, for example: python club_results.py herou_25_2026.html"
            )
    return text


def write_csv(rows: Iterable[dict[str, str]], output: TextIO) -> None:
    fieldnames = ["position", "name", "time", "team", "race_name", "birth_year", "category"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract club members from an ACN Timing race result")
    parser.add_argument("race", help="Race-results URL or path to a saved HTML file")
    parser.add_argument("-o", "--output", type=Path, help="CSV output path; stdout when omitted")
    parser.add_argument("--members-csv", type=Path, help="Use a local members CSV instead of Google Sheets")
    parser.add_argument("--sheet-id", default=SHEET_ID)
    parser.add_argument("--members-gid", default=MEMBERS_GID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        members_text = args.members_csv.read_text(encoding="utf-8") if args.members_csv else fetch_text(sheet_csv_url(args.sheet_id, args.members_gid))
        race_path = Path(args.race)
        if race_path.is_file():
            race_text = race_path.read_text(encoding="utf-8")
            results = parse_race_results(race_text)
            race_year = extract_race_year(race_text)
        else:
            race_url = args.race.casefold()
            if "ultratiming" in race_url:
                results, race_year = fetch_ultratiming_results(args.race)
            elif "acn-timing" in race_url:
                results, race_year = fetch_acn_results(args.race)
            else:
                raise ValueError("Race URL must contain 'acn-timing' or 'ultratiming'")
        rows = match_members(load_members(members_text), results, race_year=race_year)
        if args.output:
            with args.output.open("w", newline="", encoding="utf-8") as output:
                write_csv(rows, output)
        else:
            write_csv(rows, sys.stdout)
    except (OSError, requests.RequestException, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())