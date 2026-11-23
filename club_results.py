from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import subprocess
import time
import unicodedata
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TextIO
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from Levenshtein import ratio


SHEET_ID = "1HO78kc9nxhosHUhIg1P_A2ulW9Me8UnsGa41yO7Ns9M"
MEMBERS_GID = "367978600"
DEFAULT_TIMEOUT = 30
ULTRATIMING_PAGE_SIZE = 100
SPORTEASY_MEMBERS_URL = "https://rcb-gal-membres.sporteasy.net/members/list/"
MEMBERS_ROUTE_GID = "1834814752"
MEMBERS_TRAIL_GID = "497718625"
MEMBERS_GALOPOINTS_GID = "1858177828"


@dataclass(frozen=True)
class Member:
    last_name: str
    first_name: str
    birth_year: str
    sex: str = ""
    name: str = ""
    birth_date: str = ""

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
    rows = list(csv.reader(io.StringIO(csv_text)))
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if {normalize_header(value) for value in row}
            >= {"nom", "prenom", "annee naissance"}
        ),
        None,
    )
    if header_index is None:
        raise ValueError("The members CSV has no header row")

    fieldnames = rows[header_index]
    headers = {normalize_header(name): name for name in fieldnames if name}
    required = {"nom": "last name", "prenom": "first name", "annee naissance": "birth year"}
    missing = [label for key, label in required.items() if key not in headers]
    if missing:
        raise ValueError(f"The members CSV is missing columns: {', '.join(missing)}")

    members = []
    for values in rows[header_index + 1:]:
        row = {
            fieldnames[index]: values[index]
            for index in range(min(len(fieldnames), len(values)))
            if fieldnames[index]
        }
        last_name = row.get(headers["nom"], "").strip()
        first_name = row.get(headers["prenom"], "").strip()
        birth_year = row.get(headers["annee naissance"], "").strip()
        sex = row.get(headers.get("sexe", ""), "").strip()
        name = row.get(headers.get("prenom nom", ""), "").strip()
        if not name:
            name = f"{last_name} {first_name}"
        if last_name and first_name:
            members.append(Member(last_name, first_name, birth_year, sex, name))
    return members


def load_env_file(path: Path = Path(".env")) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_sporteasy_members_html(html: str) -> list[Member]:
    soup = BeautifulSoup(html, "html.parser")
    members: list[Member] = []
    seen: set[tuple[str, str, str]] = set()

    left_rows = soup.select("div.LeftRow")
    center_rows = soup.select("div.CenterRow")
    if left_rows:
        for index, left_row in enumerate(left_rows):
            name_elements = left_row.select("div.name")
            if len(name_elements) < 2:
                continue
            last_name = name_elements[0].get_text(" ", strip=True)
            first_name = name_elements[1].get_text(" ", strip=True)

            birth_date = ""
            center_row = center_rows[index] if index < len(center_rows) else None
            search_rows = [center_row, left_row] if center_row is not None else [left_row]
            for row in search_rows:
                for match in re.finditer(r"\b\d{2}/\d{2}/\d{4}\b", row.get_text(" ", strip=True)):
                    birth_date = match.group(0)
                    break
                if birth_date:
                    break

            birth_match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", birth_date)
            if not last_name or not first_name or birth_match is None:
                continue
            key = (normalize_name(last_name), normalize_name(first_name), birth_date)
            if key in seen:
                continue
            seen.add(key)
            members.append(Member(last_name, first_name, birth_match.group(3), name=f"{first_name} {last_name}", birth_date=birth_date))
        return members

    for row in soup.select("div.cWFlMQ0NOckEHU0GB72t"):
        cells = row.find_all("div", recursive=False)
        name_elements = row.select("div.name")
        if len(name_elements) >= 2:
            last_name = name_elements[0].get_text(" ", strip=True)
            first_name = name_elements[1].get_text(" ", strip=True)
        elif len(cells) >= 4:
            last_name = cells[2].get_text(" ", strip=True)
            first_name = cells[3].get_text(" ", strip=True)
        else:
            continue
        birth_match = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", row.get_text(" ", strip=True))
        if not last_name or not first_name or birth_match is None:
            continue
        birth_date = birth_match.group(0)
        key = (normalize_name(last_name), normalize_name(first_name), birth_date)
        if key in seen:
            continue
        seen.add(key)
        members.append(Member(last_name, first_name, birth_match.group(3), name=f"{first_name} {last_name}", birth_date=birth_date))
    return members


