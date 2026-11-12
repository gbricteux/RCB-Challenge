# Running club race results

This first version downloads the members from the `Membres` tab of the configured Google Sheet, reads results from ACN Timing or Ultratiming, matches participants, and writes CSV.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Usage

The Google Sheet must be accessible for export to fetch the list of members of the club. Pass an ACN Timing or Ultratiming results URL. For ACN Timing, the script extracts `ctx` and `raceId` and retrieves the complete result set from the Chronorace API. For Ultratiming, it follows the paginated results API and retrieves all pages. 

Examples of terminal commands are shown below that extract race results and return the results of the members in the output file *participants.csv* :

```sh
python club_results.py "https://www.acn-timing.com/?lng=FR#/events/2156365640494664/ctx/20260912_nadrin/generic/197994_4/home/LIVE4" --output participants.csv
```

```sh
python club_results.py "https://www.ultratiming.live/en/evenement/run-for-justine-2026/epreuve/5km/resultats" --output participants.csv
```

For development or offline use, a saved rendered HTML file is still supported:

```sh
python club_results.py race.html --members-csv membres.csv --output participants.csv
```

For ACN Timing results, the time column is detected from `#TIME`, `#NETTO`, or
`#Officiel`. If none is present, the command-line script asks you to choose the
time column from the columns returned by the API.

The output columns are `position`, `name`, `time`, `team`, `race_name`, `birth_year` and `category`.

If a runner is subscribed with team name "RCB" but is not in list of members, a warning is sent in the terminal.

## Tests
To run the tests, type in terminal :

```sh
python -m unittest -v
```

## Streamlit interface

Install the dependencies and start the web interface with:

```sh
streamlit run app.py
```

Paste an ACN Timing or Ultratiming results URL into the form. The matched club members are displayed in a table and can be downloaded as `participants.xlsx`.