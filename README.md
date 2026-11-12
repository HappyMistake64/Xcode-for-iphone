Xcode Pocket — Xcode for iPhone

Created by David Volf.

Mobilní vývojové prostředí pro tvorbu Swift/SwiftUI aplikací z iPhonu s kompilací přes GitHub Actions na vzdáleném Macu.

Stav: prototyp. Kompilace ukázkové nativní aplikace je ověřená; podpis a instalace pouze z telefonu nejsou dokončené. Projekt není port Apple Xcode ani produkt společnosti Apple.

Webový editor · GitHub Actions · Ověřený úspěšný build

Cíl

Na iPhonu vytvořit projekt, upravit Swift, spustit kompilaci, získat aplikaci a nainstalovat její aktualizaci bez vlastního počítače. Primární zařízení autora je iPhone 15 Pro. Cílem je také možnost instalace bez vlastního placeného Apple Developer členství; konkrétní podpisová cesta a její náklady nejsou dosud potvrzené.

Co je v tomto repozitáři

|Cesta                             |Účel                                             |
|----------------------------------|-------------------------------------------------|
|`.github/workflows/pocket-ios.yml`|Ručně spouštěná kompilace na macOS               |
|`native-build/generate_project.py`|Generátor nativního Xcode projektu ze snímku JSON|
|`pocket-builds/`                  |Snímky projektů odeslané ke kompilaci            |

Repozitář obsahuje build backend a nový samostatný mobilní editor ve složce [web/](web/README.md). Starší externí webový editor není tímto automaticky přepsán. Publikace GitHub Pages vyžaduje zapnutí Pages v nastavení repozitáře.

Ověřený stav k 12. 11. 2026

• Workflow Pocket iOS build používá macos-26, timeout 15 minut a xcodebuild.
• Běh 43123517253 skončil success. Dokládá kompilaci ukázkové aplikace, nikoli instalaci celého editoru.
• Generátor v5 podporuje schéma projektu v1–v3; v3 umí ověřené base64 obrázky a omezené image sety Assets.xcassets.
• Workflow generuje PocketApp-unsigned.app.zip a strukturovanou PocketApp-unsigned.ipa; oba výstupy jsou NEPODEPSANÉ. Test balení a kompilace na macOS je ověřený.
• Artefakty a dostupné logy jsou uchovávané 7 dní.
• Automatický podpis, instalace na fyzický iPhone a celý cyklus aktualizace zatím nejsou ověřené.

Architektura

1. Uživatel upraví projekt v mobilním webovém editoru.
2. Editor uloží JSON snímek do pocket-builds/<request_id>.json.
3. Spustí workflow s UUID požadavku a přesným SHA commitu.
4. Runner načte tento commit a generátor vytvoří PocketApp.xcodeproj.
5. xcodebuild sestaví Release pro generic/platform=iOS s CODE_SIGNING_ALLOWED=NO.
6. Workflow ověří checkout SHA a zveřejní nepodepsanou aplikaci, logy a identifikační manifest v artefaktech svázaných s request UUID i přesným SHA commitu.
7. Nepodepsaná IPA je připravená pro legitimní podpisovou cestu. Podpis a instalace do fyzického iPhonu zůstávají neověřené.

Kompilace probíhá v cloudu, ne přímo na procesoru iPhonu. Vyžaduje připojení a dostupný macOS runner.

Použití aktuálního workflow

V editoru nastavte repozitář HappyMistake64/Xcode-for-iphone, větev main, název aplikace a Bundle ID. Ověřte připojení, dostupnost workflow a odešlete Swift projekt ke kompilaci. Výsledek ověřujte také na kartě Actions.

Při ručním spuštění workflow musí snímek projektu již existovat v zadaném commitu:

|Vstup         |Hodnota                                        |
|--------------|-----------------------------------------------|
|`source_sha`  |Úplné 40znakové SHA commitu obsahujícího snímek|
|`request_id`  |UUID tohoto požadavku                          |
|`project_path`|Přesně `pocket-builds/<request_id>.json`       |
|`app_name`    |Zobrazovaný název aplikace                     |
|`bundle_id`   |Například `cz.davidvolf.pocketapp`             |

Nepodepsané .app.zip nelze instalovat do iPhonu pouhým otevřením. Workflow nyní sestaví správnou strukturu unsigned IPA, ale ani ta bez podpisu a provisioningu neprojde instalací.

Formát projektu a limity

Generátor přijímá JSON s format: "xcode-pocket-project", version: 1, 2 nebo 3 a mapou files obsahující relativní cesty a textové obsahy. Schéma v3 může navíc obsahovat mapu binaries s MIME typem a base64 daty obrázků. Minimálně jeden soubor musí mít příponu .swift; spustitelný projekt potřebuje také vstupní bod, například SwiftUI @main.

• Nejvýše 4 000 000 bajtů vstupního JSON a 1–200 textových souborů.
• Relativní cesty, bez konfliktů soubor/složka a názvů lišících se pouze velikostí písmen.
• Resources/: JSON, TXT, CSV, PLIST, XML, MD, HTML, CSS, JS.
• Schéma v3 podporuje PNG/JPEG/GIF/WebP v Resources/ a omezené univerzální PNG/JPEG image sety v Resources/Assets.xcassets; obecné asset katalogy, app ikony a lokalizační balíčky podporované nejsou.
• Webové soubory mimo Resources/ mohou být ve snímku, ale nejsou kompilované jako Swift.
• Jeden generovaný target a scheme PocketApp; výchozí deployment target iOS 17.0.
• Libovolné existující Xcode projekty, externí balíčky, rozšíření a více targetů nejsou implementovaným importním rozhraním.

