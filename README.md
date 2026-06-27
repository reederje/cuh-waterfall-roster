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
FLASK_DEBUG=0
```

Notes:

- `SHIFTADMIN_USER` and `SHIFTADMIN_PASSWORD` are used for calls to ShiftAdmin.
- `AUTH_PASSWORD` is required for HTTP Basic Auth on the roster endpoints.
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

## API endpoint

- `GET /api/roster` returns the roster JSON payload.