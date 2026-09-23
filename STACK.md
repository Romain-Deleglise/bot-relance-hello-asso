# Stack technique — Bot de relance des adhésions HelloAsso

Document de référence sur les choix techniques du projet : quoi, pourquoi, et
quelles alternatives ont été écartées. Complémentaire de `SUIVI.md` (état
d'avancement et tâches restantes) et de `README.md` (installation et usage).

**Dépôt :** https://github.com/Romain-Deleglise/bot-relance-hello-asso

---

## 1. Vue d'ensemble

Le bot est un **batch quotidien**, pas un service. Il démarre, fait son travail
en quelques secondes, et s'arrête. Cette nature commande tous les autres choix :
pas de serveur à maintenir, pas de port ouvert, pas de processus à surveiller,
et un redémarrage du serveur sans conséquence.

```
  cron (1×/jour)
        │
        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  python -m relance_adhesions                            │
  │                                                         │
  │  config.py      lit .env / variables d'environnement    │
  │       │                                                 │
  │  helloasso.py   OAuth2 → GET /items (paginé)  ──────────┼──► API HelloAsso
  │       │                          GET /forms/…/public    │    (HTTPS)
  │       ▼                                                 │
  │  membership.py  calcule les échéances, choisit l'étape  │
  │       │                                                 │
  │       ▼                                                 │
  │  suppression.py écarte les désinscrits  ◄───────────────┼──  data/desinscrits.txt
  │       │                                                 │
  │  state.py       écarte ce qui est déjà envoyé  ◄────────┼──  data/relances.sqlite3
  │       │                                                 │
  │       ▼                                                 │
  │  mailer.py      rend les templates, envoie  ────────────┼──► SMTP
  │       │                                                 │
  │       ▼                                                 │
  │  state.py       enregistre les envois réussis  ─────────┼──► data/relances.sqlite3
  │       │                                                 │
  │  notify.py      alerte / ping de supervision  ──────────┼──► webhook + healthcheck
  └─────────────────────────────────────────────────────────┘
        │
        ▼
  logs/relance.log
```

---

## 2. Le langage et les dépendances

### Python 3.12

Choisi pour la demande initiale, et adapté : la tâche est de l'orchestration
d'API et de texte, sans contrainte de performance. Le code utilise les
annotations de type modernes (`str | None`) et `zoneinfo`, disponibles depuis
Python 3.9–3.10.

### Deux dépendances, et c'est tout

```
requests>=2.28,<3
tzdata>=2024.1
```

**`requests`** pour les appels HTTP. Le SDK officiel
[`helloasso-python`](https://github.com/HelloAsso/helloasso-python) a été écarté
sciemment : c'est un client généré depuis l'OpenAPI, volumineux, qui aurait
apporté des dizaines de dépendances transitives pour six appels HTTP. Il a en
revanche servi de **source de vérité documentaire** — c'est en lisant ses
modèles générés qu'on a établi la structure exacte des données d'adhésion.

**`tzdata`** pour la base de fuseaux IANA. Elle n'est pas un luxe : les images
Docker « slim » ne l'embarquent pas, et `zoneinfo` échoue alors silencieusement
au changement d'heure.

Tout le reste vient de la **bibliothèque standard** : `sqlite3`, `smtplib`,
`email`, `logging`, `argparse`, `string.Template`, `zoneinfo`, `csv`.

**Pourquoi cette frugalité.** Le bot tourne sans surveillance sur le serveur
d'une association. Chaque dépendance est une mise à jour à suivre, une faille
potentielle, une panne possible. Moins il y en a, plus l'outil survivra sans
maintenance — et c'est précisément ce qu'on attend de lui.

---

## 3. L'API HelloAsso

### Authentification — OAuth2 `client_credentials`

```
POST https://api.helloasso.com/oauth2/token
```

**Piège vérifié :** l'endpoint de token est **à la racine du domaine, pas sous
`/v5`**, contrairement à tous les autres appels. Le token expire au bout de
1800 secondes ; le client le renouvelle automatiquement, avec une marge de 60
secondes, et se réauthentifie sur un 401 en cours de route.

Le client API doit avoir le rôle `OrganizationAdmin` et le privilège
`AccessTransactions`.

### Récupération des adhésions

```
GET /v5/organizations/{slug}/items
    ?tierTypes=Membership
    &itemStates=Processed&itemStates=Registered
    &withDetails=true
    &pageSize=100          ← plafonné à 100, au-delà : HTTP 400
```

### Le point structurant : aucune date de fin d'adhésion

L'API **n'expose pas d'échéance sur l'item**. Elle donne `order.date`, `payer`,
`user`, `name`, `state`, `customFields` — rien d'autre. L'échéance est
recalculée depuis la règle portée par le **formulaire** :

```
GET /v5/organizations/{slug}/forms/Membership/{formSlug}/public
→ validityType ∈ { MovingYear, Custom, Illimited }
```

| `validityType` | Calcul appliqué |
|---|---|
| `MovingYear` | `order.date` + 365 jours |
| `Custom` | `endDate` du formulaire |
| `Illimited` | jamais relancé |

Le bot lit ce champ automatiquement, une fois par formulaire, avec mise en
cache. Pour Pause IA : `MovingYear`, confirmé côté formulaire.

### La pagination, ou comment mesurer plutôt que déduire

Les métadonnées de pagination de cet endpoint sont **inutilisables** :

```json
"pagination": { "totalCount": -1, "totalPages": -1,
                "continuationToken": "202605111952183434908_183989374" }
```

`-1` signifie « inconnu », pas « zéro ». Et surtout, mesuré sur l'API réelle :

```
pageIndex=2 AVEC continuationToken  →  0 élément    (page vide, sans erreur)
pageIndex=2 SANS token              →  la suite correcte
```

Le token signifie « reprends **après** cet enregistrement » ; l'envoyer avec
`pageIndex=2` demande de sauter une page entière au-delà, d'où la page vide.

**Règle retenue :** paginer par `pageIndex` seul, ne jamais transmettre le
token, s'arrêter uniquement sur une page vide ou incomplète.

Cette règle a coûté quatre corrections successives fondées sur des hypothèses,
chacune plausible, toutes fausses. La bonne est venue en **mesurant** —
l'outil `outils/diagnostic_pagination.py` est né de cet épisode et permet de
rejouer le diagnostic à tout moment. Sa sortie ne contient aucune donnée
personnelle, elle est donc partageable.

**Leçon transposable :** face à une API dont le comportement surprend,
instrumenter avant de corriger. Et faire en sorte qu'une anomalie de comptage
produise un **avertissement visible** plutôt qu'un arrêt silencieux — c'est la
troncature muette qui a rendu ces quatre bugs si coûteux.

---

## 4. La persistance — SQLite

`data/relances.sqlite3`, deux tables :

```sql
relances(dedup_key PRIMARY KEY, item_id, email, end_date, sent_at)
executions(id, started_at, finished_at, analysed, selected, sent, errors, dry_run)
```

**Pourquoi SQLite** plutôt qu'un fichier JSON ou PostgreSQL. Un JSON aurait
suffi fonctionnellement, mais SQLite apporte l'atomicité (une interruption en
cours d'écriture ne corrompt rien) et une clé primaire qui garantit
structurellement l'absence de doublon. PostgreSQL serait un serveur de plus à
administrer pour quelques milliers de lignes. SQLite est dans la bibliothèque
standard : zéro dépendance, zéro administration.

### La clé d'anti-doublon

```
dedup_key = "{item_id}:{date_de_fin}:{étape}"
```

Chaque composant a une raison d'être :

* **`item_id`** identifie l'adhésion ;
* **`date_de_fin`** garantit qu'après un renouvellement, la personne sera bien
  relancée l'année suivante — sa nouvelle échéance produit une nouvelle clé ;
* **`étape`** permet au mail du jour d'expiration de partir malgré le préavis
  déjà envoyé.

Une relance n'est inscrite qu'**après** un envoi réussi. En cas de panne SMTP
au milieu de la liste, les personnes déjà servies ne seront pas re-sollicitées
le lendemain, et les autres le seront.

La table `executions` sert de journal : elle permet de vérifier a posteriori
que le cron tourne et ce qu'il a fait.

---

## 5. L'envoi de mails — SMTP

### Pourquoi SMTP plutôt qu'une API transactionnelle

SMTP est le dénominateur commun : il couvre le serveur d'une association comme
tous les relais transactionnels (Brevo, Mailjet, Sendgrid, Amazon SES en
exposent tous un). Changer de prestataire ne demande que de modifier les
variables `SMTP_*` — aucune ligne de code.

Pour passer à une API HTTP malgré tout, le point d'extension est explicite :
écrire dans `mailer.py` une classe exposant `send(reminder, rendered_mail)` et
la substituer à `SmtpMailer` dans `cli.py`.

### Ce qui a été durci pour la délivrabilité

Envoyer 30 mails d'un coup depuis une IP d'association est le meilleur moyen
de finir en spam. D'où :

| Réglage | Rôle |
|---|---|
| `SMTP_DELAY_SECONDS` | pause entre deux envois, pour ne pas ressembler à un spammeur |
| `SMTP_MAX_PER_CONNECTION` | reconnexion périodique, exigée par certains relais |
| `SMTP_RETRY_ATTEMPTS` / `_DELAY` | réessai sur rejet **temporaire** (4xx : greylisting, throttling) — un rejet définitif (5xx) n'est jamais réessayé |
| `MAIL_REDIRECT_TO` | mode test : tous les mails partent vers une seule adresse |
| `MAIL_BCC` | copie cachée d'archivage |

Le message est construit en **multipart** : une version texte et une version
HTML alternative. Le texte n'est pas un repli de politesse — certains clients
et la plupart des filtres anti-spam le lisent.

### Les templates

Quatre fichiers, hors du code, éditables sans savoir programmer :

```
templates/relance.txt · relance.html                  → étape « préavis »
templates/relance-expiration.txt · .html              → étape « jour d'expiration »
```

Substitution par `string.Template` (syntaxe `$variable`) plutôt que Jinja2 :
pas de dépendance, et surtout pas de logique exécutable dans un fichier que
modifiera une personne non technique. `safe_substitute` garantit qu'un `$`
oublié ne fait pas échouer l'exécution.

Variables : `$nom`, `$prenom`, `$nom_complet`, `$email`, `$date_fin`,
`$date_adhesion`, `$formule`, `$association`, `$lien_adhesion`.

### La désinscription

`data/desinscrits.txt` — une adresse par ligne, les lignes vides et les `#`
ignorés. Un fichier texte, pas une table : il s'édite à la main, se lit d'un
coup d'œil, se sauvegarde par copie. La commande `--unsubscribe adresse@ex.fr`
fait la même chose sans ouvrir d'éditeur.

---

## 6. Les garde-fous

Ce sont les choix les plus importants du projet. Un bug dans un outil qui
envoie des mails à des adhérents ne se rattrape pas : le mail est parti.

| Garde-fou | Ce qu'il empêche |
|---|---|
| **`MAX_EMAILS_PER_RUN`** (100) | au-delà du seuil, l'exécution s'interrompt **sans rien envoyer**. Protection contre un calcul d'échéance devenu faux qui déclencherait une campagne involontaire |
| **Fenêtres disjointes** | la date du jour appartient à l'étape « expiration », donc jamais deux mails le même jour à la même personne |
| **Déduplication par adresse** | seule l'adhésion la plus récente de chaque e-mail est retenue : un adhérent de trois ans ne reçoit pas trois relances |
| **`RELANCE_MAX_MEMBERSHIP_AGE_DAYS`** (800) | borne l'historique analysé, évite un rattrapage massif au premier lancement |
| **Mode dry-run** | n'envoie rien et n'écrit rien en base : rejouable à volonté |
| **`--dump-dir`** | force le dry-run et écrit chaque mail en `.eml`, `.txt`, `.html` pour relecture avant tout envoi |
| **Enregistrement après succès** | une panne en cours de liste ne fait ni perdre ni dupliquer d'envoi |

---

## 7. La supervision — et son paradoxe

`notify.py` répond à une question simple : **comment être prévenu que le bot
est en panne, quand le bot envoie des mails et que c'est justement le mail qui
est en panne ?**

Deux canaux, tous deux indépendants du SMTP :

**Webhook Discord ou Slack** (`ALERT_WEBHOOK_URL`) — reçoit un message en cas
d'échec.

**Dead-man's switch** (`HEALTHCHECK_URL`, type healthchecks.io) — le bot
« ping » une URL à chaque exécution réussie. Si le ping quotidien n'arrive pas,
le service de supervision alerte. C'est le seul mécanisme qui couvre « le cron
n'a pas tourné », « le serveur est éteint » ou « le script a été tué » —
situations qu'aucune alerte émise *par le bot* ne pourrait signaler, puisqu'il
ne tourne pas.

Les deux sont *best effort* : une alerte qui échoue est journalisée mais ne
fait jamais échouer l'exécution. Un webhook cassé ne doit pas empêcher les
relances de partir.

---

## 8. Le déploiement

### Configuration

Variables d'environnement, alimentées par un `.env` non versionné
(`chmod 600`, présent dans `.gitignore`). Le chargeur est maison — une
quarantaine de lignes — plutôt que `python-dotenv` : une dépendance de moins
pour lire un fichier `clé=valeur`. Il a été durci pour ignorer les commentaires
de fin de ligne, source de pannes obscures du type « host SMTP inutilisable
parce qu'il contient un `#` ».

**Aucun secret n'est écrit dans le code ni copié dans l'image Docker.**

### Cron

```cron
0 9 * * * cd /srv/relance-adhesions && .venv/bin/python -m relance_adhesions >> /var/log/relance-adhesions.cron.log 2>&1
```

Pas de systemd timer : cron est présent partout, se lit sans documentation, et
la tâche ne demande rien de plus.

### Docker

Image `python:3.12-slim`, exécution sous un utilisateur non privilégié
(`uid 10001`) — aucune raison de tourner en root pour un batch. `data/` et
`logs/` sont montés en volume ; **ils doivent être persistés**, faute de quoi
l'anti-doublon repart de zéro à chaque exécution et tout le monde est relancé
en boucle.

### Fuseau horaire

`Europe/Paris` explicite, via `zoneinfo` et `tzdata`. Un batch qui calcule des
échéances à la journée près ne peut pas dépendre du fuseau système : un serveur
en UTC décalerait les relances d'un jour une partie de l'année.

---

## 9. Les tests

Une quarantaine de tests, **sans aucun accès réseau** : le client HTTP est
testé contre une fausse session qui rejoue des réponses préprogrammées.
Conséquence pratique : la suite tourne en moins d'une seconde, partout, sans
identifiants.

Ce qui est couvert : calcul des échéances (y compris le 29 février),
extraction de l'e-mail et repli sur les champs personnalisés, déduplication,
fenêtres des deux étapes, anti-doublon, rendu des mails, liste d'exclusion,
alertes, parsing du `.env`, et la pagination — avec un test de non-régression
par bug rencontré, nommé d'après le piège qu'il garde.

```bash
.venv/bin/python -m pytest tests -q
```

---

## 10. Ce que la stack ne fait pas

Aussi important que le reste, pour qui reprend le projet :

* **Pas de serveur web, pas d'interface.** Tout se pilote en ligne de commande
  et par fichiers de configuration.
* **Pas de suivi d'ouverture ni de clic.** Aucun pixel espion, aucun lien
  tracké. C'est un choix : une relance d'adhésion n'est pas une campagne
  marketing, et le RGPD s'en trouve simplifié.
* **Pas de synchronisation CiviCRM.** L'association en dispose, mais le bot
  lit HelloAsso directement — moins de pièces en mouvement.
* **Pas de gestion des rebonds.** Une adresse morte génère une erreur
  journalisée ; personne ne la retire automatiquement. À surveiller si le taux
  d'échec monte.

---

## 11. Données personnelles

Pause IA est responsable de traitement pour les données de ses adhérents.

* Les **logs contiennent noms et adresses e-mail** : ils ne doivent pas être
  partagés tels quels, ni collés dans une conversation avec une IA.
* Pour toute analyse externe, utiliser `outils/diagnostic_pagination.py`, dont
  la sortie est volontairement dépourvue de données personnelles.
* Aucune donnée n'est conservée au-delà du nécessaire : la base d'anti-doublon
  ne stocke qu'un identifiant, une adresse, une date d'échéance et une date
  d'envoi.
* `.env` contient les secrets API et SMTP : `chmod 600`, jamais versionné.
