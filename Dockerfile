FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QUOTEIQ_DATA_DIR=/app/data
WORKDIR /app
COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt \
    && useradd --create-home --uid 1000 quoteiq \
    && mkdir /app/data && chown quoteiq:quoteiq /app/data
COPY --chown=quoteiq:quoteiq quoteiq ./quoteiq
COPY --chown=quoteiq:quoteiq .streamlit/config.toml ./.streamlit/config.toml
COPY --chown=quoteiq:quoteiq streamlit_app.py ./
USER quoteiq
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.getenv('PORT','8501') + '/_stcore/health')"
CMD ["sh", "-c", "exec streamlit run streamlit_app.py --server.address=0.0.0.0 --server.port=${PORT:-8501}"]
