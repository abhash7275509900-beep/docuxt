# Deploy on Render (Docker)
No environment variables needed - the secret key is built into app/auth.py. Keep your GitHub repo PRIVATE.
Advanced: Health Check Path = /health ; Disk: mount /srv/storage (5 GB+)
Instance: Standard (2 GB RAM) recommended for OCR / Office conversion.
Never commit your secret key to GitHub.
