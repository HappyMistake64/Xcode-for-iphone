import {test} from "node:test";
import assert from "node:assert/strict";
import {REPOSITORY,WORKFLOW,newProject,validateProject,validPath,makeRequest,
  validateSettings,assertIdentity,utf8Base64,matchesRun,pickRun,reconcileArtifacts,
  restoreState,GithubAPI} from "../web/core.mjs";

const UUID_A="a7612db1-a3fc-4e61-963c-cc11fedf4d20";
const UUID_B="b8612db1-a3fc-4e61-963c-cc11fedf4d20";
const UUID_C="c9612db1-a3fc-4e61-963c-cc11fedf4d20";
const SHA_A="a".repeat(40),SHA_B="b".repeat(40),SHA_C="c".repeat(40);
function identity(uuid=UUID_A,sha=SHA_A) {
  return {...makeRequest({repo:REPOSITORY},newProject(),uuid),source_sha:sha};
}
function run(r,id=21,changes={}) {
  return {id,event:"workflow_dispatch",display_title:"Pocket iOS "+r.request_id,
    path:WORKFLOW+"@refs/heads/main",repository:{full_name:REPOSITORY},
    head_repository:{full_name:REPOSITORY},status:"completed",conclusion:"success",...changes};
}
function artifact(r,kind,id=1,changes={}) {
  const name=kind==="identity"?"pocket-ios-identity-"+r.request_id+"-"+r.source_sha:
    "pocket-ios-"+kind+"-"+r.request_id;
  return {name,id,expired:false,workflow_run:{id:21},...changes};
}
test("default SwiftUI project is valid",()=>{
  const project=validateProject(newProject());
  assert.ok(project.files["PocketApp.swift"].includes("@main"));
  assert.ok(project.files["ContentView.swift"].includes("count"));
});
test("validates paths and prevents collisions",()=>{
  for (const bad of ["../secret","hello\\world","__proto__/x.swift","a//b.swift","x\n.swift"]) assert.equal(validPath(bad),false);
  const project=newProject();project.files["contentview.swift"]="let a=1";
  assert.throws(()=>validateProject(project),/soubor/i);
});
test("rejects unsafe resources and file folder conflicts",()=>{
  const project=newProject();project.files["Resources/pic.png"]="png";
  assert.throws(()=>validateProject(project),/Resource/);
  delete project.files["Resources/pic.png"];
  project.files["Foo"]="hello";project.files["Foo/Child.swift"]="let a=1";
  assert.throws(()=>validateProject(project),/Konflikt/);
});
test("supports Unicode text in Github API base64",()=>{
  assert.equal(atob(utf8Base64("Ahoj žluťoučký svět")).length>0,true);
  const decoded=new TextDecoder().decode(Uint8Array.from(atob(utf8Base64("Příliš")),(c)=>c.charCodeAt(0)));
  assert.equal(decoded,"Příliš");
});
test("creates canonical immutable request",()=>{
  const r=identity();assert.equal(r.project_path,"pocket-builds/"+UUID_A+".json");
  assert.equal(assertIdentity(r),r);
  assert.throws(()=>assertIdentity({...r,source_sha:"nope"}));
});
test("settings reject invalid bundle and repository",()=>{
  assert.throws(()=>validateSettings({bundleId:"bad"}));
  assert.throws(()=>validateSettings({repo:"./evil"}));
});
test("matches only correct workflow, title, event and repo",()=>{
  const r=identity();
  assert.equal(matchesRun(run(r),r),true);
  for (const change of [
    {path:".github/workflows/elsewhere.yml"},
    {event:"push"},
    {repository:{full_name:"other/repo"}},
    {display_title:"Pocket iOS "+UUID_B}
  ]) assert.equal(matchesRun(run(r,21,change),r),false);
});
test("three parallel builds cannot get swapped",()=>{
  const ids=[identity(UUID_A,SHA_A),identity(UUID_B,SHA_B),identity(UUID_C,SHA_C)];
  const runs=ids.map((r,i)=>run(r,21+i));
  ids.forEach((r,i)=>{
    const response=pickRun(runs,r);
    assert.equal(response.state,"found");
    assert.equal(response.run.id,21+i);
  });
});
test("multiple runs sharing request ID are ambiguous",()=>{
  const r=identity();assert.equal(pickRun([run(r,21),run(r,22)],r).state,"ambiguous");
  assert.equal(pickRun([run(r,21),run(r,22)],r,22).run.id,22);
  assert.equal(pickRun([run(r,21)],r,999).state,"mismatch");
});
test("artifact binding needs exact SHA-qualified identity in same run",()=>{
  const r=identity(),build=run(r),base=[artifact(r,"app"),artifact(r,"logs",2)];
  assert.equal(reconcileArtifacts(base,r,build).state,"missing");
  assert.equal(reconcileArtifacts([...base,artifact(r,"identity",3)],r,build).state,"success");
});
test("wrong SHA and expired artifacts fail closed",()=>{
  const r=identity(),build=run(r);
  assert.equal(reconcileArtifacts([artifact(r,"app"),artifact(r,"identity",3,{name:"pocket-ios-identity-"+UUID_A+"-"+SHA_B})],r,build).state,"missing");
  assert.equal(reconcileArtifacts([artifact(r,"app"),artifact(r,"identity",3,{expired:true})],r,build).state,"missing");
});
test("duplicate artifacts and mismatched run fail closed",()=>{
  const r=identity();
  assert.equal(reconcileArtifacts([artifact(r,"app",1),artifact(r,"app",8),artifact(r,"identity",3)],r,run(r)).state,"ambiguous");
  assert.equal(reconcileArtifacts([],r,run(r,21,{event:"push"})).state,"mismatch");
});
test("running and failed builds never expose unsigned app",()=>{
  const r=identity();
  assert.equal(reconcileArtifacts([artifact(r,"app")],r,run(r,21,{status:"in_progress"})).state,"running");
  assert.equal(reconcileArtifacts([artifact(r,"app"),artifact(r,"logs",2)],r,run(r,21,{conclusion:"failure"})).state,"failed");
});
test("restores local snapshot and build state without credentials",()=>{
  const state={project:newProject(),settings:validateSettings({}),builds:[identity()],active:"PocketApp.swift"};
  const restored=restoreState(JSON.stringify(state));
  assert.equal(restored.builds[0].request_id,UUID_A);
  assert.equal(restored.project.files["PocketApp.swift"],state.project.files["PocketApp.swift"]);
  assert.deepEqual(restoreState("bad").builds,[]);
});
test("API sends correct snapshot and dispatch with exact SHA",async()=>{
  const sent=[];
  const mocked=async (url,opts)=>{
    sent.push({url,opts});
    if(url.includes("/contents/"))return {ok:true,status:201,json:async()=>({commit:{sha:SHA_A}})};
    if(url.endsWith("/dispatches"))return {ok:true,status:204};
    return {ok:true,status:200,json:async()=>({full_name:REPOSITORY})};
  };
  const client=new GithubAPI("test-token",REPOSITORY,mocked);
  const req=identity();
  assert.equal(await client.snapshot(req,newProject()),SHA_A);
  await client.dispatch(req);
  assert.equal(sent.length,2);
  const snapshot=JSON.parse(sent[0].opts.body);
  assert.equal(snapshot.branch,"main");
  assert.ok(snapshot.content.length>100);
  const dispatch=JSON.parse(sent[1].opts.body);
  assert.equal(dispatch.inputs.source_sha,SHA_A);
  assert.equal(dispatch.inputs.request_id,UUID_A);
  assert.ok(!JSON.stringify(sent).includes("localStorage"));
});
test("API uses exact saved run ID and finds unambiguous run",async()=>{
  const r=identity();
  const mock=async (url)=>{
    if (url.includes("/runs/88"))return {ok:true,status:200,json:async()=>run(r,88)};
    if (url.includes("/runs?"))return {ok:true,status:200,json:async()=>({workflow_runs:[run(r,88)]})};
    throw Error("Unexpected "+url);
  };
  const api=new GithubAPI("mock",REPOSITORY,mock);
  assert.equal((await api.findRun(r)).run.id,88);
  assert.equal((await api.findRun({...r,run_id:88})).run.id,88);
});

