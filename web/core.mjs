// Xcode Pocket: browser protocol shared by the UI and Node tests.
export const REPOSITORY = "HappyMistake64/Xcode-for-iphone";
export const WORKFLOW = ".github/workflows/pocket-ios.yml";
export const STORAGE_KEY = "xcode-pocket-standalone-v1";
export const SHA_RE = /^[a-f0-9]{40}$/;
export const UUID_RE = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;

export function newProject() {
  return {format: "xcode-pocket-project", version: 2, files: {
    "PocketApp.swift": "import SwiftUI\n\n@main\nstruct PocketApp: App {\n    var body: some Scene {\n        WindowGroup { ContentView() }\n    }\n}\n",
    "ContentView.swift": "import SwiftUI\n\nstruct ContentView: View {\n    @State private var count = 0\n    var body: some View {\n        VStack(spacing: 24) {\n            Text(\"Ahoj z iPhonu\").font(.largeTitle)\n            Button(\"Klepnutí: \\(count)\") { count += 1 }\n                .buttonStyle(.borderedProminent)\n        }.padding()\n    }\n}\n"
  }};
}

export function validPath(name) {
  return typeof name === "string" && !!name && name.length <= 240 && name === name.normalize("NFC") &&
    !/[\\\u0000-\u001f\u007f]/.test(name) && name.split("/").every(p =>
      !!p && p === p.trim() && ![".", "..", "__proto__", "constructor", "prototype"].includes(p));
}

export function validateProject(p) {
  if (!p || p.format !== "xcode-pocket-project" || ![1, 2].includes(p.version) ||
      !p.files || Array.isArray(p.files) || typeof p.files !== "object") throw Error("Neplatný projekt.");
  const files = Object.entries(p.files);
  if (files.length < 1 || files.length > 200) throw Error("Projekt musí mít 1–200 souborů.");
  const names = new Set();
  for (const [name, body] of files) {
    if (!validPath(name) || typeof body !== "string" || names.has(name.toLowerCase())) throw Error("Neplatný soubor: " + name);
    names.add(name.toLowerCase());
    if (name.startsWith("Resources/") && !/\.(json|txt|csv|plist|xml|md|html|css|js)$/i.test(name)) throw Error("Nepodporovaný Resource: " + name);
  }
  for (const key of names) {
    const bits = key.split("/");
    for (let i = 1; i < bits.length; i++) if (names.has(bits.slice(0,i).join("/"))) throw Error("Konflikt soubor/složka.");
  }
  if (!files.some(([name]) => name.endsWith(".swift") && !name.startsWith("Tests/"))) throw Error("Chybí hlavní Swift soubor.");
  if (new TextEncoder().encode(JSON.stringify(p)).length > 4_000_000) throw Error("Projekt je větší než 4 MB.");
  return p;
}

export function validateSettings(s) {
  const repo = s.repo || REPOSITORY, appName = (s.appName || "PocketApp").trim(), bundleId = s.bundleId || "cz.davidvolf.pocketapp";
  if (!/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\/[A-Za-z0-9_.-]+$/.test(repo)) throw Error("Repozitář musí být owner/repo.");
  if (!/^[A-Za-z][A-Za-z0-9 ._-]{0,59}$/.test(appName)) throw Error("Neplatný název aplikace.");
  if (!/^[A-Za-z][A-Za-z0-9-]*(\.[A-Za-z][A-Za-z0-9-]*){2,}$/.test(bundleId)) throw Error("Neplatné Bundle ID.");
  return {repo,appName,bundleId};
}

export function makeRequest(s, project, id = crypto.randomUUID()) {
  validateProject(project);
  const settings = validateSettings(s);
  if (!UUID_RE.test(id)) throw Error("Neplatné UUID.");
  return {...settings, request_id:id, project_path:"pocket-builds/" + id + ".json",
    source_sha:null, run_id:null, stage:"prepared", created_at:new Date().toISOString()};
}

export function assertIdentity(r) {
  if (!r || !UUID_RE.test(r.request_id) || !SHA_RE.test(r.source_sha) ||
      r.project_path !== "pocket-builds/" + r.request_id + ".json") throw Error("Neúplná identita buildu.");
  validateSettings(r);
  return r;
}

export function utf8Base64(source) {
  const bytes = new TextEncoder().encode(source);
  let binary = "";
  for (let i=0; i<bytes.length; i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192));
  return btoa(binary);
}

export function matchesRun(run,r) {
  return !!run && Number.isSafeInteger(run.id) && run.id>0 &&
    run.display_title==="Pocket iOS " + r.request_id &&
    run.event==="workflow_dispatch" && String(run.path||"").split("@")[0]===WORKFLOW &&
    run.repository?.full_name===r.repo &&
    (!run.head_repository || run.head_repository.full_name===r.repo);
}

