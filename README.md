# ViewLink Local

Envoyer un trade depuis un graphique TradingView vers MetaTrader 5, **en local**,
sans service cloud ni abonnement. Rien n'est automatique : chaque ordre exige un
clic de confirmation dans l'extension, apres verification des valeurs.

## Schema

```
 TradingView (navigateur)                         Votre PC (127.0.0.1 uniquement)
+---------------------------+                   +------------------------------+
| Indicateur Pine ViewLink  |                   |  Bridge Python               |
| plots: Entry, SL, TP, Type|                   |  bridge/server.py :8787      |
+-------------+-------------+                   |  file d'attente (TTL 30 s)   |
              | legende du graphique (DOM)      |  + 50 derniers rapports      |
              v                                 +------+---------------^-------+
+---------------------------+  POST /signal            |               |
| Extension Chrome (MV3)    | -----------------------> |               | GET /next (polling)
| popup: lecture, edition,  |  GET /results            |               | POST /result
| clic "Execute"            | <----------------------- |               |
+---------------------------+                   +------v---------------+-------+
                                                |  EA MetaTrader 5             |
                                                |  mt5/ViewLinkEA.mq5          |
                                                |  re-validation + CTrade      |
                                                +------------------------------+
```

Flux : l'indicateur affiche Entry/SL/TP/Type -> le popup les lit et les affiche
-> vous verifiez/corrigez -> clic **Execute** -> le bridge valide et met en file
-> l'EA recupere le signal, le re-valide contre le prix reel, execute (ou rejette)
-> le rapport (ticket ou message d'erreur) s'affiche dans le popup.

## Arborescence

```
pine/viewlink_signal.pine     indicateur TradingView (Pine Script v5)
extension/                    extension Chrome MV3 (manifest, content, popup)
bridge/server.py              bridge local (Python 3.8+, bibliotheque standard)
bridge/test_bridge.py         tests du bridge (unittest, compatible pytest)
mt5/ViewLinkEA.mq5            Expert Advisor MetaTrader 5
tests/                        verifications de l'extension (statique + bout en bout)
CHANGELOG.md                  version, et ce qui n'a pas pu etre teste
```

## Installation pas a pas

L'ordre conseille : bridge, puis EA, puis indicateur, puis extension.

### 1. Bridge Python

Prerequis : Python 3.8 ou plus recent. Aucune dependance a installer.

```
cd bridge
python server.py
```

Au premier lancement, une cle API est generee, affichee dans la console et
sauvegardee dans `bridge/.viewlink_key` (droits 600, ignore par git). Elle est
reutilisee aux lancements suivants. Notez-la : elle sert a l'extension et a l'EA.

Variables d'environnement (optionnelles) :

| Variable        | Defaut | Role                                                     |
|-----------------|--------|----------------------------------------------------------|
| `VIEWLINK_KEY`  | -      | impose la cle API (prioritaire sur le fichier)           |
| `VIEWLINK_PORT` | 8787   | port d'ecoute (toujours sur 127.0.0.1)                   |
| `VIEWLINK_TTL`  | 30     | duree de vie d'un signal non recupere, en secondes       |

Verification : ouvrir `http://127.0.0.1:8787/health` dans le navigateur doit
renvoyer `{"ok":true,"service":"viewlink-bridge"}`.

Si vous changez le port, il faut aussi modifier `host_permissions` dans
`extension/manifest.json`, l'URL dans les Settings de l'extension, l'input
`InpBridgeUrl` de l'EA et la liste WebRequest de MT5.

### 2. Expert Advisor MetaTrader 5

1. Dans MT5 : **Fichier > Ouvrir le dossier des donnees**, puis copier
   `mt5/ViewLinkEA.mq5` dans `MQL5/Experts/`.
2. Ouvrir le fichier dans MetaEditor (F4) et compiler (F7).
3. **Outils > Options > Expert Advisors** : cocher *Autoriser WebRequest pour les
   URL listees* et ajouter `http://127.0.0.1:8787`.
4. Activer le bouton **Algo Trading** de la barre d'outils.
5. Glisser l'EA sur **un seul** graphique (n'importe quel symbole : l'EA trade le
   symbole indique par le signal). Renseigner `InpApiKey` avec la cle du bridge.
6. L'onglet **Experts** doit afficher `ViewLink: started, polling ...`.

Parametres de l'EA :