test("token verification accepts writable GitHub repository",async()=>{
  const api=new GithubAPI("test-token",REPOSITORY,async()=>{
    return {ok:true,status:200,json:async()=>({full_name:REPOSITORY,permissions:{push:true}})};
  });
  assert.equal((await api.verifyAccess()).full_name,REPOSITORY);
});
test("token verification rejects read-only repository access",async()=>{
  const api=new GithubAPI("read-only",REPOSITORY,async()=>({
    ok:true,status:200,json:async()=>({full_name:REPOSITORY,permissions:{push:false}})
  }));
  await assert.rejects(api.verifyAccess(),/jen přístup pro čtení/);
});
test("token verification reports rejected credentials",async()=>{
  const api=new GithubAPI("bad-token",REPOSITORY,async()=>({
    ok:false,status:401,text:async()=>"Bad credentials"
  }));
  await assert.rejects(api.verifyAccess(),/GitHub HTTP 401/);
});
test("token verification rejects repository identity mismatch",async()=>{
  const api=new GithubAPI("test-token",REPOSITORY,async()=>({
    ok:true,status:200,json:async()=>({full_name:"other/repo",permissions:{push:true}})
  }));
  await assert.rejects(api.verifyAccess(),/jiný repozitář/);
});
test("mobile login feedback is next to the token field",async()=>{
  const {readFile}=await import("node:fs/promises");
  const html=await readFile(new URL("../web/index.html",import.meta.url),"utf8");
  const app=await readFile(new URL("../web/app.mjs",import.meta.url),"utf8");
  assert.match(html,/id="connection-status"/);
  assert.ok(html.indexOf('id="connection-status"')>html.indexOf('id="connect-token"'));
  assert.ok(html.indexOf('id="connection-status"')<html.indexOf('class="panel workspace"'));
  assert.match(app,/connectionNote\("❌ "/);
  assert.match(app,/byId\("connect-token"\)\.addEventListener\("keydown"/);
});

test("HTTP errors do not pretend the dispatch succeeded",async()=>{
  const api=new GithubAPI("mock",REPOSITORY,async()=>({ok:false,status:403,text:async()=>"Missing Actions permission"}));
  await assert.rejects(api.dispatch(identity()),/403/);
});
