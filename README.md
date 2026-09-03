# WebPull

Universal web scraper with a clean browser UI.

Detects the site type automatically, handles authentication,
extracts structured data, and exports to JSON, CSV, and TXT.

> Built with AI assistance. All code reviewed and tested by the author.

---

## Features

- **Auto site detection** — plain HTML, NextAuth, WordPress, Django, Laravel, Shopify, Cloudflare, JS-rendered, REST API backed
- **Authentication support** — NextAuth Credentials, WordPress, HTML form login, public pages
- **Flexible extraction** — title, text, links, images, headings, tables, CSS selectors, structured field extraction
- **Pagination** — scrape multiple pages automatically
- **AI format assist** — Groq AI identifies your data type and suggests clean column names
- **Three output formats** — JSON, CSV, TXT (all generated every time)
- **Live progress feed** — watch the scrape happen in real time
- **Job history** — re-download past results
- **Clean wizard UI** — one step at a time, no overwhelm

---

## Stack

| Layer     | Technology                        |
|-----------|-----------------------------------|
| Backend   | Python · FastAPI · Uvicorn        |
| Scraping  | Requests · BeautifulSoup4         |
| AI assist | Groq API (Llama 3.1 8B)           |
| Frontend  | Vanilla HTML · CSS · JavaScript   |
| Process   | Supervisor                        |
| Server    | Nginx (reverse proxy)             |

---

## Quick Start (local)

**1. Clone the repo**
```bash
git clone https://github.com/yourusername/webpull.git
cd webpull
```

**2. Create a virtual environment**
```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```

**4. Add your Groq API key**
```bash
cp .env.example .env
# Edit .env and add your GROQ_API_KEY
```

**5. Run**
```bash
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` in your browser.

---

## Environment Variables

Create a `.env` file in the project root:

```
GROQ_API_KEY=gsk_your_key_here
GROQ_MODEL=llama-3.1-8b-instant
```

Get a free Groq API key at [console.groq.com](https://console.groq.com/keys).
No credit card required.

---

## Deploying to Oracle Cloud (free tier)

Oracle Cloud offers an Always Free VM (Ampere ARM, 4 OCPU, 24GB RAM)
which is more than enough to run WebPull.

**1. Provision a VM**
- Sign up at cloud.oracle.com
- Create a Compute instance (VM.Standard.A1.Flex — Always Free)
- Ubuntu 22.04
- Open ports 80 and 443 in the security list

**2. SSH in and set up the server**
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-venv nginx supervisor -y
```

**3. Clone and install WebPull**
```bash
cd /home/ubuntu
git clone https://github.com/yourusername/webpull.git
cd webpull
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**4. Add your .env file**
```bash
nano .env
# Add GROQ_API_KEY=your_key_here
```

**5. Set up Supervisor**
```bash
sudo mkdir -p /var/log/webpull
sudo cp supervisor.conf /etc/supervisor/conf.d/webpull.conf
# Edit the file and update paths if needed
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start webpull
```

**6. Set up Nginx**
```bash
sudo nano /etc/nginx/sites-available/webpull
```

Paste this config:
```nginx
server {
    listen 80;
    server_name your-server-ip-or-domain;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/webpull /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

WebPull is now live at your server's IP address.

**7. (Optional) Add SSL with Let's Encrypt**
```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d yourdomain.com
```

---

## Project Structure

```
webpull/
├── main.py           ← FastAPI app — routes, format assist
├── scraper.py        ← Scraper engine — detection, auth, extraction
├── jobs.py           ← Background job manager
├── static/
│   ├── index.html    ← Main UI (wizard)
│   ├── history.html  ← Job history page
│   ├── style.css     ← All styles
│   └── app.js        ← Frontend logic
├── results/          ← Scraped output files (auto-created)
├── requirements.txt
├── supervisor.conf   ← Production process config
├── .env.example      ← Environment variable template
└── README.md
```

---

## Supported Site Types

| Type              | Detection method                          | Auth support          |
|-------------------|-------------------------------------------|-----------------------|
| Plain HTML        | Content density check                     | Public / HTML form    |
| NextAuth (Next.js)| `/api/auth/providers` endpoint            | NextAuth Credentials  |
| WordPress         | `/wp-login.php` + content signatures      | WordPress login       |
| Django            | `csrftoken` + `sessionid` cookies         | HTML form             |
| Laravel           | `XSRF-TOKEN` cookie / `X-Powered-By`     | HTML form             |
| Shopify           | `myshopify.com` / `Shopify.theme`         | Warning shown         |
| JS-rendered       | Low word count + high script count        | Warning shown         |
| REST API backed   | API call patterns in scripts              | Advise direct endpoint|
| Cloudflare        | `CF-RAY` / `CF-Cache-Status` headers      | Warning shown         |

---

## Responsible Use

WebPull is a tool. Use it responsibly.

- Only scrape websites and data you are authorized to access
- Respect each site's `robots.txt` (WebPull checks this automatically)
- Use appropriate request delays — don't hammer servers
- Comply with the terms of service of any site you scrape
- Comply with applicable laws in your jurisdiction (GDPR etc.)

WebPull does not bypass CAPTCHAs, Cloudflare bot protection,
or any other security measure. It only handles standard authentication
flows that a regular user would use.

---

## Roadmap

- [ ] OAuth login support (Google, GitHub) via Playwright
- [ ] JWT / Bearer token auth mode
- [ ] Scheduled / recurring scrapes
- [ ] Webhook notifications when jobs complete
- [ ] Export to Google Sheets directly
- [ ] Profile templates for common site types

---

## License

MIT — free to use, modify, and distribute.
