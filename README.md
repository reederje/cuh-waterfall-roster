# cuh-waterfall-roster

Flask application that displays the CUH ED physician waterfall roster from ShiftAdmin.

## Requirements

- Python 3.11+
- ShiftAdmin credentials

## 1. Create and activate a virtual environment

From the project root:

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows (cmd):

```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

## 3. Configure environment variables

Create a `.env` file in the project root with the following values:

```env
SHIFTADMIN_USER=your_shiftadmin_username
SHIFTADMIN_PASSWORD=your_shiftadmin_password
AUTH_PASSWORD=choose_a_password_for_roster_access
STATE_DB_PATH=state/supertrack_state.db
FLASK_DEBUG=0
```

Notes:

- `SHIFTADMIN_USER` and `SHIFTADMIN_PASSWORD` are used for calls to ShiftAdmin.
- `AUTH_PASSWORD` is required for HTTP Basic Auth on the roster endpoints.
- `STATE_DB_PATH` is the on-disk SQLite file used for shared assignment counts and the Supertrack next-up indicator.
- The Basic Auth username is fixed in code as `cuhed`.
- Set `FLASK_DEBUG=1` for local debug mode.

## 4. Run the application

```bash
python app.py
```

The app starts on the default Flask URL:

- http://127.0.0.1:5000/

When prompted for credentials in the browser:

- Username: `cuhed`
- Password: value of `AUTH_PASSWORD` from your `.env`

## 5. Run tests

```bash
python -m pytest -q
```

Run the deterministic Supertrack allocation simulation and show the per-shift
patient totals:

```bash
python -m pytest tests/test_app.py::test_supertrack_day_simulation -q -s
```

The simulation covers 6:00 AM through 5:59 AM using the configured daily
arrival pattern and Supertrack shift schedule. It also reports arrivals that
have no eligible physician during the six-hour Supertrack assignment window.

## API endpoint

- `GET /api/roster` returns the roster JSON payload. Each active physician includes:
	- `shift_key`: identifier for that scheduled shift.
	- `patients_assigned`: shared assignment total for the shift.
	- `patients_per_hour`: assignment total divided by elapsed shift time, rounded to one decimal. The first hour uses a one-hour minimum denominator to avoid inflated rates for newly started shifts.

## Assignment tracking

- Click an active physician card to record a patient assignment. Counts and the Supertrack next-up indicator are shared across connected users.
- Double-click a physician's patient total to correct it. Press Enter or click elsewhere to commit the value; press Escape to cancel.
- Supertrack physicians are eligible for their first six hours. The next-up physician has the lowest weighted patient rate: the first two active hours use a $1.0$ multiplier, hours two through four use $1.5$, and hours four through six use $2.0$. Equal scores use the existing roster order as a stable tie-breaker.