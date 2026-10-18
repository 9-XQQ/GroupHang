/* B1/B2 real frontend functions in a VM: no browser, fetch or business server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
function extract(name) {
  const start = new RegExp('^(?:async )?function ' + name + '\\(', 'm').exec(html);
  assert.ok(start, name);
  const rest = html.slice(start.index);
  const next = /\n(?:async )?function /g; next.lastIndex = 1;
  const boundary = next.exec(rest);
  return boundary ? rest.slice(0, boundary.index) : rest;
}
const fields = new Map();
const state = {tripId: 3, user: {id: 7}, parseSessionId: 'session', currentTrip: {input_version: 1},
  importedCandidates: [], commentAssignments: {}, commentSummary: null, llmUsage: null};
const messages = [], feedback = [], calls = [];
let response = async () => ({destination_ids: [42], input_version: 2, replayed: false});
const sandbox = {state, console, crypto: {randomUUID: () => '660d097c-c55f-45c9-bf56-80c079b042a4'},
  document: {getElementById(id) {
    if (!fields.has(id)) fields.set(id, {value: '', innerHTML: '', checked: false});
    return fields.get(id);
  }},
  api: async (...args) => {calls.push(args); return response(...args);},
  toast: (...args) => messages.push(args),
  recordParseFeedback: async (...args) => feedback.push(args),
  refreshDestinations: async () => {}, clearCommentAssignmentsForCandidate: () => {},
  effectivePlaceComments: () => null, updatePickedStatus: () => {},
  setPickedLocation: () => true, escapeHtml: value => String(value).replaceAll('<', '&lt;'),
};
vm.createContext(sandbox);
for (const name of ['acceptTripVersion', 'placeFeedbackSnapshot', 'renderSharedTextResults',
  'importedDestinationNote', 'submitImportedConfirmation', 'addImportedDestination',
  'selectImportedLocation', 'copyImportedToManual', 'rejectImportedDestination', 'addDestination']) {
  vm.runInContext(extract(name), sandbox);
}
function item(name='演示公园') {
  return {name, lat: 31.2, lng: 121.4, address: '示例路1号', expected_stay_min: 60,
    parse_candidate_id: '660d097c-c55f-45c9-bf56-80c079b042a4', verification_status: 'matched',
    _proposedPlace: {}, location_candidates: []};
}
function reset(candidate) {
  state.tripId=3; state.user={id:7}; state.parseSessionId='session';
  state.currentTrip={input_version:1}; state.importedCandidates=[candidate];
  state.pendingImportedFeedback=null; calls.length=0; feedback.length=0;
}
async function main() {
  const statuses = {matched:'地图唯一匹配', ambiguous:'需要选择具体地点', not_found:'地图未找到',
    verification_failed:'地图核验失败', unverified:'尚未核验'};
  for (const [status,label] of Object.entries(statuses)) {
    const candidate = item(); candidate.verification_status=status;
    if (status !== 'matched') candidate.lat=candidate.lng=null;
    reset(candidate); sandbox.renderSharedTextResults();
    const rendered=fields.get('sharedTextResults').innerHTML;
    assert.ok(rendered.includes(label));
    assert.equal(rendered.includes('确认并加入地点'),status==='matched');
  }
  const candidate=item(); reset(candidate);
  let resolve;
  response=()=>new Promise(done=>resolve=done);
  const pending=sandbox.addImportedDestination(0);
  assert.equal(candidate._saving,true);
  assert.ok(fields.get('sharedTextResults').innerHTML.includes('disabled'));
  await sandbox.addImportedDestination(0);
  assert.equal(calls.length,1,'Double click is blocked before the first response');
  const unrelated=item('其他地点'); state.importedCandidates.unshift(unrelated);
  resolve({destination_ids:[42],input_version:2}); await pending;
  assert.deepEqual(state.importedCandidates,[unrelated],'Completion removes the candidate by identity, not old index');
  assert.ok(calls[0][0].endsWith('/places/import'));
  assert.equal(calls[0][2].places[0].confirmation.confirmed,true);

  const retry=item(); reset(retry);
  response=async()=>{throw Error('synthetic lost response');};
  await sandbox.addImportedDestination(0);
  assert.equal(retry._saving,false);
  const frozen=JSON.stringify(calls[0][2].places[0].confirmation);
  retry.name='edited after uncertain response';
  response=async()=>({destination_ids:[42],input_version:2,replayed:true});
  await sandbox.addImportedDestination(0);
  assert.equal(JSON.stringify(calls[1][2].places[0].confirmation),frozen,'Retry keeps original identity and payload');
  assert.equal(state.importedCandidates.length,0);

  const late=item(); reset(late); response=()=>new Promise(done=>resolve=done);
  const lateRequest=sandbox.addImportedDestination(0);
  state.user={id:99}; resolve({destination_ids:[42],input_version:9}); await lateRequest;
  assert.equal(feedback.length,0,'Late response cannot record feedback for another user');
  assert.equal(state.currentTrip.input_version,1);
  assert.equal(state.importedCandidates[0],late);

  const manual=item();
  Object.assign(manual, {_selectionMethod:'poi',poi_id:'stale',formatted_address:'old address',
    resolution_source:'poi_confirmed',verification_error_code:'old error',verification_complete:true});
  reset(manual); state.picked={lat:30,lng:120};
  sandbox.copyImportedToManual(0);
  assert.equal(state.picked,null,'Old map selection is cleared before manual editing');
  assert.equal(manual.verification_status,'unverified');
  assert.equal(manual.lat,null);
  assert.equal(manual.poi_id,null);
  assert.equal(manual.formatted_address,null);
  assert.equal(manual.resolution_source,null);
  assert.equal(manual.verification_error_code,null);
  assert.equal(manual.verification_complete,false);
  assert.equal(manual._selectionMethod,null);
  state.picked={lat:31.3,lng:121.5};
  fields.get('destinationName').value='Edited park';
  fields.get('destinationAddress').value='Edited address';
  fields.get('destinationStay').value='90';
  fields.get('destinationWindowStart').value='2026-11-01T13:00';
  fields.get('destinationWindowEnd').value='2026-11-01T14:00';
  response=async()=>({destination_ids:[42],input_version:2});
  await sandbox.addDestination();
  assert.ok(calls[0][0].endsWith('/places/import'));
  const confirmation=calls[0][2].places[0].confirmation;
  assert.equal(confirmation.selection_method,'manual');
  assert.equal(confirmation.destination.name,'Edited park');
  assert.ok(confirmation.destination.visit_window_start);
  assert.equal(state.pendingImportedFeedback,null);

  const ignored=item(); reset(ignored);
  await sandbox.rejectImportedDestination(0);
  assert.equal(calls.length,0,'Ignoring never invokes a persistence API');
  assert.equal(feedback[0][1],'rejected');

  const choice=item(); reset(choice);
  choice.lat=choice.lng=null; choice.verification_status='ambiguous';
  choice.location_candidates=[{name:'具体门店',address:'门店地址',lat:31.4,lng:121.6,city:'上海市',poi_id:'synthetic'}];
  sandbox.selectImportedLocation(0,0);
  assert.equal(choice._selectionMethod,'poi');
  assert.equal(choice.poi_id,'synthetic');
  assert.equal(calls.length,0,'Selecting a map candidate never saves automatically');
  assert.ok(fields.get('sharedTextResults').innerHTML.includes('确认并加入地点'));

  reset(item()); state.pendingImportedFeedback=null; state.picked={lat:31.2,lng:121.4};
  fields.get('destinationName').value='Manual'; fields.get('destinationStay').value='60';
  fields.get('destinationWindowStart').value=''; fields.get('destinationWindowEnd').value='';
  response=async()=>({id:43}); await sandbox.addDestination();
  assert.ok(calls[0][0].endsWith('/destinations'),'Ordinary manual add keeps the existing API');
  for (const status of [409,410,undefined]) {
    const failed=item(); reset(failed); messages.length=0;
    response=async()=>{const error=Error('synthetic conflict');error.status=status;throw error;};
    await sandbox.addImportedDestination(0);
    assert.equal(state.importedCandidates[0],failed);
    assert.equal(failed._saving,false);
    assert.ok(failed._confirmationRequest);
    assert.ok(messages[0][0].includes(status===410?'已被删除':status===409?'synthetic conflict':'请重试原确认'));
  }
  const stale=item(); reset(stale); messages.length=0;
  let reject;
  response=()=>new Promise((done,fail)=>reject=fail);
  const oldSession=sandbox.addImportedDestination(0);
  state.parseSessionId='new session';
  reject(Error('late failure')); await oldSession;
  assert.equal(messages.length,0,'Old parse-session failure cannot toast in the new session');
  const tripLate=item(); reset(tripLate); feedback.length=0;
  response=()=>new Promise(done=>resolve=done);
  const previousTrip=sandbox.addImportedDestination(0);
  state.tripId=4; resolve({destination_ids:[42],input_version:9}); await previousTrip;
  assert.equal(feedback.length,0);
  assert.equal(state.currentTrip.input_version,1);
  // A reload creates fresh JS state; no confirmation or preview is restored from storage.
  assert.match(html,/importedCandidates:\s*\[\]/);
  assert.match(html,/parseSessionId:\s*null/);
  assert.ok(!/localStorage\.(?:setItem|getItem)\([^\n]*(?:confirmationRequest|confirmationId)/.test(html));
  vm.runInContext(extract('parseSharedText'),sandbox);
  const applied=[];
  sandbox.applySharedTextResult=result=>applied.push(result);
  reset(item()); state.currentCity='上海市';
  for (const [id,value] of Object.entries({sharedText:'synthetic place',commentsText:'',importStay:'60',sourcePlatform:'generic'})) {
    sandbox.document.getElementById(id).value=value;
  }
  response=()=>new Promise(done=>resolve=done);
  const invalidated=sandbox.parseSharedText();
  state.parseRequestToken=null; // clearTripState invalidates the request, even if the same trip is reopened.
  resolve({tag:'old trip view'}); await invalidated;
  assert.equal(applied.length,0);
  const resolvers=[];
  response=()=>new Promise(done=>resolvers.push(done));
  const older=sandbox.parseSharedText(), newer=sandbox.parseSharedText();
  resolvers[1]({tag:'newest parse'}); await newer;
  resolvers[0]({tag:'outdated parse'}); await older;
  assert.equal(applied.length,1,'Only the newest live parse request can replace the preview');
  assert.equal(applied[0].tag,'newest parse');
  console.log('B1/B2 frontend checks passed: five states, double click, frozen retry, late response, manual editing, ignore, selection, legacy add.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