| Input             | Defaut                  | Role                                                   |
|-------------------|-------------------------|--------------------------------------------------------|
| `InpBridgeUrl`    | `http://127.0.0.1:8787` | URL du bridge (doit etre locale)                       |
| `InpApiKey`       | vide (obligatoire)      | cle API affichee par le bridge                         |
| `InpMaxLots`      | 1.0                     | plafond dur : tout signal au-dessus est rejete         |
| `InpDeviation`    | 20                      | ecart de prix toleree pour les ordres au marche (points)|
| `InpMagic`        | 870787                  | magic number des ordres                                |
| `InpPollMs`       | 1000                    | intervalle d'interrogation du bridge (>= 250 ms)       |
| `InpAllowMarket`  | true                    | autoriser les ordres au marche                         |
| `InpAllowPending` | true                    | autoriser les ordres limit/stop                        |

Controles effectues par l'EA avant tout envoi : trading autorise (terminal, EA,
compte), symbole connu et tradable, cotations disponibles, lots <= `InpMaxLots`,
lots arrondis au pas du symbole (et compris entre min et max), prix arrondis au
tick, regles limit/stop par rapport a ask/bid, SL/TP du bon cote du prix reel
(et a distance >= stops level). Tout rejet est renvoye au bridge avec un message
explicite et apparait dans le popup.

### 3. Indicateur TradingView

1. Sur TradingView, ouvrir **Pine Editor**, coller le contenu de
   `pine/viewlink_signal.pine`, **Enregistrer** puis **Ajouter au graphique**.
2. Dans les parametres de l'indicateur, choisir direction, type d'ordre, et
   eventuellement entree / SL / TP manuels (0 = automatique).

| Input                       | Defaut | Role                                          |
|-----------------------------|--------|-----------------------------------------------|
| Direction                   | Buy    | Buy ou Sell                                   |
| Order type                  | Market | Market, Limit ou Stop                         |
| Entry price (0 = last close)| 0      | prix d'entree                                 |
| Stop loss (0 = auto)        | 0      | SL auto = entree -/+ ATR x multiplicateur     |
| Take profit (0 = auto)      | 0      | TP auto = entree +/- distance SL x RR         |
| ATR length                  | 14     | periode de l'ATR                              |
| ATR multiplier for SL       | 1.5    | multiplicateur ATR                            |
| Risk/reward ratio           | 2.0    | ratio risque/rendement                        |

L'indicateur expose exactement 4 plots, dans cet ordre : Entry, SL, TP (traces
sur le graphique) et Type (barre de statut et Data Window uniquement).
Code Type : 1 buy market, 2 buy limit, 3 buy stop, -1 sell market, -2 sell limit,
-3 sell stop. **Ne pas renommer l'indicateur** : son titre doit contenir "ViewLink".
Les valeurs de l'indicateur doivent etre visibles dans la legende (option
*Valeurs des indicateurs* de la barre de statut activee).

### 4. Extension Chrome

1. Ouvrir `chrome://extensions`, activer le **Mode developpeur**.
2. **Charger l'extension non empaquetee** et choisir le dossier `extension/`.
3. Recharger l'onglet TradingView (F5) : le script de contenu n'est injecte que
   dans les pages ouvertes apres l'installation.
4. Ouvrir le popup, deplier **Settings** : coller la cle API, verifier l'URL du
   bridge, renseigner le suffixe de symbole du courtier (ex. `.m`, `#`) et les
   lots par defaut, puis **Save settings**.

Utilisation : sur le graphique, **sortir le curseur de la zone du graphique**
(la legende affiche alors les valeurs de la derniere bougie), ouvrir le popup.
La lecture est lancee automatiquement (bouton **Read chart** pour relire).
Verifier et corriger si besoin symbole, lots, sens, type, entree, SL, TP, puis
cliquer **Execute**. Le popup attend le rapport de l'EA jusqu'a 10 s : ticket en
cas de succes, message en cas de rejet, ou avertissement de delai depasse.
Apres chaque envoi, une nouvelle lecture est necessaire pour reactiver Execute.

## API du bridge

Toutes les routes sauf `/health` exigent l'en-tete `X-API-Key`.

| Methode | Route      | Appelant  | Reponse                                              |
|---------|------------|-----------|------------------------------------------------------|
| GET     | `/health`  | tous      | `{"ok":true,...}`                                    |
| POST    | `/signal`  | extension | `{"ok":true,"id":"<12 hex>","ts":...}` ou 400/401    |
| GET     | `/next`    | EA        | signal plat ou `{}` si la file est vide              |
| POST    | `/result`  | EA        | `{"ok":true}`                                        |
| GET     | `/results` | extension | `{"ok":true,"results":[...]}` (50 derniers)          |
| OPTIONS | `*`        | navigateur| preflight CORS (origines `chrome-extension://` seulement) |

Exemple de signal renvoye par `/next` (objet plat, JSON compact) :