Samostatné generování projektu (neprovádí kompilaci ani podpis):

python3 native-build/generate_project.py \
  --input pocket-builds/REQUEST_UUID.json \
  --output build-project \
  --app-name PocketApp \
  --bundle-id cz.davidvolf.pocketapp

Nahraďte REQUEST_UUID skutečným názvem existujícího snímku. Výstupní adresář musí být prázdný nebo neexistovat.

Známé problémy

Nesprávné přiřazení stavu běhu

Build Manager a nový editor ve web/ ověřují konkrétní workflow, request UUID, run ID a SHA-vázaný identitní artefakt. Specifikace: [Reliable Builds](docs/RELIABLE_BUILDS.md). Starší externí editor není aktualizovaný a iPhone Safari E2E test není dosud doložen.

V editoru byla zaznamenána zpráva „GitHub vrátil jiný běh než požadovanou kompilaci“, přestože kompilace na GitHubu uspěla. Příčina ani oprava nejsou potvrzené. Je třeba ověřit mapování request ID, workflow run ID, názvu běhu a commitu. Kontrola nesmí být odstraněna způsobem, který by zobrazoval cizí artefakt jako výsledek aktuálního požadavku.

Do opravy nejprve ověřte původní běh v Actions a použijte obnovení stavu; nespouštějte automaticky duplicitní kompilace.

Ověření připojení

Dříve se objevilo 401 Bad credentials; uživatel následně vytvořil nový token a nainstaloval workflow. Tato chyba přihlášení je oddělená od chyby párování běhů.

Instalace pouze z iPhonu

Podpis je samostatný krok po kompilaci. Dosud nebyl objednán ani ověřen žádný podpisový poskytovatel.

• SideStore: oficiální postup vyžaduje počítač pro první instalaci; současné omezení autora „jen iPhone“ tím není splněno.
• Signulous: zvažovaná služba pro podpis vlastní IPA přes web. Kompatibilitu, aktuální cenu, obnovování a podmínky je nutné ověřit před nákupem.
• Placené Apple Developer členství a distribuce přes TestFlight jsou odlišná cesta; nejsou nyní nastavené.

Neexistuje zde implementované obejití kontroly podpisu iOS. Kompletně bezplatná instalace jen z telefonu není ověřeným výsledkem projektu.

Roadmapa a kritéria dokončení

☐ Zkontrolovat obsah úspěšného artefaktu, nejen stav workflow.
◐ Nový samostatný editor uchovává identitu buildu i po obnovení stránky a má regresní testy. Živý Safari E2E průchod je nutné ještě ověřit; starší externí editor se nemění.
☑ Vytvořit nepodepsanou IPA se strukturou Payload/PocketApp.app a ověřit její obsah na macOS. Stažení je zatím přes GitHub Actions artefakt (ZIP), nikoli přímo jediným tlačítkem do Souborů.
☐ Vybrat a ověřit podpisovou cestu ovladatelnou pouze z iPhonu. Případný nákup schvaluje uživatel.
☐ Nainstalovat a otevřít ukázkovou aplikaci na skutečném iPhonu.
☐ Změnit Swift kód, sestavit aktualizaci a ověřit změnu v nainstalované aplikaci.
◐ Nový webový editor implementuje lokální uložení a JSON import/export; navigace na přesné řádky chyb kompilátoru ještě chybí.
☐ Přidat podporu binárních prostředků a asset katalogů.
☐ Samostatně navrhnout nativní verzi editoru; není podmínkou prvního kompletního vývojového cyklu.

Hlavní milník: uživatel pouze s iPhonem vytvoří projekt, upraví kód, sestaví aplikaci, nainstaluje ji a ověří její aktualizaci.

REA a původ projektu

Původní výzkumný záměr používal REA k prozkoumání Xcode. Analýza macOS binárek nepředstavuje jejich automatický převod na iOS. Tento repozitář neposkytuje Apple Xcode, SDK ani jeho proprietární zdrojové kódy. Aktuální praktická cesta používá Xcode na vzdáleném macOS runneru.

Přispívání a hlášení chyb

Do issue přiložte očekávané a skutečné chování, kroky reprodukce, verzi iOS/prohlížeče a odkaz na příslušný GitHub Actions běh. U problému s generátorem použijte minimální reprodukovatelný JSON bez soukromých údajů. U dokumentace rozlišujte ověřené funkce, známé problémy a návrhy.

Repozitář je veřejný: odeslané projektové snímky jsou veřejné. Nevkládejte do nich tokeny, hesla ani podpisové klíče. Náklady kompilací se řídí aktuálními podmínkami GitHub Actions.

Autor a licence

Created by David Volf — HappyMistake64.

Licence projektu zatím nebyla zvolena. Veřejná dostupnost repozitáře sama nepředstavuje udělení open-source licence. Xcode je označení produktu společnosti Apple; tento nezávislý projekt není společností Apple podporován.

English overview

Xcode Pocket is an experimental iPhone-first SwiftUI editor and GitHub Actions macOS build backend. The repository now includes an independent mobile PWA editor in web/, exact build matching, and a tested unsigned .ipa alongside the .app.zip. GitHub Pages must still be enabled, the real iPhone browser flow tested, and a legitimate signing/install path provided. An unsigned IPA cannot be installed by itself. This project is not Apple Xcode.
