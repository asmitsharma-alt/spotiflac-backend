# SpotiFLAC FastAPI Backend

A containerized Python backend service powered by **FastAPI** and **SpotiFLAC** that resolves Spotify tracks and downloads matching lossless audio (FLAC/MP3) from supported high-fidelity streaming services (Tidal, Qobuz, Deezer, etc.).

---

## 🚀 Features

- **Lossless FLAC Extraction**: Resolves Spotify metadata and fetches original lossless audio streams.
- **RESTful Endpoints**:
  - `GET /api/health`: Health status for Render zero-downtime checks and external monitors.
  - `GET /api/info?url=...`: Fast track metadata lookup (title, artists, album, ISRC, cover art) without downloading.
  - `GET /api/download?url=...`: Downloads and streams the audio file directly to the client as an attachment.
- **Non-Blocking Architecture**: Uses `asyncio.to_thread` for background downloading and tagging, keeping the server responsive.
- **Zero Disk Bloat**: Cleans up temporary download artifacts immediately after the client finishes downloading via FastAPI `BackgroundTasks`.
- **Render Ready**: Includes `Dockerfile` and `render.yaml` with automated keep-alive pinging to prevent sleeping on Render's free tier.

---

## 📡 API Endpoints

### 1. Health Check
```http
GET /api/health
```
**Response:**
```json
{
  "status": "ok",
  "service": "spotiflac-backend"
}
```

### 2. Track Metadata
```http
GET /api/info?url=https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT
```
**Response:**
```json
{
  "title": "Never Gonna Give You Up",
  "artists": ["Rick Astley"],
  "album": "Whenever You Need Somebody",
  "year": 1987,
  "duration_seconds": 213,
  "isrc": "GBARL9300134"
}
```

### 3. Download Audio
```http
GET /api/download?url=https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT&quality=LOSSLESS
```
- **Query Parameters**:
  - `url` (required): Spotify track URL.
  - `quality` (optional): `LOSSLESS` (default), `HIGH`, `MEDIUM`.
  - `embed_lyrics` (optional): `true` (default) or `false`.
- **Response**: Binary audio file stream (`audio/flac` or `audio/mpeg`) with `Content-Disposition` header.

---

## 🛠️ Local Development

### Prerequisites
- Python 3.11 or 3.12
- System `ffmpeg` installed and added to PATH

### Installation
```bash
# 1. Clone or navigate to the directory
cd spotiflac-backend

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start development server
uvicorn main:app --reload --port 8000
```

Open interactive Swagger API documentation in your browser:
👉 **`http://localhost:8000/docs`**

---

## 🐳 Docker Setup

```bash
# Build the Docker image
docker build -t spotiflac-api .

# Run the container
docker run -p 8000:8000 spotiflac-api
```

---

## 🌐 Deploy to Render

### Option 1: Render Blueprint (Recommended)
1. Push this folder to a GitHub repository.
2. In the [Render Dashboard](https://dashboard.render.com), click **New +** $\rightarrow$ **Blueprint**.
3. Connect your repository. Render will automatically read `render.yaml` and configure the Docker service.

### Option 2: Manual Web Service
1. Click **New +** $\rightarrow$ **Web Service**.
2. Connect your repository.
3. Select **Runtime: Docker** and choose the **Free** instance type.
4. Click **Deploy**.
