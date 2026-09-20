# Suivi — Bot de relance des adhésions HelloAsso (Pause IA)

Document de suivi autonome, destiné à une IA ou à une personne reprenant le
projet sans aucun contexte préalable. Il contient l'état exact du code, les
faits vérifiés sur l'API HelloAsso, les pièges rencontrés, et ce qu'il reste à
faire. Il est mis à jour au fil de l'avancement.

**Dernière mise à jour :** 20 septembre 2026 (après-midi)
**Dépôt :** https://github.com/Romain-Deleglise/bot-relance-hello-asso
**Branche de travail :** `claude/helloasso-membership-renewal-f6j7l0`
**Pull request :** https://github.com/Romain-Deleglise/bot-relance-hello-asso/pull/1

---

## 1. Objectif

HelloAsso ne propose ni reconduction tacite ni mail de relance pour les
adhésions annuelles. Le script comble ce manque : il interroge l'API HelloAsso
une fois par jour via cron, détecte les adhésions proches de leur échéance, et
envoie un mail de relance personnalisé invitant au renouvellement.

Deux relances par échéance :
* **préavis** — 15 jours avant l'échéance (paramétrable) ;
* **jour d'expiration** — le jour J.

---

## 2. État du projet

| Élément | État |
|---|---|
| Code | Complet et fonctionnel |
| Tests | 31 tests, tous au vert, sans accès réseau |
| Authentification API | **Vérifiée sur l'API réelle** |
| Récupération des adhésions | **Vérifiée sur l'API réelle** |
| Calcul des échéances | Vérifié sur données réelles, cohérent avec l'export CSV |
| Pagination | **Confirmée sur l'API réelle** le 20/09/2026 — 198 items analysés (voir 6.1) |
| Paiements mensuels (6.2) | **Confirmé (100 %)** : formulaire verrouillé en Année glissante, pas d'auto-renouvellement — le mail est le seul mécanisme de relance |
| Robustesse | **Audit fait + correctifs appliqués** (voir 6.7) : en-têtes de délivrabilité, retry SMTP, fuseau, déduplication par personne |
| Envoi de mails | Canal retenu : **AWS SES (SMTP)** — reste à renseigner `.env` et vérifier région / production access / SPF-DKIM (voir 6.4). Jamais testé en réel à ce jour |
| Mise en cron | Non faite |
| Textes des mails | Fonctionnels mais à retravailler — **volontairement traités en dernier** (décision Romain 20/09) |

**Aucun mail n'a jamais été envoyé à un adhérent.** Le projet n'a tourné qu'en
mode simulation (`--dry-run`).

---

## 3. Faits vérifiés sur l'API HelloAsso

