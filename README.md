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

## Cloud deployment

When deploying to Azure App Service or another managed cloud service, the
application must be started with a production server that supports
Flask-SocketIO. The local `python app.py` command is not sufficient for these
hosts, and a plain WSGI command such as `gunicorn app:app` will not handle the
Socket.IO connection correctly.

For a Linux Azure App Service, set the application **Startup Command** to:

```bash
gunicorn --worker-class eventlet --workers 1 --bind=0.0.0.0:$PORT app:app
```

The `gunicorn` and `eventlet` packages are included in `requirements.txt`. The
`$PORT` value is supplied by the cloud platform and must be used instead of a
hard-coded local port. Use one worker unless a shared Socket.IO message queue
has also been configured.

Other cloud providers may use a different configuration field or startup
format, but the requirements are the same: use a Socket.IO-compatible
production server, bind to the provider's supplied port, and enable the
provider's WebSocket support when that setting is available. Consult the
provider-specific deployment documentation for the exact command field.

For Azure App Service, store the SQLite state file in a writable persistent
location rather than the deployed application directory, for example:

```text
STATE_DB_PATH=/home/data/supertrack_state.db
```

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
- The payload also includes a `next_up` field describing the current rotation recommendation: `{"type": "physician", "area_name": ..., "phys_key": ...}` when a Supertrack physician is next, or `{"type": "area", "area_name": "Gray"|"Purple"}` when a whole pod is next.

## Assignment tracking

- Click an active physician card to record a patient assignment. Counts and the Supertrack next-up indicator are shared across connected users.
- Double-click a physician's patient total to correct it. Press Enter or click elsewhere to commit the value; press Escape to cancel.
- Supertrack physicians are eligible for their first six hours. The **Next-up method** control switches between **Phase weighting**, which uses the configured phase multipliers, and **Strict round robin**, which advances sequentially through eligible physicians after each assignment. The selected method is shared across connected users and persisted in SQLite.

## Gray & Purple pod rotation

- Gray and Purple pods share the same overall "next up" rotation as Supertrack, but they participate as a single unit rather than per physician: each current Supertrack physician counts as one rotation entry, and each non-empty Gray or Purple pod counts as exactly one entry regardless of how many physicians are working in it.
- For example, 3 Supertrack physicians plus a 1-physician Gray pod and a 2-physician Purple pod produce 5 total rotation entries, so Gray and Purple pods each come up once every 5 new assignments.
- When it is a pod's turn, the whole area card is highlighted with a **NEXT UP** badge instead of marking an individual physician.
- Gray and Purple pods do not display the "Patients assigned" or "Patients/hour" stats in the UI; those counts are still tracked internally for correction/history purposes, they are just not shown for these two pods.

## Skip next up

- Click **⏭ Skip next up** to pass on the current recommendation without recording a patient assignment.
- The rotation immediately excludes whoever was marked next up (a physician or an entire pod) and re-selects from the remaining eligible physicians/pods.
- Skipped entities are not penalized: their patient counts are unchanged, and they become eligible again as soon as any new assignment is recorded.