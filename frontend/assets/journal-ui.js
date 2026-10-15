
/* Reviewable storybook shell. Existing business handlers and IDs remain in index.html. */
const journalUI = {steps:{route:0,meeting:0},lastUser:null,mobileMap:false,mounted:false,lastTrip:null};
const journalPages = {
  trips:['我的行程','下一段好时光，从这里开始。','YOUR TRAVEL JOURNAL'],
  saved:['收藏地点','把想去的地方，先放进口袋。','PLACES TO REMEMBER'],
  meeting:['共同约点','找到一个，对大家都刚刚好的地方。','MEET SOMEWHERE NICE'],
  route:['路线规划','把想去的地方，串成一段好时光。','A DAY WELL SPENT'],
  assistant:['行程助手','说说你的想法，我们一起安排。','A LITTLE HELP & INSPIRATION'],
  import:['灵感导入','收藏一段分享，发现下一站的灵感。','FROM INSPIRATION TO A JOURNEY']
};
let welcomeTransitionTimer;
function openWelcomeLogin(){
  const scene=document.getElementById('welcomeScene'),panel=document.getElementById('welcomeLoginPanel');
  if(scene.classList.contains('is-opening'))return;
  if(scene.classList.contains('is-open')){document.getElementById('username')?.focus({preventScroll:true});return;}
  document.getElementById('cloudEntry').setAttribute('aria-expanded','true');
  scene.classList.add('is-opening');
  document.querySelector('.welcome-clouds').inert=true;
  const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  welcomeTransitionTimer=setTimeout(()=>{
    panel.hidden=false;
    scene.classList.add('is-open');
    scene.classList.remove('is-opening');
    document.getElementById('username')?.focus({preventScroll:true});
  },reduced?0:650);
}
function closeWelcomeLogin(){
  clearPasswordFields();
  clearTimeout(welcomeTransitionTimer);
  document.getElementById('welcomeLoginPanel').hidden=true;
  document.getElementById('welcomeScene').classList.remove('is-open','is-opening');
  document.querySelector('.welcome-clouds').inert=false;
  document.getElementById('cloudEntry').setAttribute('aria-expanded','false');
  document.getElementById('cloudEntry').focus({preventScroll:true});
}
let journalConfirmation = null;
function cancelJournalConfirmation(){
  const current=journalConfirmation;
  if(!current)return;
  journalConfirmation=null;
  if(current.dialog.open)current.dialog.close();
  current.resolve(false);
}
function confirmJournalAction({title,message,confirmLabel='确认',acknowledgement=''}){
  if(journalConfirmation)return Promise.resolve(false);
  let dialog=document.getElementById('journalConfirmDialog');
  if(!dialog){
    dialog=document.createElement('dialog');
    dialog.id='journalConfirmDialog';dialog.className='journal-dialog';
    dialog.setAttribute('aria-labelledby','journalConfirmTitle');
    dialog.setAttribute('aria-describedby','journalConfirmMessage');
    dialog.innerHTML='<h2 id="journalConfirmTitle"></h2><p id="journalConfirmMessage" style="white-space:pre-line"></p><label id="journalConfirmAcknowledgement" class="confirmation-ack"><input id="journalConfirmChecked" type="checkbox"><span id="journalConfirmAcknowledgementText"></span></label><div class="row"><button id="journalConfirmCancel" class="secondary" autofocus>先不操作</button><button id="journalConfirmSubmit">确认</button></div>';
    document.body.appendChild(dialog);
    dialog.addEventListener('cancel',event=>{event.preventDefault();cancelJournalConfirmation();});
    dialog.addEventListener('close',()=>{if(!dialog.open && journalConfirmation?.dialog===dialog)cancelJournalConfirmation();});
    document.getElementById('journalConfirmCancel').onclick=cancelJournalConfirmation;
    document.getElementById('journalConfirmChecked').onchange=()=>{
      document.getElementById('journalConfirmSubmit').disabled=!document.getElementById('journalConfirmChecked').checked;
    };
    document.getElementById('journalConfirmSubmit').onclick=()=>{
      if(document.getElementById('journalConfirmSubmit').disabled || !journalConfirmation)return;
      const current=journalConfirmation;journalConfirmation=null;
      dialog.close();current.resolve(true);
    };
  }
  document.getElementById('journalConfirmTitle').textContent=title;
  document.getElementById('journalConfirmMessage').textContent=message;
  document.getElementById('journalConfirmAcknowledgement').hidden=!acknowledgement;
  document.getElementById('journalConfirmAcknowledgementText').textContent=acknowledgement;
  document.getElementById('journalConfirmChecked').checked=false;
  const submit=document.getElementById('journalConfirmSubmit');
  submit.textContent=confirmLabel;submit.disabled=Boolean(acknowledgement);
  return new Promise(resolve=>{
    journalConfirmation={dialog,resolve};
    dialog.showModal();
    document.getElementById('journalConfirmCancel').focus();
  });
}
function openJournalDialog(id){ const dialog=document.getElementById(id); if(dialog && !dialog.open)dialog.showModal(); }
function closeJournalDialog(id){document.getElementById(id)?.close();}
function updateJournalShell(){
  if(!document.getElementById('journalShell'))return;
  const user=state.user, feature=collectionState.feature in journalPages ? collectionState.feature : 'trips';
  document.getElementById('welcomeScene').hidden=Boolean(user);
  document.getElementById('journalShell').hidden=!user;
  document.getElementById('guestLoginButton').hidden=Boolean(user);
  document.getElementById('userInfo').hidden=!user;
  document.body.classList.toggle('is-authenticated',Boolean(user));
  if(!journalUI.mounted)return;
  if(journalUI.lastUser!==user?.id || journalUI.lastTrip!==state.tripId)cancelJournalConfirmation();
  journalUI.lastTrip=state.tripId;
  if(journalUI.lastUser!==user?.id){
    journalUI.steps={route:0,meeting:0};journalUI.mobileMap=false;
    ['createTripDialog','joinTripDialog'].forEach(id=>closeJournalDialog(id));
    journalUI.lastUser=user?.id;
    updateJournalTripCards();
  }
  const page=journalPages[feature];
  document.getElementById('workspaceTitle').textContent=page[0];
  document.getElementById('workspaceDescription').textContent=page[1];
  document.getElementById('workspaceEyebrow').textContent=page[2];
  document.getElementById('notebookLabel').textContent=page[2];
  const grid=document.getElementById('workspaceGrid');
  grid.dataset.feature=feature;
  grid.classList.toggle('mobile-map-visible',journalUI.mobileMap && !['trips','saved'].includes(feature));
  document.getElementById('mobileMapSwitch').textContent=journalUI.mobileMap?'返回手账':'查看地图';
  document.getElementById('mobileMapSwitch').hidden=['trips','saved'].includes(feature);
  const trip=state.currentTrip;
  document.getElementById('journalTripPill').textContent=trip?trip.title+' · '+({active:'规划中',confirmed:'已确认',completed:'已完成'}[trip.status]||trip.status):'选择一段旅程';
  document.getElementById('workspaceStatus').textContent=trip?({active:'规划中',confirmed:'已确认',completed:'已完成'}[trip.status]||trip.status):'尚未选择行程';
  ['startStep','participantStep','meetingWorkflow','routeWorkflow','destinationWorkflow','importWorkflow'].forEach(id=>document.getElementById(id)?.classList.remove('step-hidden'));
  document.querySelector('.location-tools')?.classList.remove('step-hidden');
  document.querySelector('.workflow-choice')?.classList.remove('step-hidden');
  const stepNames=feature==='route'?['① 时间与出发','② 选择地点','③ 查看路线']:feature==='meeting'?['① 同行起点','② 约点条件','③ 比较推荐']:[];
  const step=journalUI.steps[feature]||0;
  grid.dataset.step=step;
  document.getElementById('journalSteps').innerHTML=stepNames.map((name,index)=>'<button class="'+(step===index?'active':'')+'" aria-current="'+(step===index?'step':'false')+'" onclick="setJournalStep('+index+')">'+name+'</button>').join('');
  if(stepNames.length && state.tripId){
    const visible=feature==='route'
      ? [['startStep','participantStep','routeWorkflow'],['destinationWorkflow'],[]][step]
      : [['startStep','participantStep'],['meetingWorkflow'],[]][step];
    ['startStep','participantStep','meetingWorkflow','routeWorkflow','destinationWorkflow'].forEach(id=>document.getElementById(id)?.classList.toggle('step-hidden',!visible.includes(id)));
    document.querySelector('.location-tools')?.classList.toggle('step-hidden',!(feature==='route'?step<2:step===0));
    document.querySelector('.workflow-choice')?.classList.toggle('step-hidden',step!==0);
    const result=document.getElementById(feature==='route'?'itineraryPanel':'resultPanel');
    if(step===2 && result.style.display==='none')document.getElementById('journalReviewHint').hidden=false;
    else document.getElementById('journalReviewHint').hidden=true;
  } else document.getElementById('journalReviewHint').hidden=true;
  const actions=document.getElementById('journalActions');
  actions.hidden=!user || !stepNames.length || Boolean(trip && trip.status!=='active');
  document.getElementById('journalBack').hidden=!state.tripId || step===0;
  document.getElementById('journalNext').textContent=!state.tripId?'选择或创建行程':step===0?'下一步：'+(feature==='route'?'选择地点':'约点条件'):step===1?(feature==='route'?'生成路线 →':'寻找推荐 →'):(feature==='route'?'生成 / 更新路线':'生成 / 更新约点推荐');

}
function setJournalStep(step){
  const feature=collectionState.feature;
  if(!['route','meeting'].includes(feature))return;
  journalUI.steps[feature]=Math.max(0,Math.min(2,step));
  updateFlowUI();
  document.getElementById('sidePanel').scrollTop=0;
  requestAnimationFrame(()=>{ if(state.map && typeof state.map.resize==='function')state.map.resize();drawAll(false); });
}
function journalPreviousStep(){setJournalStep((journalUI.steps[collectionState.feature]||0)-1);}
async function journalNextStep(){
  if(!state.tripId)return selectFeature('trips');
  const feature=collectionState.feature,step=journalUI.steps[feature]||0;
  if(step===0){setJournalStep(1);return;}
  if(step===1)setJournalStep(2);
  const button=document.getElementById('journalNext'); if(button.disabled)return;button.disabled=true;
  try{if(feature==='route')await generateItinerary();else await doRecommend();}
  finally{button.disabled=false;updateJournalShell();}
}
function toggleJournalMap(){journalUI.mobileMap=!journalUI.mobileMap;updateJournalShell();requestAnimationFrame(()=>drawAll(false));}
async function openJournalTrip(id){
  try{await openTrip(id);selectFeature(state.currentTrip.primary_workflow==='meeting'?'meeting':'route');}
  catch(e){toast(e.message,true);}
}
function updateJournalTripCards(){
  const el=document.getElementById('journalTripCards');if(!el)return;
  const items=state.myTrips||[],labels={active:'规划中',confirmed:'已确认',completed:'已完成'};
  if(!items.length){el.innerHTML='<div class="empty-notebook"><b>旅程还没有开始</b>创建一段新行程，或用朋友的邀请码加入。</div>';return;}
  el.innerHTML=items.map(item=>'<article class="destination-card"><div class="trip-card-top"><small>JOURNEY · '+String(item.trip_id).padStart(2,'0')+'</small><span class="tag '+(item.status==='active'?'green':'orange')+'">'+escapeHtml(labels[item.status]||item.status)+'</span></div><div class="trip-card-title">'+escapeHtml(item.title)+'</div><div class="trip-card-meta">'+(item.role==='creator'?'我发起的旅程':'与朋友同行')+'</div><button class="secondary" onclick="openJournalTrip('+Number(item.trip_id)+')">打开这段旅程 →</button></article>').join('');
}
function chooseJournalScene(scene){
  document.getElementById('discoveryScene').value=scene;
  document.getElementById('assistantMessage').value={meal:'我们约着吃饭，不知道去哪，推荐几家合适的餐厅。',brand:'决定吃寿司郎，比较几家合适的分店并说明理由。',afternoon:'我想在附近游玩一下午，请根据条件安排一条路线。'}[scene];
  document.getElementById('discoveryOptions').open=true;
  document.getElementById('assistantMessage').focus();
}
function mountJournalShell(){
  if(journalUI.mounted)return;journalUI.mounted=true;
  const notice=document.getElementById('mapTip');
  notice.setAttribute('role','status');notice.setAttribute('aria-live','polite');
  document.body.appendChild(notice);
  const trip=document.getElementById('tripStep');
  const createRow=document.getElementById('tripTitle').closest('.row'),joinRow=document.getElementById('inviteCode').closest('.row');
  for(const [id,title,row,inputId] of [['createTripDialog','开始一段新旅程',createRow,'tripTitle'],['joinTripDialog','和朋友一起出发',joinRow,'inviteCode']]){
    const dialog=document.createElement('dialog');dialog.id=id;dialog.className='journal-dialog';
    dialog.innerHTML='<div class="eyebrow">LET THE JOURNEY BEGIN</div><h2>'+title+'</h2><p class="muted">'+(inputId==='tripTitle'?'给这次相聚起个名字，之后邀请朋友加入。':'输入朋友分享给你的行程邀请码。')+'</p>';
    dialog.appendChild(row);
    const cancel=document.createElement('button');cancel.className='secondary';cancel.textContent='先不急';cancel.onclick=()=>dialog.close();dialog.appendChild(cancel);document.body.appendChild(dialog);
    document.getElementById(inputId).setAttribute('aria-label',inputId==='tripTitle'?'行程名称':'行程邀请码');
    document.getElementById(inputId).addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();if(inputId==='tripTitle')doCreateTrip();else doJoinTrip();}});
  }
  trip.querySelector('h2').hidden=true;
  const intro=document.createElement('div');intro.className='trips-intro';intro.innerHTML='<h2>把相聚，写进手账里。</h2><p>不用急着决定去哪儿，先约上想一起出发的人。</p><div class="trip-quick-actions"><button onclick="openJournalDialog(\'createTripDialog\')">＋ 创建行程</button><button class="secondary" onclick="openJournalDialog(\'joinTripDialog\')">用邀请码加入</button></div>';trip.prepend(intro);
  const filter=document.getElementById('tripStatusFilter');
  const row=document.createElement('div');row.className='row';row.innerHTML='<span style="align-self:center;font-size:12px;color:#8b775c">我的旅行手账</span>';row.appendChild(filter);intro.after(row);
  document.getElementById('myTripSelect').closest('.row').hidden=true;
  const cards=document.createElement('div');cards.id='journalTripCards';cards.className='trip-library-grid';row.after(cards);
  const review=document.createElement('div');review.id='journalReviewHint';review.className='empty-notebook';review.hidden=true;review.style.order='10';review.innerHTML='<b>这一程，慢慢展开。</b>点击下方按钮生成结果。已有路线会显示在地图下方。';document.getElementById('sidePanel').appendChild(review);
  const assistant=document.getElementById('assistantStep'),options=document.getElementById('discoveryOptions');
  options.open=false;options.querySelector('summary').textContent='调整城市、时间、范围与偏好';
  const scenes=document.createElement('div');scenes.className='scene-choices';scenes.innerHTML='<button onclick="chooseJournalScene(\'meal\')">一起吃点什么？<small>不知道去哪吃，找一个对大家都方便的地方。</small></button><button onclick="chooseJournalScene(\'brand\')">喜欢的店，选哪一家？<small>比较品牌分店，顺便看看周边有什么。</small></button><button onclick="chooseJournalScene(\'afternoon\')">留一个下午给风景。<small>从收藏和附近地点里，安排一段小旅行。</small></button>';
  assistant.querySelector('h2').after(scenes);
  const message=document.getElementById('assistantMessage');message.rows=3;message.placeholder='说说你想怎么度过这段时光…';
  message.insertAdjacentElement('afterend',options);
  const cloud=document.getElementById('cloudEntry');let pointerStart=null;
  cloud.addEventListener('pointerdown',event=>{pointerStart={id:event.pointerId,x:event.clientX};cloud.setPointerCapture(event.pointerId);});
  cloud.addEventListener('pointermove',event=>{if(pointerStart && event.pointerId===pointerStart.id && Math.abs(event.clientX-pointerStart.x)>70){pointerStart=null;openWelcomeLogin();}});
  cloud.addEventListener('pointerup',()=>pointerStart=null);cloud.addEventListener('pointercancel',()=>pointerStart=null);
  updateJournalTripCards();updateFlowUI();
}
document.addEventListener('DOMContentLoaded',mountJournalShell);