```
{"symbol":"EURUSD","side":"buy","order":"limit","lots":0.1,"entry":1.08,"sl":1.075,"tp":1.09,"comment":"ViewLink","id":"3f2a9c01b7de","ts":1760000000.123}
```

Validation de `/signal` : symbole `^[A-Za-z0-9._#\-]{1,24}$` ; side `buy|sell` ;
order `market|limit|stop` ; 0 < lots <= 100 ; entree obligatoire pour limit/stop ;
nombres finis et non negatifs ; coherence (buy : SL < entree < TP ; sell :
SL > entree > TP ; 0 = non defini) ; commentaire assaini (31 caracteres max) ;
corps limite a 10 Ko. Un signal non recupere apres 30 s est supprime et un rapport
`expired` est enregistre pour que le popup l'affiche.

## Tests

```
./tests/run_all.sh                # tout
python -m pytest bridge           # bridge seul (ou: python bridge/test_bridge.py)
SKIP_E2E=1 ./tests/run_all.sh     # sans le test navigateur
```

- `bridge/test_bridge.py` : authentification refusee, signal valide, symbole
  invalide, geometrie SL/TP invalide, champs invalides, expiration d'un signal
  perime, file vide, ordre FIFO, stockage et limite des resultats, CORS, cle.
- `tests/check_extension.py` : validation du manifest, absence de script inline et
  de ressource externe, absence d'emoji dans les sources.
- `node --check` sur tous les fichiers JS.
- `tests/extension_e2e.js` : charge l'extension dans Chromium (Playwright), sert une
  imitation de la legende TradingView, lance le vrai bridge et simule l'EA : erreur
  si l'onglet n'est pas TradingView, erreur si l'indicateur est absent, analyse des
  nombres (moins unicode, espaces fines, milliers), blocage d'un ordre incoherent,
  double clic = un seul signal, affichage du ticket, du rejet, du delai depasse et
  d'une cle API refusee.

## Securite

- Aucun ordre sans clic de l'utilisateur sur **Execute** ; l'indicateur ne fait
  qu'afficher des valeurs. Protection contre le double clic, et relecture
  obligatoire entre deux ordres.
- Le bridge n'ecoute que sur `127.0.0.1` (non configurable), jamais sur le reseau.
- Cle API obligatoire (comparaison a temps constant `hmac.compare_digest`),
  aucune cle codee en dur ; fichier de cle en droits 600 et exclu de git.
- CORS accorde uniquement aux origines `chrome-extension://` : une page web
  quelconque ne peut pas lire les reponses du bridge (et ne connait pas la cle).
- Validation des entrees des deux cotes : bridge (format, coherence) et EA
  (re-validation complete contre le marche reel, plafond `InpMaxLots`).
- Les signaux expirent apres 30 s : un ordre oublie ne part pas plus tard.
- Restez prudent : la cle est stockee en clair dans `chrome.storage.local` et dans
  `bridge/.viewlink_key`. Toute personne ayant acces a votre session utilisateur
  peut envoyer des ordres. Testez d'abord sur un **compte demo**.

## Limites connues

- **DOM TradingView** : la lecture repose sur des selecteurs non documentes
  (`data-name="legend-source-item"`, `legend-source-title`, classes `valueValue`).
  TradingView les modifie regulierement ; ils sont isoles dans trois fonctions en
  tete de `extension/content.js` (`findLegendItems`, `getLegendTitle`,
  `getLegendValueNodes`).
- **Valeurs sous le curseur** : la legende affiche les valeurs de la bougie
  survolee. Si le curseur est sur le graphique, les valeurs lues peuvent etre
  celles d'une bougie passee. Toujours verifier dans le popup.
- **Precision** : les valeurs lues sont celles affichees (precision du symbole sur
  TradingView), pas la valeur interne exacte.
- **Separateurs** : `1,234` est interprete comme 1234 (milliers). Une virgule
  decimale avec exactement 3 decimales serait mal lue.
- **Symbole** : deduit du titre de l'onglet ; les noms TradingView et ceux du
  courtier different souvent (suffixe, `XAUUSD` vs `GOLD`, contrats futures).
  Utiliser le suffixe des Settings ou corriger le champ a la main.
- **Ordres au marche** : executes au prix MT5 du moment ; l'entree lue sur le
  graphique ne sert qu'aux controles cote bridge.
- **Un signal a la fois par cycle** de polling ; un seul EA doit interroger le
  bridge (sinon les signaux sont repartis entre les EA).
- WebRequest n'est pas disponible dans le testeur de strategie MT5.
- Pas de HTTPS sur la boucle locale (inutile hors reseau, mais le trafic local
  n'est pas chiffre).

Voir `CHANGELOG.md` pour la liste de ce qui n'a pas pu etre teste.