def normalize_sheet_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def build_google_member_row(member: Member, sex: str, row_number: int) -> list[str]:
    sex = sex.strip().upper()
    if sex not in {"H", "D"}:
        raise ValueError("Member sex must be H or D")
    normalized_last = normalize_sheet_name(member.last_name)
    normalized_first = normalize_sheet_name(member.first_name)
    normalized_name = f"{normalized_first} {normalized_last}"
    formula = (
        f'=SWITCH(F{row_number};"H";SWITCH(INT((2026-E{row_number})/10);'
        f'1;"ESH";2;"SH1";3;"SH2";4;"V1";5;"V2";6;"V3";7;"V4";8;"V5";"");'
        f'"D";SWITCH(INT((2026-E{row_number})/10);'
        f'1;"ESD";2;"SD1";3;"SD2";4;"A1";5;"A2";6;"A3";7;"A4";8;"A5";""))'
    )
    return [
        normalized_last,
        normalized_first,
        normalized_name,
        f"{member.first_name} {member.last_name}",
        member.birth_year,
        sex,
        formula,
    ]


def find_google_members_insertion_row(csv_text: str, member: Member) -> int:
    rows = list(csv.reader(io.StringIO(csv_text)))
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if "nom" in {normalize_sheet_name(value) for value in row}
            and "prenom" in {normalize_sheet_name(value) for value in row}
        ),
        None,
    )
    if header_index is None:
        raise ValueError("The Google members CSV has no header row")

    header = [normalize_sheet_name(value) for value in rows[header_index]]
    first_index = header.index("prenom")
    last_index = header.index("nom")
    member_key = (
        normalize_name(member.first_name),
        normalize_name(member.last_name),
    )

    last_member_row = header_index + 1
    for row_index, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if len(row) <= max(first_index, last_index):
            continue
        sheet_last_name = normalize_name(row[last_index])
        sheet_first_name = normalize_name(row[first_index])
        if not sheet_last_name or not sheet_first_name:
            continue

        last_member_row = row_index
        sheet_key = (sheet_first_name, sheet_last_name)
        if sheet_key > member_key:
            return row_index

    return last_member_row + 1


def google_name_box_selects_row(name_box_value: str, row_number: int) -> bool:
    value = name_box_value.strip().upper().replace("$", "").replace(" ", "")
    if re.fullmatch(rf"{row_number}:{row_number}", value):
        return True

    range_match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", value)
    if range_match:
        start_row = int(range_match.group(2))
        end_row = int(range_match.group(4))
        return start_row <= row_number <= end_row

    cell_match = re.fullmatch(r"[A-Z]+(\d+)", value)
    return bool(cell_match and int(cell_match.group(1)) == row_number)


def google_name_box_selects_cell(name_box_value: str, column: str, row_number: int) -> bool:
    value = name_box_value.strip().upper().replace("$", "").replace(" ", "")
    if "!" in value:
        value = value.rsplit("!", 1)[1]

    expected = f"{column.upper()}{row_number}"
    if value == expected:
        return True

    range_match = re.fullmatch(
        rf"({re.escape(expected)}):({re.escape(expected)})",
        value,
    )
    return range_match is not None


def find_challenge_insertion_row(csv_text: str, participant_name: str, gid: str) -> int | None:
    rows = list(csv.reader(io.StringIO(csv_text)))
    name_column = 0 if gid == MEMBERS_GALOPOINTS_GID else 2
    target = normalize_name(participant_name)

    for row_number, row in enumerate(rows, start=1):
        if len(row) <= name_column:
            continue
        if normalize_name(row[name_column]) == target:
            return row_number
    return None


def get_main_successor(member: Member, members: Iterable[Member]) -> Member | None:
    ordered = sorted(
        members,
        key=lambda candidate: (
            normalize_name(candidate.first_name),
            normalize_name(candidate.last_name),
        ),
    )
    member_key = (
        normalize_name(member.first_name),
        normalize_name(member.last_name),
    )
    for index, candidate in enumerate(ordered):
        candidate_key = (
            normalize_name(candidate.first_name),
            normalize_name(candidate.last_name),
        )
        if candidate_key > member_key:
            return candidate
    return None


