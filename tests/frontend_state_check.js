/* Pure state regression checks: executes real functions, no browser or network. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../frontend/index.html'), 'utf8');
function extract(name) {
  const pattern = new RegExp('^(?:async )?function ' + name + '\\(', 'm');
  const found = pattern.exec(html);
  assert.ok(found, 'Function missing: ' + name);
  const rest = html.slice(found.index);
  const next = /\n(?:async )?function /g; next.lastIndex = 1;
  const boundary = next.exec(rest);
  return boundary ? rest.slice(0, boundary.index) : rest;
}
const fields = new Map();
const state = {token:'cookie',tripId:2,user:{id:1},currentTrip:{input_version:2},participants:['original'],
               destinations:['original'],planningFormDirty:true,participantFormDirty:true,itinerary:null};
let response = async () => ({input_version:7,participants:['updated'],destinations:['updated']});
const sandbox = {
  state, console, document:{getElementById(id){
    if (!fields.has(id)) fields.set(id,{value:'draft',checked:false,textContent:'',style:{}});
    return fields.get(id);
  }},
  api:(...args)=>response(...args), toast(){}, renderDestinations(){},renderParticipants(){},
  refreshRouteComparison(){},itineraryConflicts:()=>[],drawAll(){},hydrateMyParticipantForm(){throw Error('Dirty form overwritten');},
  toDateTimeLocal:value=>value||'',readReturnLocation:()=>null,renderItinerary(){},
};
vm.createContext(sandbox);
for(const name of ['acceptTripVersion','refreshDestinations','refreshParticipants','fetchPlanningSettings','savePlanningSettings','refreshMyTrips','importSelections','generateItinerary'])
  vm.runInContext(extract(name),sandbox);
async function main(){
  assert.equal(sandbox.acceptTripVersion(2,1,7),true);
  assert.equal(state.currentTrip.input_version,7);
  sandbox.acceptTripVersion(2,1,3);
  assert.equal(state.currentTrip.input_version,7,'Late reads cannot lower version');
  assert.equal(sandbox.acceptTripVersion(3,1,9),false);
  assert.equal(sandbox.acceptTripVersion(2,99,9),false);

  await sandbox.fetchPlanningSettings();
  assert.equal(fields.get('planStart'),undefined,'Dirty planning form stays untouched');

  let resolve;
  response=()=>new Promise(done=>resolve=done);
  const pending=sandbox.refreshDestinations();
  state.tripId=3;
  resolve({input_version:99,destinations:['from previous trip']});
  await pending;
  assert.deepEqual(state.destinations,['original']);
  assert.equal(state.currentTrip.input_version,7);

  state.tripId=2;
  const participantRead=sandbox.refreshParticipants();
  state.user={id:2};
  resolve({input_version:99,participants:['from previous account']});
  await participantRead;
  assert.deepEqual(state.participants,['original']);

  state.user={id:1};
  fields.set('planStart',{value:'2026-11-01T13:00'});
  fields.set('planEnd',{value:'2026-11-01T18:00'});
  response=async()=>({input_version:8});
  assert.equal(await sandbox.savePlanningSettings(),true);
  assert.equal(state.currentTrip.input_version,8,'Save updates version immediately');

  state.planningFormDirty=true;
  response=()=>new Promise(done=>resolve=done);
  const save=sandbox.savePlanningSettings();
  state.user={id:2};
  resolve({input_version:99});
  assert.equal(await save,false);
  assert.equal(state.planningFormDirty,true,'Late save does not clear another account form');

  state.user={id:1};
  const imported=sandbox.importSelections([],8);
  state.tripId=3;
  resolve({input_version:99});
  await imported;
  assert.equal(state.currentTrip.input_version,8,'Late import does not change current trip');

  state.tripId=2;
  fields.set('myTripSelect',{innerHTML:'new account trips'});
  const trips=sandbox.refreshMyTrips();
  state.user={id:2};
  resolve({trips:[{title:'previous account secret'}]});
  await trips;
  assert.equal(fields.get('myTripSelect').innerHTML,'new account trips');
  state.user={id:1};state.tripId=2;state.currentTrip={my_role:'member',input_version:8};
  const calls=[];
  response=async(path,method)=>{calls.push([path,method]);return {input_version:8,plan_id:5};};
  await sandbox.generateItinerary();
  assert.deepEqual(calls,[['/trips/2/itinerary-plans','POST']],'Member generation must not save creator-only settings');
  assert.equal(state.itinerary.plan_id,5);
  Object.assign(sandbox,{
    clearTripState(){state.tripId=null;state.currentTrip=null;},renderTripInfo(){},updateFlowUI(){},
    refreshParticipants:async()=>{},refreshDestinations:async()=>{},fetchPlanningSettings:async()=>{},
    fetchLatestItinerary:async()=>{},connectWS(){},
  });
  vm.runInContext('let tripOpenRequest=0;',sandbox);
  vm.runInContext(extract('openTrip'),sandbox);
  let first,second;
  response=path=>new Promise(done=>{if(path==='/trips/2')first=done;else second=done;});
  const oldOpen=sandbox.openTrip(2).catch(error=>error);
  const latestOpen=sandbox.openTrip(3);
  second({trip_id:3,primary_workflow:'itinerary',status:'active'});
  await latestOpen;
  first({trip_id:2,primary_workflow:'itinerary',status:'active'});
  await oldOpen;
  assert.equal(state.tripId,3,'A late earlier click must not replace the latest selected trip');
  vm.runInContext('let itineraryConfirmationBusy=false;',sandbox);
  vm.runInContext(extract('confirmItinerary'),sandbox);
  let acceptedOptions;
  Object.assign(sandbox,{renderItinerary(){},refreshMyTrips:async()=>{}});
  state.tripId=3;state.user={id:1};
  state.currentTrip={title:'review',status:'active',input_version:10};
  state.itinerary={plan_id:4,input_version:10,is_stale:false,ordered_stops:[{name:'A'}],
                   participant_arrivals:[{estimated:true}],route_legs:[]};
  const confirmationCalls=[];
  response=async(path,method,body)=>{confirmationCalls.push([path,body]);return {...state.itinerary,status:'confirmed'};};
  sandbox.confirmJournalAction=async options=>{acceptedOptions=options;return false;};
  await sandbox.confirmItinerary(4);
  assert.equal(confirmationCalls.length,0,'Cancel must never post a confirmation');
  assert.ok(acceptedOptions.acknowledgement,'Estimated routes need explicit acknowledgement');

  sandbox.confirmJournalAction=async()=>{state.itinerary.is_stale=true;return true;};
  await sandbox.confirmItinerary(4);
  assert.equal(confirmationCalls.length,0,'A route changed while the dialog is open must not be confirmed');

  state.itinerary.is_stale=false;
  sandbox.confirmJournalAction=async()=>true;
  await sandbox.confirmItinerary(4);
  assert.equal(confirmationCalls.length,1);
  assert.equal(confirmationCalls[0][1].accept_estimated_routes,true);
  assert.equal(state.currentTrip.status,'confirmed');

  state.currentTrip.status='active';state.itinerary.status='draft';
  sandbox.confirmJournalAction=async()=>{state.user={id:2};return true;};
  await sandbox.confirmItinerary(4);
  assert.equal(confirmationCalls.length,1,'Account switch cancels the pending confirmation');
  vm.runInContext(extract('applyTripEditability'),sandbox);
  const editableGroups = new Map();
  sandbox.document.querySelectorAll=selector=>{
    if(!editableGroups.has(selector))editableGroups.set(selector,[{disabled:false}]);
    return editableGroups.get(selector);
  };
  state.currentTrip={status:'completed',primary_workflow:'itinerary',my_role:'creator'};
  state.workflowMode='route';
  sandbox.applyTripEditability();
  assert.ok([...editableGroups.values()].every(controls=>controls[0].disabled),'Completed planning controls are read-only');
  state.currentTrip.status='active';
  sandbox.applyTripEditability();
  assert.ok([...editableGroups.values()].every(controls=>!controls[0].disabled),'Reopened creator controls become editable');
  state.currentTrip.my_role='member';
  sandbox.applyTripEditability();
  for(const [selector,controls] of editableGroups)
    assert.equal(controls[0].disabled,selector.includes('#routeWorkflow'),'Members keep own start and place actions, but cannot edit shared settings');
  for(const name of ['escapeHtml','formatTime','itineraryConflicts','renderConflictReview','refreshRouteComparison','renderRouteComparison','clearPasswordFields'])vm.runInContext(extract(name),sandbox);
  state.currentTrip={status:'active',my_role:'creator'};
  const unsafeName='<img src=x onerror=alert(1)>';
  const reviewPlan={plan_id:88,warnings:[{code:'destination_time_window_conflict',destination_id:7,message:unsafeName,late_min:12},{code:'estimated_routes',message:'estimate'}]};
  assert.equal(sandbox.itineraryConflicts(reviewPlan).length,1,'Estimates are distinct from time conflicts');
  const reviewHtml=sandbox.renderConflictReview(reviewPlan);
  assert.ok(reviewHtml.includes('12 分钟'));
  assert.ok(!reviewHtml.includes(unsafeName),'Conflict messages are HTML escaped');
  assert.ok(reviewHtml.includes('调整该地点'));
  state.currentTrip.status='completed';
  assert.ok(!sandbox.renderConflictReview(reviewPlan).includes('onclick='),'Readonly plan has no editing shortcut');
  state.currentTrip.status='active';state.tripId=2;state.user={id:1};state.itinerary={plan_id:88};
  const originalComparison={planId:88,available:false};state.routeComparison=originalComparison;
  response=()=>new Promise(done=>resolve=done);
  const lateComparison=sandbox.refreshRouteComparison();
  state.user={id:2};resolve({available:true,changes:{current_plan_id:88}});await lateComparison;
  assert.equal(state.routeComparison,originalComparison,'Late comparison cannot overwrite another account');
  state.user={id:1};
  fields.set('recoveryCodeValue',{value:'temporary secret'});fields.set('recoveryCodeLabel',{hidden:false});
  sandbox.clearPasswordFields();
  assert.equal(fields.get('recoveryCodeValue').value,'');
  assert.equal(fields.get('recoveryCodeLabel').hidden,true,'Closing account hides and clears the once-only recovery secret');
  vm.runInContext(extract('renderDestinations'),sandbox);
  state.participants=[{user_id:1,role:'creator'}];state.user={id:1};state.currentTrip={status:'active'};
  state.destinations=[{id:7,name:'Appointment',visit_status:'must_visit',expected_stay_min:90,
    submitted_by:{user_id:1,name:'Owner'},visit_window_start:'2026-11-01T13:00:00+08:00',visit_window_end:'2026-11-01T14:00:00+08:00'}];
  state.feedbackByDestination={};state.editingStayId=7;
  fields.set('destinationList',{dataset:{},innerHTML:'',querySelectorAll:()=>[]});
  sandbox.renderDestinations();
  assert.ok(fields.get('destinationList').innerHTML.includes('window-start-7'),'A saved appointment opens the editable time-window form');
  assert.ok(fields.get('destinationList').innerHTML.includes('保存时间约束'));
  vm.runInContext(extract('openConflictEditor'),sandbox);
  sandbox.journalUI={mobileMap:true};sandbox.selectFeature=()=>{};
  let selectedStep;
  sandbox.setJournalStep=step=>{selectedStep=step;};
  state.currentTrip={status:'active',my_role:'creator'};
  fields.set('stay-7',{focus(){},scrollIntoView(){}});
  sandbox.openConflictEditor('place',7);
  assert.equal(selectedStep,1);
  assert.equal(sandbox.journalUI.mobileMap,false,'Conflict shortcut leaves the mobile map and opens the visible editor');
  console.log('Frontend state regressions passed (version, dirty forms, late reads/writes, account isolation).');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
