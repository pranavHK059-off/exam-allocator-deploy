# Deploy this project as a working website

**Important:** GitHub Pages only hosts static files. It cannot run this project's Flask/Python allocation engine. To make the uploaded CSVs and allocation work, keep the code in GitHub and deploy the Flask app as a web service (for example, on Render).

## 1. Upload the project to GitHub

1. Extract this ZIP.
2. Upload the *contents* of this folder to the root of your GitHub repository (not the ZIP itself, and not a nested folder).
3. Make sure `app.py`, `allocation_engine.py`, `index.html`, `requirements.txt`, and `Procfile` are at the repository root.
4. Do not upload `.venv`, `__pycache__`, or a local SQLite database.

## 2. Deploy the repository as a web service

1. Sign in to Render and choose **New → Web Service**.
2. Connect the GitHub repository you just uploaded.
3. Use these settings:
   - **Runtime:** Python
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Deploy and wait for the service to become live.
5. Open the Render URL it gives you. The website and `/api/...` endpoints are served by the same Flask app, so the frontend's relative API requests work without a separate CORS setup.

## 3. Verify it

Open `https://YOUR-SERVICE.onrender.com/api/health` (replace with your actual service URL). It should return JSON with `"status":"ok"`. Then open the service's root URL and test uploading the four CSV tables and generating an allocation.

## Notes

- Do not use the GitHub Pages URL for this Flask version. Use the deployed web-service URL.
- A free host may sleep when idle and its local SQLite storage may not persist across restarts/redeploys. Re-upload test CSVs when needed; for durable production data, configure a persistent disk/database.
- This prototype does not include user authentication. Do not upload real student personal data to a public deployment unless you have approval and appropriate access controls.