def find_missing_members_in_tab(
    csv_text: str,
    gid: str,
    sporteasy_members: Iterable[Member],
) -> list[str]:
    members = list(sporteasy_members)

    if gid == MEMBERS_GID:
        tab_members = load_members(csv_text)
        present_names = {
            normalize_name(f"{member.first_name} {member.last_name}")
            for member in tab_members
        }
        present_names.update(
            normalize_name(f"{member.last_name} {member.first_name}")
            for member in tab_members
        )
    else:
        rows = list(csv.reader(io.StringIO(csv_text)))
        name_column = 0 if gid == MEMBERS_GALOPOINTS_GID else 2
        present_names = {
            normalize_name(row[name_column])
            for row in rows
            if len(row) > name_column and normalize_name(row[name_column])
        }

    missing: list[str] = []
    seen: set[str] = set()
    for member in members:
        first_last = normalize_name(f"{member.first_name} {member.last_name}")
        last_first = normalize_name(f"{member.last_name} {member.first_name}")
        if first_last not in present_names and last_first not in present_names:
            label = f"{member.first_name} {member.last_name}"
            key = normalize_name(label)
            if key not in seen:
                seen.add(key)
                missing.append(label)

    missing.sort(key=normalize_name)
    return missing


def find_extra_members_in_tab(
    csv_text: str,
    gid: str,
    sporteasy_members: Iterable[Member],
) -> list[str]:
    members = list(sporteasy_members)
    sporteasy_names = {
        normalized_name
        for member in members
        for normalized_name in (
            normalize_name(f"{member.first_name} {member.last_name}"),
            normalize_name(f"{member.last_name} {member.first_name}"),
        )
    }

    if gid == MEMBERS_GID:
        tab_names = [
            f"{member.first_name} {member.last_name}"
            for member in load_members(csv_text)
        ]
    else:
        rows = list(csv.reader(io.StringIO(csv_text)))
        name_column = 0 if gid == MEMBERS_GALOPOINTS_GID else 2
        tab_names = [
            row[name_column].strip()
            for row in rows
            if len(row) > name_column
            and row[name_column].strip()
            and normalize_name(row[name_column])
            and normalize_name(row[name_column]) not in {"participant", "name", "nom", "prenom nom"}
        ]

    extras: list[str] = []
    seen: set[str] = set()
    for name in tab_names:
        normalized_name = normalize_name(name)
        if normalized_name not in sporteasy_names:
            if normalized_name not in seen:
                seen.add(normalized_name)
                extras.append(name)

    extras.sort(key=normalize_name)
    return extras


def find_duplicate_names(csv_text: str, gid: str) -> list[str]:
    rows = list(csv.reader(io.StringIO(csv_text)))

    if gid == MEMBERS_GID:
        header_index = next(
            (
                index
                for index, row in enumerate(rows)
                if "prénom nom" in {normalize_header(value) for value in row}
                or "prenom nom" in {normalize_header(value) for value in row}
            ),
            None,
        )
        if header_index is None:
            return []
        header = [normalize_header(value) for value in rows[header_index]]
        column_indexes = [index for index, value in enumerate(header) if value in {"prénom nom", "prenom nom"}]
        column_index = column_indexes[0] if column_indexes else 2
        data_rows = rows[header_index + 1:]
    else:
        column_index = 2
        data_rows = rows

    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for row in data_rows:
        if len(row) <= column_index:
            continue
        value = re.sub(r"\s+", " ", row[column_index]).strip()
        if not value:
            continue
        key = normalize_name(value)
        counts[key] = counts.get(key, 0) + 1
        labels.setdefault(key, value)

    return [labels[key] for key, count in counts.items() if count > 1]


