import {
  STORAGE_KEY, REPOSITORY, GithubAPI, newProject, makeRequest, validateProject,
  validateSettings, restoreState, reconcileArtifacts, validPath
} from "./core.mjs";

const byId = id => document.getElementById(id);
const local = restoreState(localStorage.getItem(STORAGE_KEY));
const state = {project:local.project,settings:local.settings,builds:local.builds,active:local.active};
let api = null, polling = false, busy = false;

function persist() {
  localStorage.setItem(STORAGE_KEY,JSON.stringify(state));
}
function note(message, type="info") {
  const area = byId("status");
  area.textContent = message;
  area.dataset.type = type;
}
function errorMessage(err) {return err instanceof Error ? err.message : String(err);}
function htmlEl(tag,className,text) {
  const el=document.createElement(tag);
  if (className) el.className=className;
  if (text !== undefined) el.textContent=text;
  return el;
}
function syncSettings() {
  state.settings=validateSettings({repo:byId("repo").value,appName:byId("app-name").value,bundleId:byId("bundle-id").value});
  persist();
}
function saveEditing() {
  if (!state.active || !(state.active in state.project.files)) return;
  state.project.files[state.active]=byId("code").value;
  persist();
}
function renderFiles() {
  const names=Object.keys(state.project.files).sort();
  if (!names.includes(state.active)) state.active=names[0];
  byId("files").replaceChildren(...names.map(name=>{
    const option=htmlEl("option","",name);
    option.value=name;option.selected=name===state.active;return option;
  }));
  byId("code").value=state.project.files[state.active]||"";
  byId("filename").textContent=state.active;
}
function renderBuilds() {
  const wrapper=byId("builds");
  wrapper.replaceChildren();
  if (!state.builds.length) {
    wrapper.append(htmlEl("p","muted","Zatím žádná kompilace. První build spustíš tlačítkem Sestavit."));
    return;
  }
  for (const item of state.builds) {
    const box=htmlEl("article","build-card");
    const title=htmlEl("div","build-title");
    title.append(htmlEl("strong","",item.appName+" · "+item.stage));
    title.append(htmlEl("span","muted",new Date(item.created_at).toLocaleString("cs-CZ")));
    box.append(title,htmlEl("div","mono",item.request_id));
    if (item.source_sha) box.append(htmlEl("div","small mono","Commit "+item.source_sha.slice(0,12)));
    if (item.message) box.append(htmlEl("p","small",item.message));
    if (item.run_id) box.append(htmlEl("div","small","Workflow run #"+item.run_id));
    if (Array.isArray(item.artifacts)&&item.artifacts.length) {
      const list=htmlEl("div","chips");
      for (const a of item.artifacts) list.append(htmlEl("span","chip",a.name));
      box.append(list);
    }
    const actions=htmlEl("div","action-row");
    const refresh=htmlEl("button","secondary","↻ Ověřit");
    refresh.type="button";
    refresh.addEventListener("click",()=>void refreshBuild(item));
    actions.append(refresh);
    if (item.stage==="ambiguous" || item.stage==="mismatch") {
      const choose=htmlEl("button","secondary","Zadat Run ID");
      choose.type="button";
      choose.addEventListener("click",()=>{
        const answer=prompt("Zadej přesné GitHub Actions Run ID ze stránky Actions.");
        if (!answer) return;
        if (!/^[1-9][0-9]*$/.test(answer) || !Number.isSafeInteger(Number(answer))) return note("Neplatné Run ID.","error");
        item.run_id=Number(answer);item.stage="dispatched";item.message="";
        persist();renderBuilds();void refreshBuild(item);
      });
      actions.append(choose);
    }
    if (Number.isSafeInteger(item.run_id) && item.run_id>0) {
      const link=htmlEl("a","small external","📦 Otevřít běh a stáhnout artefakty");
      link.href="https://github.com/"+encodeURI(item.repo)+"/actions/runs/"+item.run_id;
      link.target="_blank";link.rel="noopener noreferrer";
      actions.append(link);
    }
    box.append(actions);
    wrapper.append(box);
  }
}
async function connect() {
  try {
    syncSettings();
    const token=byId("connect-token").value;
    const candidate=new GithubAPI(token,state.settings.repo);
    const repo=await candidate.check();
    if (repo.full_name!==state.settings.repo) throw Error("GitHub vrátil jiný repozitář.");
    api=candidate;
    byId("connect-token").value="";
    byId("connection").textContent="● Připojeno: "+state.settings.repo;
    byId("connection").dataset.connected="true";
    note("Připojeno. Token zůstává pouze v paměti otevřené stránky.","success");
    await refreshAll();
  } catch(err){note(errorMessage(err),"error");}
}
async function submit() {
  if (busy) return;
  busy=true;
  byId("btn-build").disabled=true;
  try {
    if (!api) throw Error("Nejdřív připoj GitHub token.");
    saveEditing();syncSettings();validateProject(state.project);
    if (state.settings.repo!==api.repo) throw Error("Repozitář se změnil, znovu se připoj.");
    const req=makeRequest(state.settings,state.project);
    state.builds.unshift(req);state.builds=state.builds.slice(0,30);persist();renderBuilds();
    note("Ukládám neměnný snímek projektu do GitHubu...");
    try {
      req.source_sha=await api.snapshot(req,state.project);
      req.stage="committed";persist();renderBuilds();
      // Persist the source SHA BEFORE dispatch: a network failure must not auto-repeat the build.
      await api.dispatch(req);
      req.stage="dispatched";req.message="Požadavek odeslán. Hledám správný běh podle UUID.";
      persist();renderBuilds();
      note("Kompilace odeslána. Průběh bude dostupný i po obnovení stránky.","success");
      await refreshBuild(req);
    } catch(err) {
      req.stage=req.source_sha?"submission_uncertain":"snapshot_failed";
      req.message=errorMessage(err)+(req.source_sha?" — znovu nespouštěj naslepo, nejprve zkontroluj Actions.":"");
      persist();renderBuilds();throw err;
    }
  } catch(err){note(errorMessage(err),"error");}
  finally {busy=false;byId("btn-build").disabled=false;}
}
async function refreshBuild(item) {
  if (!api) return note("Znovu připoj GitHub token po obnovení stránky.","error");
  if (!item.source_sha) return;
  try {
    const located=await api.findRun(item);
    if (located.state!=="found") {
      item.stage=located.state;item.message=located.state==="awaiting"?
        "GitHub zatím nezaindexoval odpovídající běh. Nezakládej nový.":
        "Identita běhu není jednoznačná. Žádný cizí artefakt se nezobrazí.";
      persist();renderBuilds();return;
    }
    const run=located.run;
    item.run_id=run.id;
    if (run.status!=="completed") {
      item.stage=run.status||"running";
      item.message="Čeká se na dokončení GitHub Actions.";
      persist();renderBuilds();return;
    }
    const artifacts=await api.artifacts(run.id);
    const result=reconcileArtifacts(artifacts,item,run);
    item.stage=result.state;item.artifacts=result.artifacts;
    item.message=result.state==="success"?
      "Ověřeny metadata běhu, UUID, SHA a názvy artefaktů. Výstup je NEPODEPSANÝ; instalace vyžaduje podpis.":
      result.state==="failed"?"Build skončil chybou. Otevři GitHub Actions a projdi logy.":
      "Nesoulad či chybějící artefakty. Výsledek nelze bezpečně přiřadit.";
    persist();renderBuilds();
  } catch(err){item.message=errorMessage(err);persist();renderBuilds();}
}
async function refreshAll() {
  if (polling||!api) return;
  polling=true;
  try {
    for (const item of state.builds.slice(0,8)) {
      if (item.source_sha && !["success","failed","missing","mismatch","ambiguous"].includes(item.stage))
        await refreshBuild(item);
    }
  } finally {polling=false;}
}
function exportProject() {
  try{
    saveEditing();validateProject(state.project);
    const url=URL.createObjectURL(new Blob([JSON.stringify(state.project,null,2)],{type:"application/json"}));
    const a=htmlEl("a");a.href=url;a.download="xcode-pocket-project.json";a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);note("Projekt exportován.","success");
  }catch(err){note(errorMessage(err),"error");}
}
async function importProject(file) {
  try {
    if (!file || file.size>4_000_000) throw Error("Import podporuje JSON do 4 MB.");
    const result=JSON.parse(await file.text());
    validateProject(result);
    // Only replace after full validation.
    state.project=result;state.active=Object.keys(result.files)[0];
    persist();renderFiles();note("Projekt importován bez ztráty historie buildů.","success");
  }catch(err){note(errorMessage(err),"error");}
}
function addFile() {
  const name=prompt("Nový soubor, např. Sources/Model.swift");
  if (!name) return;
  if (!validPath(name)||Object.keys(state.project.files).some(x=>x.toLowerCase()===name.toLowerCase())) return note("Neplatný nebo duplicitní soubor.","error");
  const old=state.active;saveEditing();state.project.files[name]="";
  try{validateProject(state.project);}catch(err){delete state.project.files[name];state.active=old;return note(errorMessage(err),"error");}
  state.active=name;persist();renderFiles();
}
function deleteFile() {
  const name=state.active;if(!name||!confirm("Odstranit "+name+" z lokálního projektu?")) return;
  const old=state.project.files[name];delete state.project.files[name];
  try{validateProject(state.project);}catch(err){state.project.files[name]=old;return note(errorMessage(err),"error");}
  state.active=Object.keys(state.project.files)[0];persist();renderFiles();
}
function init() {
  byId("repo").value=state.settings.repo;
  byId("app-name").value=state.settings.appName;
  byId("bundle-id").value=state.settings.bundleId;
  renderFiles();renderBuilds();
  byId("files").addEventListener("change",e=>{
    saveEditing();state.active=e.target.value;persist();renderFiles();
  });
  byId("code").addEventListener("input",saveEditing);
  byId("check-access").addEventListener("click",()=>void connect());
  byId("btn-build").addEventListener("click",()=>void submit());
  byId("btn-refresh").addEventListener("click",()=>void refreshAll());
  byId("btn-add").addEventListener("click",addFile);
  byId("btn-remove").addEventListener("click",deleteFile);
  byId("btn-export").addEventListener("click",exportProject);
  byId("btn-import").addEventListener("click",()=>byId("input-import").click());
  byId("input-import").addEventListener("change",e=>{
    void importProject(e.target.files[0]);e.target.value="";
  });
  for (const id of ["repo","app-name","bundle-id"]) byId(id).addEventListener("change",()=>{
    try{syncSettings();}catch(err){note(errorMessage(err),"error");}
  });
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)void refreshAll();});
  setInterval(()=>{if (!document.hidden)void refreshAll();},12000);
  if("serviceWorker" in navigator) navigator.serviceWorker.register("./sw.js").catch(()=>{});
  note("Lokální editor připraven. GitHub token není uložený na zařízení.");
}
init();
