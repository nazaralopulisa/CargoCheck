# CargoCheck dashboard - container for AWS App Runner (or any container host).
#
# Build (on an Apple Silicon Mac, --platform is required for AWS):
#   docker build --platform linux/amd64 -t cargocheck .
# Run locally:
#   docker run -p 8080:8080 cargocheck        -> http://localhost:8080

FROM python:3.12-slim

WORKDIR /srv/cargocheck
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080

# Install dependencies first, so code changes don't reinstall everything.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code, theme, the inbox data, and the pipeline's saved results.
# (.dockerignore keeps out secrets, the virtualenv and the organizers' server/.)
COPY app/ app/
COPY .streamlit/ .streamlit/
COPY data/sdoc-hackathon-bundle/ data/sdoc-hackathon-bundle/
COPY output/ output/

EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/_stcore/health')"

CMD ["sh", "-c", "exec streamlit run app/dashboard.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true --browser.gatherUsageStats=false"]