def sync_sporteasy_members_to_google_sheet(
    sporteasy_members: Iterable[Member],
    member_sexes: dict[tuple[str, str], str],
    sheet_id: str = SHEET_ID,
) -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise ValueError("Playwright is required to update the Google Sheet") from error

    sheet_text = fetch_text(sheet_csv_url(sheet_id, MEMBERS_GID))
    sheet_members = load_members(sheet_text)
    ordered_members = list(sheet_members)

    approved: list[Member] = []
    sporteasy_members = list(sporteasy_members)
    sporteasy_by_key = {
        (normalize_name(member.last_name), normalize_name(member.first_name)): member
        for member in sporteasy_members
    }
    for key, sex in member_sexes.items():
        member = sporteasy_by_key.get(key)
        if member is not None:
            approved.append(Member(member.last_name, member.first_name, member.birth_year, sex, member.name, member.birth_date))

    if not approved:
        duplicate_summary: dict[str, list[str]] = {}
        tab_gids = {
            "Membres": MEMBERS_GID,
            "Challenge Route": MEMBERS_ROUTE_GID,
            "Challenge Trail": MEMBERS_TRAIL_GID,
        }
        for tab_name, gid in tab_gids.items():
            tab_csv = fetch_text(sheet_csv_url(sheet_id, gid))
            duplicates = find_duplicate_names(tab_csv, gid)
            if duplicates:
                duplicate_summary[tab_name] = duplicates
                warnings.warn(
                    f"Duplicate names found in {tab_name} column C: "
                    + ", ".join(duplicates),
                    RuntimeWarning,
                    stacklevel=2,
                )
        if not duplicate_summary:
            print("Duplicate check passed: no duplicates found in column C of Membres, Route, or Trail.")

        member_presence_tabs = {
            "Membres": MEMBERS_GID,
            "Challenge Route": MEMBERS_ROUTE_GID,
            "Challenge Trail": MEMBERS_TRAIL_GID,
            "Galopoints": MEMBERS_GALOPOINTS_GID,
        }
        for tab_name, gid in member_presence_tabs.items():
            tab_csv = fetch_text(sheet_csv_url(sheet_id, gid))
            missing_members = find_missing_members_in_tab(tab_csv, gid, sporteasy_members)
            if missing_members:
                print(f"Missing SportEasy members in {tab_name}: {', '.join(missing_members)}")
            else:
                print(f"All SportEasy members are present in {tab_name}.")

            extra_members = find_extra_members_in_tab(tab_csv, gid, sporteasy_members)
            if extra_members:
                print(f"Members in {tab_name} but not in SportEasy: {', '.join(extra_members)}")
            else:
                print(f"No members in {tab_name} are missing from SportEasy.")

        return []

    profile_dir = Path(".google-chrome-profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    google_status_url = "http://127.0.0.1:9222/json/version"

    import requests

    chrome_binary = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    debug_port = 9222
    google_status_url = f"http://127.0.0.1:{debug_port}/json/version"

    try:
        requests.get(google_status_url, timeout=1).raise_for_status()
    except requests.RequestException:
        if not Path(chrome_binary).is_file():
            raise ValueError("Google Chrome was not found")
        subprocess.Popen([
            chrome_binary,
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                requests.get(google_status_url, timeout=1).raise_for_status()
                break
            except requests.RequestException:
                time.sleep(1)
        else:
            raise ValueError("Could not start Google Chrome")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
        try:
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                pages = context.pages
                page = pages[0] if pages else context.new_page()
            else:
                context = browser.new_context()
                page = context.new_page()

            def open_sheet_page(gid: str) -> None:
                sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={gid}"
                try:
                    page.goto(sheet_url, wait_until="commit", timeout=120_000)
                except Exception as error:
                    if "ERR_ABORTED" not in str(error):
                        raise

                login_prompt_shown = False
                deadline = 300
                while deadline > 0:
                    try:
                        page.wait_for_selector("#t-name-box", timeout=5_000)
                        if page.locator("#t-name-box").is_visible():
                            return
                    except Exception:
                        pass

                    if "accounts.google.com" in page.url or "google.com/signin" in page.url:
                        if not login_prompt_shown:
                            print("Sign in to Google in the normal Chrome window; do not close it. Waiting for the sheet...", flush=True)
                            login_prompt_shown = True
                    page.wait_for_timeout(5_000)
                    deadline -= 1

                raise ValueError("Google Sheet did not become editable; sign in or open the sheet with permission")

            open_sheet_page(MEMBERS_GID)

            def open_sheet(gid: str) -> None:
                open_sheet_page(gid)

            def insert_row_above(row_number: int) -> None:
                name_box = page.locator("#t-name-box")
                for _ in range(3):
                    name_box.fill(f"A{row_number}:G{row_number}")
                    name_box.press("Enter")
                    page.wait_for_timeout(500)
                    if google_name_box_selects_row(name_box.input_value(), row_number):
                        break
                    page.wait_for_timeout(500)
                else:
                    raise ValueError(
                        f"Could not place the cursor on row {row_number} before inserting a line"
                    )

                try:
                    page.keyboard.press("Shift+Space")
                    page.wait_for_timeout(300)
                    page.get_by_role(
                        "menuitem", name=re.compile(r"Insertion|Insert", re.I)
                    ).click(timeout=3_000)
                    page.get_by_role(
                        "menuitem", name=re.compile(r"Lignes|Rows", re.I)
                    ).hover(timeout=3_000)
                    page.get_by_role(
                        "menuitem",
                        name=re.compile(r"Insérer une ligne au-dessus|Insert (?:one )?row above", re.I),
                    ).click(timeout=3_000)
                except Exception:
                    page.keyboard.press("Escape")
                    page.keyboard.press("Meta+Alt+=")

                page.wait_for_timeout(3_000)

            def set_cell(cell: str, value: str) -> None:
                name_box = page.locator("#t-name-box")
                name_box.fill(cell)
                name_box.press("Enter")
                page.wait_for_timeout(250)
                page.keyboard.press("F2")
                page.keyboard.press("Control+A")
                page.keyboard.insert_text(value)
                page.keyboard.press("Enter")
                page.wait_for_timeout(500)

            inserted_names: list[str] = []
            for member in approved:
                sheet_text = fetch_text(sheet_csv_url(sheet_id, MEMBERS_GID))
                insertion_row = find_google_members_insertion_row(sheet_text, member)

                open_sheet(MEMBERS_GID)
                insert_row_above(insertion_row)
                row = build_google_member_row(member, member.sex, insertion_row)
                for column, value in zip("ABCDEF", row):
                    set_cell(f"{column}{insertion_row}", value)

                name_box = page.locator("#t-name-box")
                name_box.fill(f"G{insertion_row - 1}:G{insertion_row}")
                name_box.press("Enter")
                page.keyboard.press("Meta+d")

                page.wait_for_timeout(10_000)
                try:
                    name_box.fill(f"A{insertion_row}:G{insertion_row}")
                    name_box.press("Enter")
                    page.wait_for_timeout(2_000)
                except Exception:
                    pass

                ordered_members.append(member)
                successor = get_main_successor(member, ordered_members)
                successor_name = (
                    f"{successor.first_name} {successor.last_name}" if successor is not None else None
                )

                for gid in (MEMBERS_ROUTE_GID, MEMBERS_TRAIL_GID):
                    open_sheet(gid)
                    page.wait_for_timeout(5_000)
                    challenge_text = fetch_text(sheet_csv_url(sheet_id, gid))
                    challenge_row = (
                        find_challenge_insertion_row(challenge_text, successor_name, gid)
                        if successor_name is not None
                        else None
                    )
                    if challenge_row is None:
                        warnings.warn(
                            f"Could not find successor {successor_name or '<none>'} in challenge tab {gid}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                        continue
                    insert_row_above(challenge_row)

                open_sheet(MEMBERS_GALOPOINTS_GID)
                page.wait_for_timeout(5_000)
                challenge_text = fetch_text(sheet_csv_url(sheet_id, MEMBERS_GALOPOINTS_GID))
                challenge_row = (
                    find_challenge_insertion_row(challenge_text, successor_name, MEMBERS_GALOPOINTS_GID)
                    if successor_name is not None
                    else None
                )
                if challenge_row is not None:
                    insert_row_above(challenge_row)
                else:
                    warnings.warn(
                        f"Could not find successor {successor_name or '<none>'} in Galopoints",
                        RuntimeWarning,
                        stacklevel=2,
                    )

                    page.wait_for_timeout(3_000)

                inserted_names.append(f"{member.first_name} {member.last_name}")

            duplicate_summary: dict[str, list[str]] = {}
            tab_gids = {
                "Membres": MEMBERS_GID,
                "Challenge Route": MEMBERS_ROUTE_GID,
                "Challenge Trail": MEMBERS_TRAIL_GID,
            }
            for tab_name, gid in tab_gids.items():
                tab_csv = fetch_text(sheet_csv_url(sheet_id, gid))
                duplicates = find_duplicate_names(tab_csv, gid)
                if duplicates:
                    duplicate_summary[tab_name] = duplicates
                    warnings.warn(
                        f"Duplicate names found in {tab_name} column C: "
                        + ", ".join(duplicates),
                        RuntimeWarning,
                        stacklevel=2,
                    )
            if not duplicate_summary:
                print("Duplicate check passed: no duplicates found in column C of Membres, Route, or Trail.")

            member_presence_tabs = {
                "Membres": MEMBERS_GID,
                "Challenge Route": MEMBERS_ROUTE_GID,
                "Challenge Trail": MEMBERS_TRAIL_GID,
                "Galopoints": MEMBERS_GALOPOINTS_GID,
            }
            for tab_name, gid in member_presence_tabs.items():
                tab_csv = fetch_text(sheet_csv_url(sheet_id, gid))
                missing_members = find_missing_members_in_tab(
                    tab_csv,
                    gid,
                    sporteasy_members,
                )
                if missing_members:
                    print(f"Missing SportEasy members in {tab_name}: {', '.join(missing_members)}")
                else:
                    print(f"All SportEasy members are present in {tab_name}.")

                extra_members = find_extra_members_in_tab(
                    tab_csv,
                    gid,
                    sporteasy_members,
                )
                if extra_members:
                    print(f"Members in {tab_name} but not in SportEasy: {', '.join(extra_members)}")
                else:
                    print(f"No members in {tab_name} are missing from SportEasy.")

            def sort_challenge_table(gid: str) -> None:
                open_sheet(gid)
                page.wait_for_timeout(2_000)

                data_menu = page.get_by_role(
                    "menuitem",
                    name=re.compile(r"^(?:Données|Data)$", re.I),
                )
                data_menu.click(timeout=5_000)

                sort_sheet_item = page.get_by_role(
                    "menuitem",
                    name=re.compile(r"Trier une feuille|Sort sheet", re.I),
                )
                sort_sheet_item.click(timeout=5_000)

                sort_by_column_a_item = page.get_by_role(
                    "menuitem",
                    name=re.compile(
                        r"Trier la feuille à partir de la colonne A.*(?:De A à Z|A to Z)|"
                        r"Sort sheet by column A.*(?:A to Z)",
                        re.I,
                    ),
                )
                sort_by_column_a_item.first.click(timeout=5_000)
                page.wait_for_timeout(3_000)

            if inserted_names:
                for gid in (MEMBERS_ROUTE_GID, MEMBERS_TRAIL_GID):
                    sort_challenge_table(gid)

        finally:
            browser.close()

    return inserted_names

def fetch_sporteasy_members(url: str = SPORTEASY_MEMBERS_URL) -> list[Member]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise ValueError("Playwright is required to fetch SportEasy members") from error

    env = load_env_file()
    username = env.get("SPORTEASY_USERNAME")
    password = env.get("SPORTEASY_PASSWORD")
    if not username or not password:
        raise ValueError("SPORTEASY_USERNAME and SPORTEASY_PASSWORD must be set in .env")

    members: list[Member] = []
    seen: set[tuple[str, str, str]] = set()

    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        cache_root = Path.home() / "Library/Caches/ms-playwright"
        cached_browsers = list(cache_root.glob("chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"))
        if cached_browsers:
            launch_options["executable_path"] = str(cached_browsers[-1])
        browser = playwright.chromium.launch(**launch_options)
        try:
            page = browser.new_page()
            try:
                page.goto(url, wait_until="commit", timeout=120_000)
            except Exception as error:
                page.wait_for_timeout(5_000)
                if "/members/list" not in page.url and "Sporteasy" not in page.title():
                    raise ValueError("Could not open the SportEasy members page") from error
            page.wait_for_timeout(5_000)

            agree = page.get_by_role("button", name="Agree and close")
            if agree.count():
                agree.click()

            username_input = page.get_by_placeholder("john.doe@email.com")
            try:
                username_input.wait_for(state="visible", timeout=30_000)
            except Exception:
                username_input = None

            if username_input is not None:
                username_input.fill(username)
                password_input = page.get_by_placeholder("****************")
                password_input.wait_for(state="visible", timeout=10_000)
                password_input.fill(password)
                login_button = page.get_by_role("button", name="Log in", exact=True)
                login_button.wait_for(state="visible", timeout=10_000)
                login_button.click()
                page.wait_for_url("**/members/list/**", timeout=60_000)

            try:
                page.locator("[data-cy='scrollable-table-row'], div.LeftRow").first.wait_for(
                    state="visible", timeout=30_000
                )
            except Exception:
                page.wait_for_timeout(5_000)

            size_100 = page.get_by_role("button", name="100", exact=True)
            if size_100.count():
                size_100.click()
                page.wait_for_timeout(5_000)

            for html in [page.content()]:
                for member in parse_sporteasy_members_html(html):
                    key = (normalize_name(member.last_name), normalize_name(member.first_name), member.birth_date)
                    if key not in seen:
                        seen.add(key)
                        members.append(member)

            body_text = page.locator("body").inner_text(timeout=10_000)
            total_match = re.search(r"(\d+)\s+members?", body_text, re.IGNORECASE)
            if total_match is None:
                total_match = re.search(r"(\d+) membres?", body_text, re.IGNORECASE)
            page_count = max(1, (int(total_match.group(1)) + 99) // 100) if total_match else 1

            for page_number in range(2, page_count + 1):
                page_controls = page.locator('[role="button"]')
                control_found = False
                for index in range(page_controls.count()):
                    control = page_controls.nth(index)
                    try:
                        text = control.inner_text(timeout=500).strip()
                    except Exception:
                        continue
                    if text == str(page_number):
                        control.click()
                        control_found = True
                        break
                if not control_found:
                    warnings.warn(
                        f"Could not find SportEasy pagination control for page {page_number}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    break
                page.wait_for_timeout(5_000)
                for member in parse_sporteasy_members_html(page.content()):
                    key = (normalize_name(member.last_name), normalize_name(member.first_name), member.birth_date)
                    if key not in seen:
                        seen.add(key)
                        members.append(member)
        finally:
            browser.close()

    if not members:
        raise ValueError("Could not extract any members from the SportEasy page")
    return members


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


def clean_api_name_cell(value: object) -> tuple[str, str]:
    cell = BeautifulSoup(str(value or ""), "html.parser")
    bold = cell.find("b")
    name = " ".join(bold.stripped_strings) if bold is not None else cell.get_text(" ", strip=True)
    team_element = cell.find("small")
    team = team_element.get_text(" ", strip=True) if team_element is not None else ""
    return name, team


def _select_acn_time_index(
    column_names: list[str], input_fn: Callable[[str], str] | None = None
) -> int:
    for fragment in ("#TIME", "#NETTO", "#OFFICIEL"):
        fragment_casefolded = fragment.casefold()
        for index, name in enumerate(column_names):
            if fragment_casefolded in name.casefold():
                return index

    available_columns = "\n".join(
        f"{index + 1}. {name}" for index, name in enumerate(column_names)
    )
    prompt = (
        "Could not identify the ACN results time column. Choose its number or exact "
        f"column name:\n{available_columns}\nTime column: "
    )
    read_input = input if input_fn is None else input_fn
    while True:
        try:
            choice = read_input(prompt).strip()
        except EOFError as error:
            raise ValueError("A time column must be selected from the ACN results") from error
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(column_names):
                return index
        else:
            try:
                return column_names.index(choice)
            except ValueError:
                pass
        print("Please enter a listed column number or its exact name.", file=sys.stderr)


def fetch_acn_results(
    url: str, input_fn: Callable[[str], str] | None = None
) -> tuple[list[RaceResult], int | None]:
    api_url, race_year = acn_results_api_url(url)
    if race_year is None:
        warnings.warn(
            "Could not determine the race year from the ACN Timing URL",
            RuntimeWarning,
            stacklevel=2,
        )
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
    time_index = _select_acn_time_index(column_names, input_fn)
    category_index = index_containing("#CAT")
    team_index = next((index for index, name in enumerate(column_names) if "#TEAM" in name), None)

    rows = payload.get("Groups", [{}])[0].get("SlaveRows", [])
    results = []
    for row in rows:
        required_index = max(position_index, name_index, time_index, category_index)
        if len(row) <= required_index:
            continue
        source_name, embedded_team = clean_api_name_cell(row[name_index])
        results.append(
            RaceResult(
                position=clean_api_cell(row[position_index]).rstrip("."),
                source_name=source_name,
                finish_time=clean_api_cell(row[time_index]),
                category=clean_api_cell(row[category_index]),
                team=(
                    clean_api_cell(row[team_index])
                    if team_index is not None
                    else embedded_team
                ),
            )
        )
    return results, race_year


def fetch_api_results(
    url: str, input_fn: Callable[[str], str] | None = None
) -> tuple[list[RaceResult], int | None]:
    """Backward-compatible name for the ACN Timing results fetcher."""
    return fetch_acn_results(url, input_fn=input_fn)


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
            if not participation.get("finalTime"):
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
    if race_year_match is None:
        warnings.warn(
            "Could not determine the race year from the Ultratiming URL",
            RuntimeWarning,
            stacklevel=2,
        )
        race_year = None
    else:
        race_year = int(race_year_match.group())
    return results, race_year


def category_matches(member: Member, category: str, race_year: int | None = None) -> bool:
    category = normalize_header(category)
    category_sex: str
    if category in {"msr", "vsr"}:
        category_sex = "m" if category == "msr" else "f"
    else:
        under_age_match = re.fullmatch(r"u(\d+)([mf])", category)
        master_match = re.fullmatch(r"m(\d+)([mf])", category)
        senior_match = re.fullmatch(r"(?:se|sen)([mhfd])", category)
        acn_category_match = re.fullmatch(r"(jun|esp)([hd])|(ve|ai)([1-4])", category)
        legacy_age_match = re.fullmatch(r"([mfv])(\d+)", category)
        legacy_senior_match = re.fullmatch(r"se([hf])", category)
        if under_age_match:
            category_sex = under_age_match.group(2)
        elif master_match:
            category_sex = master_match.group(2)
        elif senior_match:
            category_sex = {"m": "m", "h": "m", "f": "f", "d": "f"}[
                senior_match.group(1)
            ]
        elif acn_category_match:
            youth_code, youth_sex, veteran_code, veteran_number = (
                acn_category_match.groups()
            )
            if youth_code is not None:
                category_sex = {"h": "m", "d": "f"}[youth_sex]
            else:
                category_sex = "m" if veteran_code == "ve" else "f"
        elif legacy_age_match:
            category_sex = {"m": "m", "f": "f", "v": "f"}[legacy_age_match.group(1)]
        elif legacy_senior_match:
            category_sex = {"h": "m", "f": "f"}[legacy_senior_match.group(1)]
        else:
            warnings.warn(
                f"Unrecognized race category: {category or '<empty>'}",
                RuntimeWarning,
                stacklevel=2,
            )
            return False
    member_sex = normalize_header(member.sex)
    member_sex = {"homme": "m", "h": "m", "male": "m", "monsieur": "m", "femme": "f", "female": "f", "dame": "f", "d": "f"}.get(member_sex, member_sex[:1])
    if member_sex != category_sex:
        return False
    return True


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
    return response.text


def write_csv(rows: Iterable[dict[str, str]], output: TextIO) -> None:
    fieldnames = ["position", "name", "time", "team", "race_name", "birth_year", "category"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract club members from an ACN Timing race result")
    parser.add_argument("race", nargs="?", help="Race-results URL")
    parser.add_argument("-o", "--output", type=Path, help="CSV output path; stdout when omitted")
    parser.add_argument("--members-csv", type=Path, help="Use a local members CSV instead of Google Sheets")
    parser.add_argument("--sync-sporteasy-members", action="store_true", help="Add missing SportEasy members to the Google Sheet")
    parser.add_argument("--sheet-id", default=SHEET_ID)
    parser.add_argument("--members-gid", default=MEMBERS_GID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.sync_sporteasy_members:
            sheet_members = load_members(fetch_text(sheet_csv_url(args.sheet_id, args.members_gid)))
            sporteasy_members = fetch_sporteasy_members()
            sheet_keys = {
                (normalize_name(member.last_name), normalize_name(member.first_name))
                for member in sheet_members
            }
            missing = [
                member
                for member in sporteasy_members
                if (normalize_name(member.last_name), normalize_name(member.first_name)) not in sheet_keys
            ]
            if not missing:
                print("No missing SportEasy members found.")

            missing.sort(
                key=lambda member: (
                    normalize_name(member.first_name),
                    normalize_name(member.last_name),
                )
            )
            print(f"Found {len(missing)} missing SportEasy members:")
            for index, member in enumerate(missing, start=1):
                print(f"{index}. {member.first_name} {member.last_name} ({member.birth_date})")

            member_sexes: dict[tuple[str, str], str] = {}
            for member in missing:
                key = (
                    normalize_name(member.last_name),
                    normalize_name(member.first_name),
                )
                sex = input(
                    f"Confirm sex for {member.first_name} {member.last_name} (H/D, or skip): "
                ).strip().upper()
                if sex == "SKIP":
                    continue
                if sex not in {"H", "D"}:
                    print("Skipped: sex must be H or D.")
                    continue
                member_sexes[key] = sex

            if not member_sexes:
                print("No members approved for insertion.")

            print(f"Inserting {len(member_sexes)} approved member(s) into Google Sheets...")
            inserted = sync_sporteasy_members_to_google_sheet(
                sporteasy_members,
                member_sexes,
                args.sheet_id,
            )
            print("Inserted:")
            for name in inserted:
                print(f"- {name}")
            return 0
        elif args.members_csv:
            members = load_members(args.members_csv.read_text(encoding="utf-8"))
        else:
            members = load_members(fetch_text(sheet_csv_url(args.sheet_id, args.members_gid)))
        race_url = args.race.casefold()
        if "ultratiming" in race_url:
            results, race_year = fetch_ultratiming_results(args.race)
        elif "acn-timing" in race_url:
            results, race_year = fetch_acn_results(args.race)
        else:
            raise ValueError("Race URL must contain 'acn-timing' or 'ultratiming'")
        rows = match_members(members, results, race_year=race_year)
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