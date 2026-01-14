# CPU Stresser API

A Python HTTP API for running CPU stress tests using `stress-ng` with configurable parameters.

## Features

- **CPU Stress Endpoint**: Run stress-ng with configurable CPU count and duration
- **Health Check**: `/health` endpoint for upstream health checks
- **Process Management**: Start, stop, and list running stress tests

## Requirements

- Python 3.8+
- `stress-ng` installed on the system

### Install stress-ng

```bash
# Ubuntu/Debian
sudo apt-get update && sudo apt-get install -y stress-ng

# Amazon Linux
sudo yum install -y stress-ng
```

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Start the API server

```bash
python app.py
```

Or with uvicorn directly:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

### API Endpoints

#### Health Check
```bash
GET /health
```

Response:
```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T12:00:00"
}
```

#### Start CPU Stress
```bash
POST /stress
Content-Type: application/json

{
  "cpu": 2,
  "timeout": 60
}
```

Response:
```json
{
  "message": "CPU stress started with 2 workers for 60 seconds",
  "cpu": 2,
  "timeout": 60,
  "process_id": 12345
}
```

#### List Running Stresses
```bash
GET /stress
```

#### Stop a Stress Test
```bash
DELETE /stress/{process_id}
```

### API Documentation

Once the server is running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Example Usage

```bash
# Health check
curl http://localhost:8000/health

# Start stress test (2 CPUs for 60 seconds)
curl -X POST http://localhost:8000/stress \
  -H "Content-Type: application/json" \
  -d '{"cpu": 2, "timeout": 60}'

# List running stresses
curl http://localhost:8000/stress

# Stop a stress test
curl -X DELETE http://localhost:8000/stress/12345
```