Ces points ont été établis soit sur le SDK officiel généré depuis l'OpenAPI v5
([HelloAsso/helloasso-python](https://github.com/HelloAsso/helloasso-python)),
soit — pour ceux marqués **[mesuré]** — par appel réel à l'API de production de
Pause IA. Ne pas les remettre en cause sans nouvelle mesure.

### 3.1 Authentification **[mesuré]**

* Endpoint : `POST https://api.helloasso.com/oauth2/token` — **à la racine,
  pas sous `/v5`**.
* Grant : `client_credentials`, avec `client_id` et `client_secret` dans le
  corps en `application/x-www-form-urlencoded`.
* Réponse : `access_token`, `refresh_token`, `expires_in` (1800 s).
* Le client API doit avoir le rôle `OrganizationAdmin` et le privilège
  `AccessTransactions`.

### 3.2 Récupération des adhésions **[mesuré]**

* Endpoint : `GET https://api.helloasso.com/v5/organizations/{slug}/items`
* Filtres utilisés : `tierTypes=Membership`,
  `itemStates=Processed` + `itemStates=Registered`, `withDetails=true`.
* `pageSize` est **plafonné à 100** — au-delà, HTTP 400 `ArgumentInvalid`.

### 3.3 Il n'y a AUCUNE date de fin d'adhésion dans l'API

C'est le point central du projet. Le modèle `Item` expose `order.date` (date de
commande), `payer`, `user`, `name`, `state`, `tierDescription`, `customFields` —
**et aucune échéance**. Elle doit être recalculée.

La règle est portée par le formulaire :
`GET /v5/organizations/{slug}/forms/Membership/{formSlug}/public` renvoie
`validityType` :

| `validityType` | Signification | Calcul |
|---|---|---|
| `MovingYear` | année glissante | `order.date` + 365 jours |
| `Custom` | période fixe | `endDate` du formulaire |
| `Illimited` | sans échéance | jamais relancé |

**[mesuré]** Le formulaire de Pause IA (`formulaire-d-adhesion-a-pause-ia`)
renvoie `validityType=MovingYear`, `endDate=null`. Les adhésions sont donc en
année glissante de 12 mois à partir de la date de commande.

### 3.4 L'e-mail est sur le payeur, pas sur l'adhérent

`payer.email` porte l'adresse ; `user` ne contient que prénom et nom. Pour les
adhésions payées pour un tiers, le script cherche en repli une adresse dans les
`customFields` de l'item.

### 3.5 Pagination — le piège principal **[mesuré]**

Ce point a coûté quatre corrections successives. Les métadonnées de pagination
de cet endpoint sont **inutilisables** :

```json
"pagination": {
  "pageSize": 100, "pageIndex": 1,
  "totalCount": -1,        // « inconnu », PAS un total
  "totalPages": -1,        // « inconnu », PAS un nombre de pages
  "continuationToken": "202605111952183434908_183989374"
}
```

Et surtout, mesuré par `outils/diagnostic_pagination.py` :

```
pageIndex=2 AVEC continuationToken  ->  0 élément      (page vide)
pageIndex=2 SANS token              ->  20 éléments    (suite correcte)
```

**`continuationToken` signifie « reprendre après cet enregistrement ».** Le
transmettre en même temps que `pageIndex=2` demande de sauter une page entière
au-delà du token : l'API répond une page vide, sans erreur.

**Règle retenue :** paginer par `pageIndex` seul, ne jamais envoyer le token,
et s'arrêter uniquement sur une page vide ou incomplète. Ne jamais se fier à
`totalCount` ni à `totalPages`.

L'outil `outils/diagnostic_pagination.py` permet de rejouer ce diagnostic à
tout moment ; sa sortie ne contient aucune donnée personnelle.

---

## 4. Architecture

```
relance_adhesions/
  config.py      # configuration depuis variables d'environnement / .env
  helloasso.py   # client API v5 : OAuth2, pagination, retries, rate limiting
  membership.py  # normalisation des items, calcul des échéances, étapes
  state.py       # anti-doublon et journal d'exécution (SQLite)
  mailer.py      # rendu des templates, envoi SMTP, mode dry-run
  cli.py         # orchestration et ligne de commande
templates/       # 4 fichiers : préavis et expiration, en texte et HTML
outils/          # diagnostic de pagination
tests/           # 31 tests, aucun accès réseau
```

Dépendance unique : `requests`. SQLite et SMTP viennent de la bibliothèque
standard.

**Concepts clés du code :**

* `Membership` — une adhésion normalisée (adhérent, dates, formulaire).
* `Reminder` — un couple `(Membership, étape)` où l'étape vaut `preavis` ou
  `expiration`. C'est l'unité de relance.
* `Reminder.dedup_key` — `item_id:date_de_fin:étape`. La date de fin garantit
  qu'après renouvellement la personne sera relancée l'année suivante ; l'étape
  permet au second mail de partir malgré le premier.
* Les deux fenêtres sont **disjointes par construction** (la date du jour
  appartient à l'étape `expiration`), donc jamais deux mails le même jour.

**Garde-fous délibérés :**

* `MAX_EMAILS_PER_RUN` (100 par défaut) — au-delà, l'exécution s'interrompt
  **sans rien envoyer**. Protection contre un mauvais calcul d'échéance qui
  déclencherait une campagne involontaire.
* Déduplication par adresse : seule l'adhésion la plus récente de chaque
  e-mail est retenue.
* `RELANCE_MAX_MEMBERSHIP_AGE_DAYS` (800) borne l'historique analysé.
* Une relance n'est enregistrée en base qu'**après** un envoi réussi.

---

## 5. Comment tester

```bash
cd ~/bot-relance-hello-asso
git pull
.venv/bin/python -m relance_adhesions --dump-dir ./apercu-mails --days-before 120
```

`--dump-dir` force le mode simulation et écrit chaque mail rendu en `.eml`,
`.txt` et `.html`. Le dry-run n'écrit rien dans la base anti-doublon : il est
rejouable à volonté.

Tests unitaires : `.venv/bin/python -m pytest tests -q`

Diagnostic API : `.venv/bin/python -m outils.diagnostic_pagination`

---

## 6. Tâches restantes, par ordre de priorité

### 6.1 ~~BLOQUANT~~ — RÉSOLU le 20/09/2026 : pagination confirmée

Le correctif (commit `9ddda3b`) a été **exécuté contre l'API réelle** le
20/09/2026 en dry-run. La sortie donne :

```
198 items d'adhésion analysés, 198 exploitables
```

N ≈ 200, et non N = 100 exact : la pagination n'est plus tronquée. Le chiffre
concorde avec les 198 contributeurs annoncés par HelloAsso. Ce blocant est
**fermé**.

Réserve (non bloquante) : la concordance 198 = 198 contributeurs est un signal
fort mais pas une preuve formelle d'exhaustivité. Toute anomalie de comptage
future doit produire un avertissement visible (cf. section 7) plutôt qu'un
arrêt silencieux ; en cas de doute, rejouer `outils/diagnostic_pagination.py`.

### 6.2 ~~BLOQUANT~~ — TRANCHÉ le 20/09/2026 : les relances sont justifiées

**91 % des adhésions de Pause IA sont en « Paiement en plusieurs fois »**
(678 lignes sur 749 dans l'export des paiements).

Question qui était ouverte : **ces prélèvements mensuels s'arrêtent-ils au bout
de 12 mois, ou se poursuivent-ils automatiquement ?**

**Réponse retenue (confiance ~95 %) : ils s'arrêtent. La relance est justifiée.**

Trois éléments convergents :

1. **Deux produits distincts chez HelloAsso** :
   * l'adhésion à **période de validité définie** (année civile, scolaire, ou
     **année glissante = `MovingYear`**) : échéance fixe, **pas de reconduction
     tacite** — l'association doit lancer une campagne de renouvellement ;
   * l'**« adhésion mensuelle »** : abonnement récurrent reconduit tacitement le
     1er du mois, statut membre maintenu tant que l'abonnement est actif.
   Ce sont deux mécanismes différents. Seul le second s'auto-renouvelle.
2. **Paiement en plusieurs fois et renouvellement automatique sont mutuellement
   exclusifs** sur un formulaire HelloAsso (source : centre d'aide et blog
   HelloAsso). Un formulaire qui propose le paiement échelonné — le cas de
   Pause IA — ne peut donc pas être à reconduction tacite ; les mensualités ne
   font que fractionner une cotisation à montant fixe et s'arrêtent une fois le
   total payé.
3. **Corroboration par notre propre mesure** : le formulaire renvoie
   `validityType=MovingYear` avec une échéance calculée à J+365. Un abonnement
   auto-renouvelé n'aurait pas ce modèle de validité à terme fixe.

Sources : blog HelloAsso « Durée de validité et renouvellement des cotisations »
(le renouvellement y est décrit comme une campagne à lancer manuellement),
centre d'aide « paiement en plusieurs fois », blog « votre outil de paiement des
adhésions évolue ».

**Vérification définitive faite le 20/09/2026 (capture de l'admin HelloAsso) :**
le formulaire « Formulaire d'adhésion à Pause IA » est en **Période d'adhésion =
Année glissante**, désormais **verrouillée** (« Des paiements ont déjà été
effectués sur cette campagne. La période d'adhésion et l'adresse web ne peuvent
plus être modifiées. »). C'est donc bien une adhésion à échéance fixe sans
reconduction tacite. Confiance portée à **100 %**. Le système de mail de ce
projet est le seul mécanisme de relance possible ; HelloAsso ne relancera jamais
ces adhérents automatiquement.

Effet de calendrier à garder en tête pour les textes (6.3) : avec un paiement
étalé sur douze mois, le préavis à J-15 arrive entre l'avant-dernière et la
dernière mensualité. Recevoir « votre adhésion arrive à échéance » quinze jours
après avoir payé peut surprendre ; le texte devra le dire explicitement.

### 6.3 Retravailler les textes des mails — À FAIRE EN DERNIER

**Décision Romain (20/09/2026) : on avance d'abord sur la partie technique, les
textes seront retravaillés à la fin.** Ce point est donc gelé jusqu'à ce que le
reste soit stabilisé — d'autant qu'il dépend de la réponse au point 6.2.

Demande explicite de Romain : les quatre templates (`templates/relance.txt`,
`relance.html`, `relance-expiration.txt`, `relance-expiration.html`) sont
fonctionnels mais pas aboutis. Romain a fourni deux textes de référence (ton
chaleureux, argument de l'indépendance, sans mention SEPA) à intégrer ici.

Variables disponibles : `$nom`, `$prenom`, `$nom_complet`, `$email`,
`$date_fin`, `$date_adhesion`, `$formule`, `$association`, `$lien_adhesion`.
Syntaxe `string.Template`, également utilisable dans `MAIL_SUBJECT` et
`MAIL_SUBJECT_EXPIRATION`.

**Point de timing à trancher en même temps que les textes.** Le second mail
proposé dit « votre adhésion **a expiré** le `$date_fin` ». Pour que ce soit
toujours vrai, l'étape `expiration` doit partir **après** l'échéance, pas le
jour J. Or la fenêtre actuelle `[today - days_after ; today]` déclenche le mail
**le jour même** de l'échéance (« expire aujourd'hui »). Deux options quand on
fera les textes : (a) adapter le texte du jour J (« expire aujourd'hui ») ; ou
(b) introduire un décalage pour que le second mail parte N jours après
l'échéance (ex. J+15), ce qui colle au texte « a expiré ». À décider avec les
templates ; ne rien changer à la logique de dates avant.

### 6.4 Configurer l'envoi de mails — canal retenu : AWS SES (SMTP)

**Décision 20/09/2026 : on utilise le compte AWS SES existant de Pause IA**
(compte SES `CiviCRM_Mail`, expéditeur `contact@pauseia.fr`). SES expose un
endpoint SMTP standard : **aucun code à écrire**, tout passe par la config
`SMTP_*` (voir le bloc SES commenté dans `.env.example`).

Prérequis :

1. **Production access + expéditeur vérifié (SPF/DKIM)** — **confirmés** : le
   compte SES sert déjà CiviCRM en production depuis `contact@pauseia.fr`.
2. **Région** — le endpoint est `email-smtp.<région>.amazonaws.com`. **Recopier
   le `SMTP_HOST` déjà utilisé par CiviCRM** : les identifiants SMTP SES sont
   liés à une région (le mot de passe est dérivé de la clé IAM *et* de la
   région), donc le mot de passe existant n'authentifie que dans la région de
   CiviCRM. Même host + mêmes identifiants = même région garantie. Inutile de
   tester à l'aveugle.

Identifiants (nom d'utilisateur SMTP `AKIA…`, mot de passe SMTP SES) : **dans
`.env` uniquement** (`chmod 600`, gitignoré). Jamais dans le code, un commit ou
un échange. Le mot de passe SMTP SES n'est PAS le secret access key IAM.

Débit : SES tolère 14 msg/s en production ; `SMTP_DELAY_SECONDS=1` (défaut)
laisse une marge très large pour ~30 relances.

Premier envoi réel recommandé : `MAX_EMAILS_PER_RUN=5` et l'adresse de Romain
en `MAIL_BCC`.

Pour passer plus tard à l'API HTTP SES (v2) plutôt qu'au SMTP : écrire dans
`mailer.py` une classe exposant `send(reminder, rendered_mail)` et la substituer
à `SmtpMailer` dans `cli.py`. Non nécessaire tant que le SMTP suffit.

### 6.5 Mise en production — durcie le 20/09/2026

`crontab.example` et `Dockerfile` révisés :

* **`flock`** dans la ligne cron : empêche deux exécutions de se chevaucher (une
  exécution lente ne peut plus être doublée par la suivante → pas de double
  relance dans la fenêtre sélection/écriture anti-doublon).
* **Heure de cron ≠ logique de dates** : cron tourne à l'heure du serveur
  (souvent UTC), mais la sélection des adhésions suit `RELANCE_TIMEZONE`. Exemple
  fourni : `0 7 * * *` UTC = 9 h Paris (été).
* **`MAILTO`** : cron notifie en cas de sortie non vide (erreur).
* **Docker** : exécution sous utilisateur non-root (uid 10001), dossiers
  `data/` et `logs/` créés et attribués. Les dossiers hôtes montés doivent être
  accessibles en écriture par cet uid (`chown -R 10001 data logs`).
* **`tzdata`** ajouté à `requirements.txt` : sans lui, `zoneinfo` échoue sur les
  images « slim ». Repli ultime sur UTC codé en dur (`resolve_timezone`) pour
  qu'un fuseau manquant ne puisse jamais empêcher le bot de tourner.

Les volumes `data/` (base anti-doublon) et `logs/` doivent être persistés.

### 6.6 Administratif

Le dépôt était vide à la création : après le merge de la PR #1, `main` existe et
porte le code, mais la **branche par défaut du dépôt pointe encore sur
`claude/helloasso-membership-renewal-f6j7l0`** au lieu de `main`. À corriger
dans Settings → Branches côté GitHub (réglage non modifiable par le script).

---

### 6.7 Audit de robustesse et correctifs appliqués (20/09/2026, après-midi)

Audit en lecture seule du cœur du code, puis correctifs. Ce qui était déjà
solide (pagination, retries/backoff réseau, refresh token sur 401, garde-fous
anti-campagne, écriture en base après envoi, `safe_substitute`, gestion Bcc
sans fuite) n'a pas été touché. Correctifs :

* **H1 — En-têtes de délivrabilité.** `build_message` pose désormais `Date` et
  `Message-ID` explicites (leur absence pénalise l'anti-spam et casse le
  threading ; `smtplib` ajoutait `Date` mais pas `Message-ID`).
* **H2 — Erreur SQLite après envoi.** `mark_sent` est désormais protégé : une
  écriture anti-doublon qui échoue est signalée sans interrompre le batch (avant,
  elle plantait toute l'exécution et provoquait un renvoi au passage suivant).
* **H3 — Cadence d'envoi.** Temporisation configurable entre messages
  (`SMTP_DELAY_SECONDS`, 1 s) + reconnexion périodique optionnelle
  (`SMTP_MAX_PER_CONNECTION`).
* **H4 — Retry sur rejet temporaire.** Un rejet SMTP 4xx (greylisting,
  throttling) est réessayé avec backoff (`SMTP_RETRY_ATTEMPTS`,
  `SMTP_RETRY_DELAY_SECONDS`) ; un 5xx échoue immédiatement.
* **D1 (option c) — Déduplication par personne.** `dedupe_memberships` clé sur
  `(e-mail, nom)` et non l'e-mail seul : deux personnes partageant une adresse
  (même payeur) sont relancées chacune, et les adhésions successives d'une même
  personne sont fusionnées (plus de relance après renouvellement, même anticipé).
* **D2 — Fuseau horaire.** Tout le calcul de dates est ancré sur
  `RELANCE_TIMEZONE` (défaut `Europe/Paris`) : plus de décalage d'un jour dû à
  l'UTC du serveur pour les commandes passées tard le soir.
* **D3 — List-Unsubscribe.** En-tête `List-Unsubscribe: mailto:` ajouté
  (`UNSUBSCRIBE_EMAIL`, repli sur Reply-To puis l'expéditeur). Pas de variante
  « One-Click » HTTP (exigerait un endpoint web).

Tests : 39 au vert, toujours sans accès réseau. Nouveaux tests couvrant le
fuseau, la déduplication par personne, les en-têtes de délivrabilité et le retry
SMTP transitoire vs définitif.

---

## 7. Leçons de la session, à ne pas rejouer

**La troncature silencieuse a coûté cinq échanges.** Le script paraissait
fonctionner parfaitement tout en n'analysant que 100 adhérents sur ~200. Quatre
corrections successives ont été faites sur hypothèse, chacune ayant l'air
raisonnable :

1. `totalPages` absent → repli sur 1 → arrêt page 1.
2. `totalCount = -1` pris pour un total → arrêt page 1.
3. `totalPages = -1` → `pageIndex >= totalPages` vrai → arrêt page 1.
4. Envoi simultané du token et de `pageIndex` → page vide → arrêt page 1.

La correction n'est venue qu'après avoir **mesuré** le comportement réel de
l'API plutôt que de le déduire. L'outil `outils/diagnostic_pagination.py` est né
de ce constat : en cas de comportement inattendu de l'API, mesurer d'abord.

Corollaire pratique : toute anomalie de comptage doit produire un
**avertissement visible** plutôt qu'un arrêt silencieux.

---

## 8. Données et RGPD

Pause IA est responsable de traitement pour les données de ses adhérents. Les
logs du script contiennent noms et adresses e-mail ; ils ne doivent pas être
partagés tels quels. Pour toute analyse externe, utiliser
`outils/diagnostic_pagination.py`, dont la sortie est volontairement dépourvue
de données personnelles, ou un export anonymisé.

`.env` contient les secrets API et SMTP. Il est dans `.gitignore` et doit
rester en `chmod 600`.
