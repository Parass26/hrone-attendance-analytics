# HROne Employee Attendance & Analytics API

## Overview

A REST API built with **Python, FastAPI, and MongoDB** for managing employee records, attendance punch-in/punch-out, attendance corrections, and attendance analytics. The API includes request validation, duplicate-operation handling, audit history for corrections, paginated attendance retrieval, department summaries, late-arrival rankings, daily trends, and MongoDB query-plan inspection.

## Technology

- Python 3.11
- FastAPI and Uvicorn
- MongoDB 6.0+ (MongoDB 7.0 is the assignment's grader version)
- PyMongo, Pydantic, and python-dotenv
- Swagger UI for interactive API testing

## Prerequisites

Install Python 3.11 and have access to either a MongoDB Atlas deployment or a local MongoDB server. VS Code is optional. Links: [Python](https://www.python.org/downloads/release/python-3119/), [MongoDB Atlas](https://www.mongodb.com/atlas), [MongoDB Community Server](https://www.mongodb.com/try/download/community), [VS Code](https://code.visualstudio.com/download).

## Setup on Windows

Open PowerShell in the repository root. If the virtual environment does not already exist, create it and install the dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create a `.env` file in the repository root (the same directory as `requirements.txt`). For MongoDB Atlas, use the connection URI provided by Atlas:

```dotenv
MONGO_URI="mongodb+srv://<username>:<password>@<cluster-address>/<database>?retryWrites=true&w=majority"
MONGO_DB="hrone_attendance"
```

For a local MongoDB server, the URI commonly looks like this:

```dotenv
MONGO_URI="mongodb://127.0.0.1:27017"
MONGO_DB="hrone_attendance"
```

Replace placeholders with your own values. Make sure the database is reachable and the Atlas network access and database-user permissions are configured. Never commit or share `.env` or real database credentials.

## Run the API

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Keep the terminal open. Open **http://127.0.0.1:8000/docs** to use Swagger UI. To run with automatic reload during development:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List/retrieve employees |
| `POST` | `/attendance/punch-in` | Record punch-in |
| `POST` | `/attendance/punch-out` | Record punch-out |
| `GET` | `/attendance` | Query attendance records with pagination/filters |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct attendance and record audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee analytics |
| `GET` | `/analytics/departments/summary` | Department summary |
| `GET` | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| `GET` | `/analytics/departments/{department}/trend` | Daily department trend |
| `GET` | `/admin/explain/{endpoint}` | Inspect query execution plans |

Use `/docs` for the exact parameters, request bodies, and response schemas for each operation.

## Validation and responses

The API validates inputs such as epoch-millisecond timestamps, month/date formats, attendance statuses, and pagination limits. Common HTTP responses include `200` for a successful request, `201` for creation, `404` when a resource is not found, `409` for a conflicting duplicate operation, and `422` for invalid input. The exact response depends on the endpoint and request.

## Troubleshooting

- **MongoDB connection errors:** Check `MONGO_URI`, `MONGO_DB`, the local MongoDB service or Atlas network access, and database-user permissions.
- **Missing Python packages:** Run the dependency installation command again using `.venv\Scripts\python.exe`.
- **Port 8000 is occupied:** Stop the process using it or run Uvicorn with another port, for example `--port 8001`, then open `http://127.0.0.1:8001/docs`.
- **Request returns `422`:** Compare the request with the schema shown for that endpoint in Swagger UI.

## Submission and security

Submit the required source and documentation files specified by the assignment. Do not commit `.env`, database credentials, `.venv/`, `__pycache__/`, or compiled `.pyc` files. Keep local backup files out of the submission unless the assignment specifically requests them.
