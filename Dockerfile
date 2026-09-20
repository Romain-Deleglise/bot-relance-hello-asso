FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY relance_adhesions/ ./relance_adhesions/
COPY templates/ ./templates/

# Exécution sous un utilisateur non privilégié : aucune raison de tourner en
# root pour un simple batch. Les dossiers persistants sont créés et lui sont
# attribués pour qu'il puisse y écrire une fois montés en volume.
RUN useradd --create-home --uid 10001 relance \
    && mkdir -p /app/data /app/logs \
    && chown -R relance:relance /app
USER relance

# Les données persistantes (base anti-doublon, logs) sont montées en volume :
#   docker run -v /srv/relance/data:/app/data -v /srv/relance/logs:/app/logs ...
VOLUME ["/app/data", "/app/logs"]

# La configuration est injectée par --env-file ou par des variables
# d'environnement ; aucun secret n'est copié dans l'image.
ENTRYPOINT ["python", "-m", "relance_adhesions", "--env-file", ""]
