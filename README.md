# CopyGuard — YouTube Copyright Detector

CopyGuard is a web-based tool built for TV channels and content creators to **automatically find stolen or reuploaded copies of their videos on YouTube**. Instead of manually searching YouTube every day, CopyGuard does it for you — scanning, filtering, and presenting only the suspicious videos that need your attention.

---

## What Problem Does It Solve?

When a TV channel uploads a drama episode or music video to YouTube, other channels often reupload that same content without permission. Finding these stolen copies manually is time-consuming — you have to search different keyword variations, scroll through hundreds of results, and check each video one by one.

CopyGuard automates the entire process:
1. You enter your show name and official channel handles
2. CopyGuard searches YouTube using multiple keyword variations simultaneously
3. It automatically filters out your own official channel's videos
4. It presents only the suspicious third-party videos for your team to review
5. Your team marks each video as **Stolen** or **Not Stolen** with one click
6. Confirmed stolen videos are saved and can be exported to Excel for documentation

---

## Key Features

### Find Stolen Content
- Search YouTube by show name (teledrama, music event, live event, or other)
- Add **multiple keywords** — Sinhala names, transliterations, alternate spellings — all searched at the same time for wider coverage
- Add your official YouTube channel handles so CopyGuard automatically excludes your own content from results
- Results show only third-party channels that uploaded your content

### Review Workflow
- Default view shows only **unreviewed suspicious videos** — nothing already decided
- Each video card shows: thumbnail, title, channel name, upload date, view count, and a direct YouTube link
- Click **STOLEN** (red) or **NOT STOLEN** (green) — the card animates away immediately
- Counter updates live showing how many are pending, confirmed stolen, and cleared
- Official channel videos are completely hidden from the review queue

### Report a Stolen URL
- If you find a stolen video yourself, paste its YouTube URL directly
- CopyGuard fetches the video details automatically and saves it as confirmed stolen
- It also scans that uploader's full channel for more stolen content

### Export to Excel
- Export all confirmed stolen videos as a CSV file compatible with Microsoft Excel
- Includes: show name, video title, channel name, channel URL, video URL, upload date
- Supports Sinhala text (UTF-8 with BOM for Excel compatibility)

### Scan Video / Audio Files
- Upload your own video or audio file to check if it matches known content
- Useful for verifying suspicious files before reporting

### My Channel Monitoring
- Add your official YouTube channel URLs to track your own upload statistics
- Keep a record of your monitored channels in one place

---

## Deployment (Docker)

### Prerequisites
- Docker and Docker Compose installed
- A `backend/.env` file with your credentials (copy from `backend/.env.example`)

### Start the app

```bash
docker compose up -d
```

The app will be available at `http://localhost` (port 80 via Nginx).

### Make it publicly accessible (no domain needed)

```bash
docker run -d --name cg-tunnel --network host cloudflare/cloudflared:latest tunnel --url http://localhost:80
docker logs cg-tunnel 2>&1 | grep trycloudflare
```

This gives you a free public URL like `https://random-words.trycloudflare.com`.

### Environment Variables (`backend/.env`)

```
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your_secure_password
JWT_SECRET=change_this_to_a_random_secret
JWT_EXPIRE_HOURS=24
POSTGRES_PASSWORD=your_secure_db_password
UPLOAD_DIR=uploads
```

---

## Technology

- **Backend** — Python / FastAPI
- **Database** — PostgreSQL (persisted via Docker volume)
- **YouTube scanning** — YouTube RSS feeds + oEmbed API (no API key required)
- **Authentication** — JWT tokens with role-based access control
- **Frontend** — Vanilla HTML/CSS/JS (no framework dependencies)
- **Infrastructure** — Docker + Nginx reverse proxy + optional Cloudflare Tunnel

---

*Developed by Nipun Dinudaya*