export function pickRun(runs,r,expectedId=null) {
  const matching=runs.filter(x=>matchesRun(x,r));
  if (expectedId!==null) return {state: matching.some(x=>x.id===expectedId)?"found":"mismatch",
    run:matching.find(x=>x.id===expectedId)||null};
  if (matching.length===0) return {state:"awaiting",run:null};
  if (matching.length>1) return {state:"ambiguous",run:null};
  return {state:"found",run:matching[0]};
}

export function reconcileArtifacts(artifacts,r,run) {
  assertIdentity(r);
  if (!matchesRun(run,r)) return {state:"mismatch",artifacts:[]};
  if (run.status!=="completed") return {state:"running",artifacts:[]};
  const names={app:"pocket-ios-app-"+r.request_id,logs:"pocket-ios-logs-"+r.request_id,
    identity:"pocket-ios-identity-"+r.request_id+"-"+r.source_sha};
  const found={};
  for (const [kind,name] of Object.entries(names)) {
    const options=artifacts.filter(a=>a && a.name===name && a.expired!==true &&
      (!a.workflow_run || a.workflow_run.id===run.id) && Number.isSafeInteger(a.id));
    if (options.length>1) return {state:"ambiguous",artifacts:[]};
    found[kind]=options[0]||null;
  }
  if (run.conclusion!=="success") return {state:"failed",artifacts:[found.logs,found.identity].filter(Boolean)};
  if (!found.identity||!found.app) return {state:"missing",artifacts:[]};
  return {state:"success",artifacts:[found.app,found.logs,found.identity].filter(Boolean)};
}

export class GithubAPI {
  constructor(token,repo=REPOSITORY,fetcher=fetch) {
    if (!token || !token.trim()) throw Error("Vlož GitHub token (Contents a Actions).");
    this.token=token.trim(); this.repo=validateSettings({repo}).repo; this.fetcher=fetcher;
  }
  async request(method,path,body) {
    const response=await this.fetcher("https://api.github.com/repos/"+this.repo+path,{
      method,cache:"no-store",
      headers:{"Authorization":"Bearer "+this.token,"Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",...(body===undefined?{}:{"Content-Type":"application/json"})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})
    });
    if (!response.ok) throw Error("GitHub HTTP "+response.status+": "+(await response.text().catch(()=>"")).slice(0,230));
    return response.status===204?null:response.json();
  }
  async check(){return this.request("GET","");}
  async verifyAccess(){
    const repository=await this.check();
    if (String(repository?.full_name||"").toLowerCase()!==this.repo.toLowerCase())
      throw Error("GitHub vrátil jiný repozitář.");
    if (repository?.permissions?.push===false)
      throw Error("Token má k repozitáři jen přístup pro čtení. Povol Contents: Read/Write.");
    return repository;
  }
  async snapshot(r,p){
    if (r.repo!==this.repo) throw Error("Repozitář nesouhlasí.");
    const result=await this.request("PUT","/contents/"+r.project_path,{
      message:"build: "+r.request_id,content:utf8Base64(JSON.stringify(validateProject(p))),branch:"main"});
    if (!SHA_RE.test(result?.commit?.sha)) throw Error("GitHub nevrátil commit SHA.");
    return result.commit.sha;
  }
  async dispatch(r) {
    assertIdentity(r);
    if (r.repo!==this.repo) throw Error("Repozitář nesouhlasí.");
    return this.request("POST","/actions/workflows/pocket-ios.yml/dispatches",{
      ref:"main",inputs:{source_sha:r.source_sha,request_id:r.request_id,
        project_path:r.project_path,app_name:r.appName,bundle_id:r.bundleId}});
  }
  async getRun(id){return this.request("GET","/actions/runs/"+Number(id));}
  async findRun(r) {
    if (r.run_id) return pickRun([await this.getRun(r.run_id)],r,r.run_id);
    const matches=[];
    for (let page=1; page<=5;page++) {
      const result=await this.request("GET","/actions/workflows/pocket-ios.yml/runs?event=workflow_dispatch&per_page=100&page="+page);
      const runs=result.workflow_runs||[];
      matches.push(...runs.filter(x=>matchesRun(x,r)));
      if (matches.length>1 || runs.length<100) break;
    }
    return pickRun(matches,r);
  }
  async artifacts(id){
    const result=[];
    for (let page=1;page<=5;page++){
      const answer=await this.request("GET","/actions/runs/"+Number(id)+"/artifacts?per_page=100&page="+page);
      const values=answer.artifacts||[]; result.push(...values);
      if (values.length<100) break;
    }
    return result;
  }
}

export function restoreState(raw){
  try {
    const p=JSON.parse(raw);
    return {project:validateProject(p.project),settings:validateSettings(p.settings||{}),
      builds:Array.isArray(p.builds)?p.builds.filter(x=>x&&UUID_RE.test(x.request_id)).slice(0,30):[],
      active:typeof p.active==="string"?p.active:"PocketApp.swift"};
  } catch{return {project:newProject(),settings:validateSettings({}),builds:[],active:"PocketApp.swift"};}
}
