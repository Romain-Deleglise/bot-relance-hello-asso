"""Alerte et supervision, par des canaux INDÉPENDANTS du service de mail.

Le point crucial : si l'envoi de mails (SES/SMTP) tombe en panne, on ne peut pas
s'en servir pour prévenir. Ce module s'appuie donc sur deux canaux distincts :

* un **webhook** (Discord ou Slack) qui reçoit un message en cas d'échec ;
* un **dead-man's switch** (healthcheck.io ou équivalent) : le bot « ping » une
  URL à chaque exécution réussie ; si le ping quotidien attendu n'arrive pas, le
  service de supervision alerte — ce qui couvre aussi « la cron n'a pas tourné »
  ou « le serveur est éteint », qu'aucune alerte interne ne pourrait signaler.

Toutes les fonctions sont *best effort* : une alerte qui échoue est journalisée
mais ne fait jamais échouer l'exécution (on ne veut pas qu'un webhook cassé
empêche les relances de partir).
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


def ping_healthcheck(url: str, success: bool = True, timeout: int = 10) -> None:
    """Signale l'état de l'exécution au service de supervision (dead-man switch).

    Convention type healthchecks.io : `<url>` pour un succès, `<url>/fail` pour
    un échec. Sans URL configurée, ne fait rien.
    """
    if not url:
        return
    target = url if success else url.rstrip("/") + "/fail"
    try:
        requests.get(target, timeout=timeout)
        logger.debug("Ping healthcheck envoyé (%s)", "succès" if success else "échec")
    except requests.RequestException as exc:
        logger.warning("Ping healthcheck impossible (%s) : %s", target, exc)


def send_alert(webhook_url: str, message: str, timeout: int = 10) -> None:
    """Pousse un message d'alerte sur un webhook Discord ou Slack.

    Le corps porte à la fois `content` (clé Discord) et `text` (clé Slack) :
    chaque plateforme lit la sienne et ignore l'autre, donc le même appel
    fonctionne pour les deux sans configuration de type. Sans URL, ne fait rien.
    """
    if not webhook_url:
        return
    try:
        requests.post(
            webhook_url, json={"content": message, "text": message}, timeout=timeout
        )
        logger.info("Alerte envoyée sur le webhook de supervision")
    except requests.RequestException as exc:
        logger.warning("Alerte webhook impossible : %s", exc)
