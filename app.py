from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO

import requests
import streamlit as st
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from club_results import (
    fetch_acn_results,
    fetch_text,
    fetch_ultratiming_results,
    load_members,
    match_members,
    sheet_csv_url,
)


def rows_to_excel(rows: list[dict[str, str]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Matched members"

    fieldnames = [
        "position",
        "name",
        "time",
        "team",
        "race_name",
        "birth_year",
        "category",
    ]
    worksheet.append(fieldnames)
    for row in rows:
        worksheet.append([row.get(fieldname, "") for fieldname in fieldnames])

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for column_index, fieldname in enumerate(fieldnames, start=1):
        values = [str(worksheet.cell(row, column_index).value or "") for row in range(1, worksheet.max_row + 1)]
        worksheet.column_dimensions[get_column_letter(column_index)].width = min(max(map(len, values)) + 2, 40)

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def load_results(url: str) -> tuple[list[dict[str, str]], int | None, str]:
    members_text = fetch_text(sheet_csv_url())
    normalized_url = url.casefold()
    if "ultratiming" in normalized_url:
        results, race_year = fetch_ultratiming_results(url)
    elif "acn-timing" in normalized_url:
        results, race_year = fetch_acn_results(url)
    else:
        raise ValueError("L'URL doit contenir 'acn-timing' ou 'ultratiming'.")
    warnings = StringIO()
    with redirect_stdout(warnings):
        rows = match_members(load_members(members_text), results, race_year)
    return rows, race_year, warnings.getvalue()


st.set_page_config(page_title="Résultats des courses du RCB", page_icon="🏃")
st.title("Résultats des membres du RCB")
with st.expander("Cliquer ici pour plus d'information"):
    st.write("Coller l'URL de la page de résultat d'une course sur les sites ACN Timing ou Ultratiming pour récupérer les résultats des membres du RCB.")
    st.write("exemples : ")
    st.write(" - https://www.acn-timing.com/?lng=NL#/events/2156361345579978/ctx/20261011_brugge/generic/199034_2/home/LIVE2")
    st.write(" - https://www.ultratiming.live/en/evenement/trail-de-la-cochonne-2026/epreuve/13km/resultats")
    st.write("L'app charge la liste des membres du RCB dans l'onglet \"Membres\" du fichier google sheet \"Challenge RCB 2026\", puis compare les noms des participants à la course avec les noms des membres du RCB, et récupère la position, le nom et le temps des membres du RCB.")
    st.write("Le nom de l'équipe (colonne team), si elle est disponible dans les résultats, est le nom indiqué dans les résultats de la course et permet de valider que le coureur a bien indiqué RCB comme équipe lors de son inscription.")
    st.write("Le nom du participant à la course (race_name), l'année de naissance et la catégorie servent à vérifier que le participant à la course est bien le membre du RCB.")

url = st.text_input(
    "URL des résultats",
    placeholder="https://www...",
)

if st.button("Chercher les membres RCB", type="primary"):
    if not url.strip():
        st.error("Entrer une URL de résultats.")
    else:
        try:
            with st.spinner("Chargement des résultats..."):
                rows, race_year, warnings = load_results(url.strip())
            st.session_state["matched_rows"] = rows
            st.session_state["race_year"] = race_year
            st.session_state["warnings"] = warnings
        except (ValueError, OSError, requests.RequestException) as error:
            st.error(str(error))

rows = st.session_state.get("matched_rows")
if rows is not None:
    warnings = st.session_state.get("warnings", "").strip()
    if warnings:
        st.warning(warnings)
    st.subheader(f"Trouvé {len(rows)} membres du RCB")
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.download_button(
        "Télécharger le fichier excel",
        data=rows_to_excel(rows),
        file_name="participants.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
