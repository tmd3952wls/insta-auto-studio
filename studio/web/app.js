'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name=studio-token]').content;
const fishLoginLink = document.createElement('a');
fishLoginLink.textContent = 'Fish 로그인 화면 열기 ↗';
fishLoginLink.target = '_blank';
fishLoginLink.rel = 'noopener noreferrer';
fishLoginLink.hidden = true;
$('fishState').after(fishLoginLink);
let job = null, selectedPhoto = 0, previewUrl = null, previewSequence = 0, previewTimer, dirtyScript = false;
let collection = null, collectionSelected = new Set(), sourceBusy = false, polling = false;
const exportCache = new Map();
const busyStates = new Set(['preparing','analyzing','recommending','quoting','generating','rendering','previewing','enhancing']);
const numericControls = ['fontSize','posY','posX'];
let candidateSelected = new Set(), dirtyStyle = false, modelComparison = null;
let previewMode = 'photo', connectionState = null, uiError = null;
let selectedCaptionCue=0,captionCueCount=0;
const captionCueControls=document.createElement('div');captionCueControls.className='caption-cues';captionCueControls.setAttribute('aria-label','현재 사진의 화면 자막');$('previewNote').before(captionCueControls);
async function api(path, body, binary=false) {
  const options = body === undefined ? {} : {method:'POST',headers:{'X-Studio-Token':token},body:body instanceof FormData ? body : JSON.stringify(body)};
  if(body !== undefined && !(body instanceof FormData)) options.headers['Content-Type']='application/json';
  const response = await fetch(path, options);
  if(!response.ok){let message;try{message=(await response.json()).detail;}catch{message=await response.text();}throw new Error(typeof message==='string'?message:JSON.stringify(message));}
  if(binary)return response.blob();
  const value=await response.json();
  if(path==='/api/connections'){
    fishLoginLink.hidden = true;
    if(value.fish_login_url){
      const url = new URL(value.fish_login_url);
      if(url.protocol === 'https:' && (url.hostname === 'fish.audio' || url.hostname.endsWith('.fish.audio'))){
        fishLoginLink.href = url.href;
        fishLoginLink.hidden = false;
      }
    }
    for(const button of document.querySelectorAll('[data-connect]')) {
      button.disabled = Boolean(value[button.dataset.connect + '_busy']);
    }
    const link=$('codexAuthUrl');
    const match=(value.codex_login||'').match(/https:\/\/auth\.openai\.com\/[^\s\x1b]+/);
    link.hidden=value.codex||!match;
    if(match)link.href=match[0];
  }
  return value;
}
function showError(error){uiError=error.message||String(error);$('status').className='error';$('statusTitle').textContent='확인이 필요해요.';$('statusDetail').textContent=uiError;}
function action(fn){return async()=>{uiError=null;try{await fn();}catch(error){showError(error);}};}
function hasUnsavedEdits(){return Boolean(job&&(dirtyScript||dirtyStyle||dirtyFacts));}
// Domain schema (stay, shopping): wording, features and user-entered facts come from /api/domains.
let domainList=[],chosenDomain='stay',dirtyFacts=false,domainKey='';
let captionPresets=[],savedDefaultStyle=null;
try{chosenDomain=localStorage.getItem('studioDomain')||'stay';}catch{}
function domainInfo(id=job?.domain||chosenDomain){return domainList.find(d=>d.id===id)||domainList[0]||null;}
function lines(element,text){element.replaceChildren();String(text||'').split('<br>').forEach((part,i)=>{if(i)element.append(document.createElement('br'));element.append(part);});}
function drawTabs(){
  const box=$('domainTabs');if(box.dataset.ids===domainList.map(d=>d.id).join())return;box.dataset.ids=domainList.map(d=>d.id).join();box.replaceChildren();
  for(const item of domainList){const tab=document.createElement('button');tab.type='button';tab.setAttribute('role','radio');tab.dataset.domain=item.id;tab.textContent=item.label;
    tab.onclick=()=>{if(job||sourceBusy)return;chosenDomain=item.id;try{localStorage.setItem('studioDomain',chosenDomain);}catch{}domainKey='';drawStatus();};box.append(tab);}
}
// 을/를: picks the object particle from the last syllable's final consonant (사진을, 메뉴를).
function eul(word){const code=(word||'').charCodeAt(word.length-1)-0xac00;return word+(code>=0&&code<=11171&&code%28?'을':'를');}
function words(){return domainInfo()?.ui||{noun:'사진',unit:'장',select:'선택한 사진',analyze:'사진 분석 & 대본 만들기 ↗',nav_hint:'←/→ 키로 사진 이동',heading:'사진 & 이야기'};}
function hasNarration(){return domainInfo()?.features.narration!==false;}
function drawDomain(){
  if(domainList.length&&!domainList.some(d=>d.id===chosenDomain))chosenDomain=domainList[0].id;
  drawTabs();const id=job?.domain||chosenDomain,info=domainInfo(id);
  for(const tab of document.querySelectorAll('#domainTabs [data-domain]')){const on=tab.dataset.domain===id;tab.setAttribute('aria-checked',String(on));tab.disabled=sourceBusy||(Boolean(job)&&!on);tab.title=job&&!on?'영상 종류는 새 작업에서 바꿀 수 있어요':'';}
  if(!info)return;
  const key=`${info.id}|${job?.id||''}`;if(key===domainKey)return;domainKey=key;
  if(!job){$('captionPreset').value='';$('captionPresetName').value='';$('captionPresetNotice').textContent='글자 크기·색상·위치·정렬을 저장해 다른 작업에서 불러오세요. 같은 이름으로 저장하면 설정을 갱신합니다.';if(info.id==='stay-silent')loadStyle({caption:{font_size:64,x:-35,y:-30,color:'#FFFFFF',outline:8,background_opacity:0.2,shadow:3,align:'left'},speed:1});else if(savedDefaultStyle)loadStyle(savedDefaultStyle);}
  const ui=info.ui;$('introText').textContent=ui.intro||'';$('uploadTitle').textContent=ui.upload_title||'';
  lines($('previewTitle'),ui.preview_title);lines($('previewText'),ui.preview_text);
  $('linkSource').hidden=!info.features.link_collection;
  $('prepare').textContent=info.features.enhance&&info.media==='photo'?'고화질 + 대본 함께 만들기 ↗':ui.analyze;
  const video=info.media==='video';
  const narrated=hasNarration();
  $('targetCount').min=info.id==='stay-silent'?'8':'1';$('targetCount').max=info.id==='stay-silent'?'10':'60';
  if(!$('targetCount').validity.valid)$('targetCount').value='10';
  $('speed').closest('label').hidden=!narrated;document.querySelector('.voice-note').hidden=!narrated;
  $('audioDownload').closest('.export-card').hidden=!narrated;
  $('download').closest('.export-card').querySelector('small').textContent=narrated?'자막 + 내레이션 · MP4':'자막 포함 · 음성 없음 · MP4';
  $('advancedStyle').querySelector('summary').textContent=narrated?'자막 위치·색상·음성 속도 설정':'자막 위치·색상 설정';
  $('recoverVoice').closest('details').hidden=!narrated;
  $('files').accept=video?'video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v,.webm':'image/jpeg,image/png,image/webp';
  $('uploadHint').textContent=ui.upload_hint||'드래그하거나 클릭해서 선택 · JPG, PNG, WebP';
  $('photoHint').textContent=video?'영상을 누르면 자막 가림(확대·위치)을 조절할 수 있어요':info.id==='stay-silent'?'8~10장 선택 · 장당 2초 · 전체 16~20초':'모든 사진이 영상에 담깁니다 · 끌어서 순서 변경';
  document.querySelector('.composition-controls').hidden=video;$('motionEditor').hidden=video;$('modelPanel').hidden=video;
  $('sourceHeading').textContent=ui.heading;$('photoPreview').textContent=ui.select;
  $('stepPhotosLabel').textContent=`${ui.noun} 선택`;$('factsTitle').textContent=ui.facts_title||`${info.label} 정보`;$('factsHelp').textContent=ui.facts_help||'대본의 사실 근거로만 쓰여요. 가격·효능 같은 사실은 여기에 적은 것만 대본에 들어가요.';
  document.querySelector('input[name=compositionMode][value=ai]').closest('label').hidden=!info.features.ai_composition;
  $('badgeControls').hidden=!info.disclosure;$('thumbnailPanel').hidden=info.media!=='video';
  $('captionText').defaultValue=ui.caption_default||'';if(!job?.storyboard)$('captionText').value=$('captionText').defaultValue;
  if(compositionMode()==='ai')$('compositionHelp').textContent=ui.ai_help||'';
  const fields=$('factsFields');fields.replaceChildren();$('factsPanel').hidden=!job||!info.facts.length;
  for(const fact of info.facts){const label=document.createElement('label');label.className='field';label.textContent=fact.label;const input=document.createElement(fact.key==='memo'?'textarea':'input');input.dataset.fact=fact.key;input.maxLength=300;input.value=job?.facts?.[fact.key]||'';input.oninput=()=>{dirtyFacts=true;drawStatus();};label.append(input);fields.append(label);}
  if(!job)drawPhotos();  // Empty grid header follows the chosen kind (사진 0장 / 소스 영상 0개).
}
function factsValue(){const value={};for(const input of document.querySelectorAll('#factsFields [data-fact]'))value[input.dataset.fact]=input.value;return value;}

function confirmDiscard(){return !hasUnsavedEdits()||window.confirm('저장하지 않은 변경 사항이 있어요. 변경 사항을 버리고 이동할까요? 취소하면 계속 편집할 수 있어요.');}
window.addEventListener('beforeunload',event=>{if(hasUnsavedEdits()){event.preventDefault();event.returnValue='';}});
function numericValid(){return numericControls.every(id=>$(id+'Input').validity.valid);}
function caption(){if(!numericValid())throw new Error('글자 크기와 X/Y 위치의 입력 범위를 확인해 주세요.');return {font_size:Number($('fontSize').value),x:Number($('posX').value),y:Number($('posY').value),color:$('color').value,outline:Number($('outline').value),background_opacity:Number($('bgOpacity').value)/100,shadow:Number($('shadow').value),align:$('captionAlign').value};}
const badgeControls=['badgeSide','badgeSize','badgeX','badgeY'];
function badge(){if(!badgeControls.slice(1).every(id=>$(id).validity.valid))throw new Error('광고 표기의 크기와 거리 입력 범위를 확인해 주세요.');return {side:$('badgeSide').value,size:Number($('badgeSize').value),x:Number($('badgeX').value),y:Number($('badgeY').value)};}
// Video domains: the still that opens the reel, with a title styled apart from the captions.
const thumbNumbers=['thumbSize','thumbY','thumbX','thumbOutline','thumbBg','thumbShadow'],thumbControls=['thumbEnabled','thumbText','thumbColor','thumbAlign',...thumbNumbers];
let thumbFrame={source:null,time:0},thumbTimer=null;
function thumbnail(){if(!thumbNumbers.every(id=>$(id).validity.valid))throw new Error('썸네일 제목의 크기·위치 입력 범위를 확인해 주세요.');return {enabled:$('thumbEnabled').checked,text:$('thumbText').value,source:thumbFrame.source,time:thumbFrame.time,style:{font_size:Number($('thumbSize').value),x:Number($('thumbX').value),y:Number($('thumbY').value),color:$('thumbColor').value,outline:Number($('thumbOutline').value),background_opacity:Number($('thumbBg').value)/100,shadow:Number($('thumbShadow').value),align:$('thumbAlign').value}};}
function drawThumbFrame(){$('thumbFrameInfo').textContent=thumbFrame.source===null?'썸네일 사진: 첫 컷의 첫 화면(자동)':`썸네일 사진: 영상 ${thumbFrame.source+1} · ${Number(thumbFrame.time).toFixed(1)}초`;}
async function showThumbnail(){if(!job||!job.photos.length)return;const id=job.id,blob=await api(`/api/jobs/${id}/thumbnail-preview`,{thumbnail:thumbnail(),badge:badge()},true);if(job?.id!==id)return;previewMode='photo';$('video').pause();++previewSequence;if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(blob);$('preview').src=previewUrl;$('preview').hidden=false;$('previewEmpty').hidden=true;$('video').hidden=true;}
function options(){return {max_motion:Number($('motion').value),caption:caption(),badge:badge(),thumbnail:thumbnail(),speed:Number($('speed').value)};}
function loadStyle(options=job?.options){if(!options)return;const c=options.caption;if(options.badge){$('badgeSide').value=options.badge.side;$('badgeSize').value=options.badge.size;$('badgeX').value=options.badge.x;$('badgeY').value=options.badge.y;}if(options.thumbnail){const t=options.thumbnail,s=t.style;$('thumbEnabled').checked=t.enabled;$('thumbText').value=t.text;thumbFrame={source:t.source??null,time:t.time||0};$('thumbSize').value=s.font_size;$('thumbX').value=s.x;$('thumbY').value=s.y;$('thumbColor').value=s.color;$('thumbOutline').value=s.outline;$('thumbBg').value=Math.round(s.background_opacity*100);$('thumbShadow').value=s.shadow;$('thumbAlign').value=s.align||'center';drawThumbFrame();}for(const [id,key] of [['fontSize','font_size'],['posX','x'],['posY','y'],['color','color'],['outline','outline'],['shadow','shadow']])$(id).value=c[key];$('captionAlign').value=c.align||'center';for(const id of numericControls){$(id+'Input').value=$(id).value;$(id+'Error').hidden=true;$(id+'Input').removeAttribute('aria-invalid');}$('bgOpacity').value=Math.round(c.background_opacity*100);$('speed').value=String(options.speed??1);motionOptions();updateOutputs();}
function drawCaptionPresets(selected=$('captionPreset').value){$('captionPreset').replaceChildren(new Option('설정을 선택하세요',''));for(const item of captionPresets)$('captionPreset').append(new Option(item.name,item.id));$('captionPreset').value=selected;}
$('captionPreset').onchange=action(async()=>{const item=captionPresets.find(p=>p.id===$('captionPreset').value);if(!item)return;loadStyle({caption:item.caption,speed:Number($('speed').value)});$('captionPresetName').value=item.name;if(job){dirtyStyle=true;queuePreview();}drawStatus();$('captionPresetNotice').textContent=`${item.name} 설정을 불러왔어요.`;});
$('saveCaptionPreset').onclick=action(async()=>{const name=$('captionPresetName').value.trim();if(!name)throw new Error('저장할 자막 설정의 이름을 입력해 주세요.');const item=await api('/api/caption-presets',{name,caption:caption()});captionPresets=await api('/api/caption-presets');drawCaptionPresets(item.id);$('captionPresetNotice').textContent=`${item.name} 설정을 저장했어요. 다른 작업에서도 선택할 수 있어요.`;});
function motionOptions(){
  $('motion').value=String(Math.min(Number($('motion').defaultValue),job?.photos.length||0));
}
function updateOutputs(){$('bgOpacityValue').value=$('bgOpacity').value+'%';}
function queuePreview(){previewMode='photo';$('video').pause();updateOutputs();clearTimeout(previewTimer);previewTimer=setTimeout(()=>updatePreview().catch(showError),180);drawPreviewStatus(Boolean(sourceBusy||job&&busyStates.has(job.state)));}
async function updatePreview(){
  if(!job||!job.photos.length||!numericValid())return;
  if(previewMode==='video'&&(job.preview||job.result)){showVideo();return;}
  const sequence=++previewSequence,id=job.id,photo=selectedPhoto;
  const focus=job.storyboard?.crop_focus.find(f=>f.photo===photo);
  const style=caption(),cues=await api(`/api/jobs/${id}/caption-cues`,{text:$('captionText').value,caption:style});
  if(sequence!==previewSequence||job?.id!==id||selectedPhoto!==photo||previewMode!=='photo')return;
  captionCueCount=cues.length;selectedCaptionCue=Math.min(selectedCaptionCue,Math.max(cues.length-1,0));captionCueControls.replaceChildren();captionCueControls.hidden=cues.length<2;
  for(const [index,text] of cues.entries()){const button=document.createElement('button');button.type='button';button.className='outline';button.textContent=`${index+1}. ${text}`;button.setAttribute('aria-pressed',String(index===selectedCaptionCue));button.onclick=action(async()=>{selectedCaptionCue=index;await updatePreview();});captionCueControls.append(button);}
  const text=cues[selectedCaptionCue]||'';
  const body=job.photos[photo]?.kind==='video'?{photo,time:previewTime,text,caption:style,badge:badge(),...(cropDraft?{crop:cropDraft}:{})}
    :{photo,text,caption:style,...(focus?{focus:{x:focus.x,y:focus.y}}:{})};
  const blob=await api(`/api/jobs/${id}/preview`,body,true);
  if(sequence!==previewSequence||job?.id!==id||selectedPhoto!==photo||previewMode!=='photo')return;
  if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(blob);$('preview').src=previewUrl;
  $('preview').hidden=false;$('previewEmpty').hidden=true;$('video').hidden=true;drawPreviewStatus(Boolean(sourceBusy||busyStates.has(job.state)));
}
function drawPhotos(){
  const grid=$('photoGrid');photoDrag=null;grid.classList.remove('reordering');grid.replaceChildren();$('photoCount').textContent=`${videoDomain()?'소스':'사용'} ${words().noun} ${job?job.photos.length:0}${words().unit}`;if(!job)return;
  const order=job.photo_order?.length?job.photo_order:job.photos.map((_,i)=>i);
  order.forEach((index,position)=>{if(job.photos[index].kind==='video'){grid.append(videoCard(index,position));return;}const photo=job.photos[index],card=document.createElement('div');card.className='ordered-photo';card.onpointerdown=e=>grabPhoto(e,card,position);
    const button=document.createElement('button');button.title=photo.name;button.setAttribute('aria-label',`${position+1}번 사진 선택`);button.classList.toggle('selected',index===selectedPhoto);
    const image=document.createElement('img');image.src=`/api/jobs/${job.id}/files/${photo.file}`;image.alt=photo.name;image.loading='lazy';image.draggable=false;const number=document.createElement('span');number.textContent=String(position+1).padStart(2,'0');button.append(image,number);
    button.onclick=action(()=>selectPhoto(index));
    const controls=document.createElement('div');controls.className='photo-moves';for(const [label,delta] of [['←',-1],['→',1]]){const move=document.createElement('button');move.textContent=label;move.setAttribute('aria-label',`${position+1}번 사진 ${delta<0?'앞':'뒤'}으로 이동`);move.disabled=planLocked()||position+delta<0||position+delta>=order.length;move.onclick=action(()=>movePhoto(position,position+delta));controls.append(move);}card.append(button,controls);enhanceControls(card,button,photo,index,position);
    const reason=job.selection_reasons?.[photo.sha256]||job.storyboard?.selection_reasons?.[String(index)];if(reason){const details=document.createElement('small');details.textContent=reason;card.append(details);}grid.append(card);
  });drawCandidates();drawPhotoNav();drawCropEditor();
}
// Video domains (shopping): uploaded source clips, each zoomed to hide burned-in subtitles, cut into script segments.
let previewTime=0,cropDraft=null,cropTimer=null,selectedSegment=null;
function videoDomain(){return domainInfo()?.media==='video';}
function videoCard(index,position){
  const photo=job.photos[index],card=document.createElement('div');card.className='ordered-photo source-video';
  const button=document.createElement('button');button.title=photo.name;button.setAttribute('aria-label',`${position+1}번 영상 선택`);button.classList.toggle('selected',index===selectedPhoto);
  const image=document.createElement('img');image.src=`/api/jobs/${job.id}/files/${photo.thumb}`;image.alt=photo.name;image.loading='lazy';image.draggable=false;
  const number=document.createElement('span');number.textContent=`${String(position+1).padStart(2,'0')} · ${photo.duration.toFixed(1)}초`;button.append(image,number);
  const zoom=photo.crop?.zoom||1;if(zoom>1.001){const badge=document.createElement('b');badge.className='hd-badge';badge.textContent=`×${zoom.toFixed(2)}`;badge.title='자막 가림 확대';button.append(badge);}
  button.onclick=action(()=>selectSource(index));const name=document.createElement('small');name.textContent=photo.name;card.append(button,name);return card;
}
function selectedSource(){const photo=job?.photos[selectedPhoto];return photo?.kind==='video'?photo:null;}
function drawCropEditor(){
  const source=selectedSource();$('cropEditor').hidden=!source;if(!source)return;
  const crop=cropDraft||source.crop||{zoom:1,x:.5,y:.5};
  $('cropTitle').textContent=`영상 ${selectedPhoto+1} · 자막 가림`;
  $('cropReason').textContent=source.crop_reason?`AI 판단: ${source.crop_reason}`:'영상을 분석하면 AI가 자막 위치를 보고 확대를 제안해요.';
  $('cropZoom').value=crop.zoom;$('cropX').value=crop.x;$('cropY').value=crop.y;$('cropTime').max=Math.max(source.duration-0.1,0).toFixed(1);$('cropTime').value=previewTime;cropLabels();
  const locked=planLocked();for(const id of ['cropZoom','cropX','cropY','cropNone'])$(id).disabled=locked;$('cropAi').disabled=locked||!source.crop_ai;
}
function cropLabels(){const place=(value,low,high)=>value<0.4?low:value>0.6?high:'가운데';$('cropZoomValue').value=`×${Number($('cropZoom').value).toFixed(2)}`;$('cropYValue').value=place(Number($('cropY').value),'위쪽','아래쪽');$('cropXValue').value=place(Number($('cropX').value),'왼쪽','오른쪽');$('cropTimeValue').value=`${Number($('cropTime').value).toFixed(1)}초`;}
async function selectSource(index,time=null){selectedPhoto=index;cropDraft=null;previewTime=time??Math.min(1,(job.photos[index].duration||1)/2);previewMode='photo';$('video').pause();drawPhotos();await updatePreview();}
function editCrop(){cropDraft={zoom:Number($('cropZoom').value),x:Number($('cropX').value),y:Number($('cropY').value)};cropLabels();queuePreview();clearTimeout(cropTimer);const index=selectedPhoto,draft=cropDraft;cropTimer=setTimeout(()=>saveCrop(index,draft).catch(showError),500);}
async function saveCrop(index,draft){if(!job||planLocked())return;const storyboard=job.storyboard;job={...await api(`/api/jobs/${job.id}/sources/${index}/crop`,draft),preview_stale:true};if(dirtyScript)job.storyboard=storyboard;if(cropDraft===draft)cropDraft=null;drawPhotos();drawStatus();}
for(const id of ['cropZoom','cropX','cropY'])$(id).addEventListener('input',editCrop);
$('cropTime').addEventListener('input',()=>{previewTime=Number($('cropTime').value);cropLabels();queuePreview();});
$('cropAi').onclick=()=>{const ai=selectedSource()?.crop_ai;if(!ai)return;$('cropZoom').value=ai.zoom;$('cropX').value=ai.x;$('cropY').value=ai.y;editCrop();};
$('cropNone').onclick=()=>{$('cropZoom').value=1;$('cropX').value=.5;$('cropY').value=.5;editCrop();};
function currentScene(){const plan=job?.storyboard;if(!plan)return null;return plan.segments?.length?plan.scenes.find(s=>s.photos.includes(selectedSegment)):plan.scenes.find(s=>s.photos.includes(selectedPhoto));}
function regroup(change){const plan=job.storyboard,groups=plan.scenes.map(s=>s.photos.map(i=>({...plan.segments[i]})));change(groups);let next=0;plan.segments=groups.flat();plan.scenes.forEach((scene,i)=>{scene.photos=groups[i].map(()=>next++);});selectedSegment=null;planChanged();drawScript();}
function secondsInput(value,label){const input=document.createElement('input');input.type='number';input.step='0.1';input.min='0';input.value=Number(value).toFixed(1);input.className='segment-time';input.setAttribute('aria-label',label);return input;}
function segmentValid(segment){const length=segment.end-segment.start,source=job.photos[segment.source];return Boolean(source)&&segment.start>=0&&length>=0.5&&length<=8&&segment.end<=source.duration+0.05;}
function segmentRows(scene,sceneIndex){
  const plan=job.storyboard,locked=planLocked(),list=document.createElement('div');list.className='segment-list';
  scene.photos.forEach((segmentIndex,k)=>{
    const segment=plan.segments[segmentIndex],row=document.createElement('div');row.className='segment-row';row.classList.toggle('selected',segmentIndex===selectedSegment);row.classList.toggle('invalid',!segmentValid(segment));
    const pick=document.createElement('select');pick.setAttribute('aria-label',`${sceneIndex+1}번 장면 ${k+1}번 구간의 영상`);
    job.photos.forEach((photo,i)=>{if(photo.kind!=='video')return;const option=document.createElement('option');option.value=String(i);option.textContent=`영상 ${i+1}`;pick.append(option);});pick.value=String(segment.source);
    const start=secondsInput(segment.start,`${sceneIndex+1}번 장면 ${k+1}번 구간 시작 초`),end=secondsInput(segment.end,`${sceneIndex+1}번 장면 ${k+1}번 구간 끝 초`);
    const sync=()=>{segment.source=Number(pick.value);segment.start=Number(start.value);segment.end=Number(end.value);row.classList.toggle('invalid',!segmentValid(segment));planChanged();};
    for(const input of [pick,start,end]){input.disabled=locked;input.oninput=sync;}
    const view=document.createElement('button');view.type='button';view.className='quiet';view.textContent='보기';view.onclick=action(async()=>{selectedSegment=segmentIndex;$('captionText').value=scene.text;await selectSource(segment.source,segment.start);drawScript();});
    const remove=document.createElement('button');remove.type='button';remove.className='quiet';remove.textContent='삭제';remove.disabled=locked||scene.photos.length<2;remove.onclick=()=>regroup(groups=>groups[sceneIndex].splice(k,1));
    const seconds=document.createElement('span');seconds.className='segment-sep';seconds.textContent='~';
    row.append(pick,start,seconds,end,view,remove);list.append(row);
  });
  const add=document.createElement('button');add.type='button';add.className='quiet segment-add';add.textContent='+ 구간 추가';add.disabled=locked||scene.photos.length>=4;
  add.onclick=()=>regroup(groups=>{const last=groups[sceneIndex].at(-1),length=job.photos[last.source].duration,start=Math.max(0,Math.min(last.end,length-2));groups[sceneIndex].push({source:last.source,start:+start.toFixed(1),end:+Math.min(start+2,length).toFixed(1)});});
  list.append(add);return list;
}
// 사진 끌어서 순서 바꾸기: 마우스는 6px 이상 끌면, 터치는 길게 누르면 시작한다.
let photoDrag=null,photoClickBlock=0;
function grabPhoto(e,card,from){
  if(photoDrag||planLocked()||(e.pointerType==='mouse'&&e.button!==0)||e.target.closest('.photo-moves,.photo-enhance'))return;
  photoDrag={card,from,id:e.pointerId,x0:e.clientX,y0:e.clientY,x:e.clientX,y:e.clientY,touch:e.pointerType!=='mouse',active:false};
  if(photoDrag.touch)photoDrag.timer=setTimeout(startPhotoDrag,350);
}
function startPhotoDrag(){
  const d=photoDrag;if(!d||d.active)return;d.active=true;d.card.classList.add('dragging');$('photoGrid').classList.add('reordering');
  try{d.card.setPointerCapture(d.id);}catch{}navigator.vibrate?.(15);
  const scroll=()=>{if(photoDrag!==d||!d.active)return;const grid=$('photoGrid'),r=grid.getBoundingClientRect(),edge=40;
    const step=d.y<r.top+edge?-Math.ceil((r.top+edge-d.y)/4):d.y>r.bottom-edge?Math.ceil((d.y-r.bottom+edge)/4):0;
    if(step){grid.scrollTop+=step;placeDraggedPhoto();}requestAnimationFrame(scroll);};
  requestAnimationFrame(scroll);
}
function placeDraggedPhoto(){
  const d=photoDrag,grid=$('photoGrid'),target=document.elementFromPoint(d.x,d.y)?.closest('.ordered-photo');
  if(!target||target===d.card||target.parentElement!==grid)return;
  const cards=[...grid.children];if(cards.indexOf(d.card)<cards.indexOf(target))target.after(d.card);else target.before(d.card);
}
function endPhotoDrag(drop){
  const d=photoDrag;if(!d)return;photoDrag=null;clearTimeout(d.timer);if(!d.active)return;
  photoClickBlock=Date.now()+400;d.card.classList.remove('dragging');$('photoGrid').classList.remove('reordering');
  const to=[...$('photoGrid').children].indexOf(d.card);
  if(!drop||to<0)drawPhotos();else if(to!==d.from)movePhoto(d.from,to).catch(error=>{drawPhotos();showError(error);});
}
document.addEventListener('pointermove',e=>{
  const d=photoDrag;if(!d||e.pointerId!==d.id)return;d.x=e.clientX;d.y=e.clientY;
  if(!d.active){if(Math.hypot(d.x-d.x0,d.y-d.y0)<6)return;if(d.touch){endPhotoDrag(false);return;}startPhotoDrag();}
  e.preventDefault();placeDraggedPhoto();
});
document.addEventListener('pointerup',e=>{if(photoDrag?.id===e.pointerId)endPhotoDrag(true);});
document.addEventListener('pointercancel',e=>{if(photoDrag?.id===e.pointerId)endPhotoDrag(false);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&photoDrag?.active){e.preventDefault();endPhotoDrag(false);}},true);
document.addEventListener('touchmove',e=>{if(photoDrag?.active)e.preventDefault();},{passive:false});
document.addEventListener('contextmenu',e=>{if(photoDrag)e.preventDefault();});
document.addEventListener('click',e=>{if(Date.now()<photoClickBlock){e.preventDefault();e.stopPropagation();photoClickBlock=0;}},true);
function enhanceLocked(){return Boolean(!job||sourceBusy||busyStates.has(job.state)||job.pending_media||job.storyboard||Object.keys(job.generated).length||Object.keys(job.narration).length||job.result);}
function combinedPreparation(){return domainInfo()?.features.enhance&&domainInfo()?.media==='photo';}
function prepareBlockReason(){
  if(!job?.photos.length)return `${eul(words().noun)} 먼저 추가해 주세요.`;
  if(sourceBusy||busyStates.has(job.state))return '진행 중인 작업이 끝나면 사용할 수 있어요.';
  if(job.pending_media)return '제출한 영상·음성 생성 작업을 먼저 완료해 주세요.';
  if(!numericValid())return '자막 크기와 위치의 입력 범위를 확인해 주세요.';
  const count=job.photos.length,min=domainInfo()?.min_media||10;
  if(count<min)return `${words().noun} ${min}${words().unit} 이상 준비해 주세요. 현재 ${count}${words().unit}입니다.`;
  if(job.domain==='stay-silent'&&count>10)return '음성X는 후보에서 사용할 사진 8~10장을 골라 주세요.';
  const submitted=Object.values(job.generated).some(r=>r.file||r.generation_id||r.idempotency_key)||Object.values(job.narration).some(r=>r.file||r.url||['submitting','submitted','completed'].includes(r.state));
  if((combinedPreparation()||job.workflow_version<2)&&(submitted||job.result))return '영상·음성 생성 이후에는 새 작업에서 함께 만들기를 실행해 주세요.';
  return '';
}
function enhanceControls(card,button,photo,index,position){
  const enhanced=photo.enhanced||{},done=enhanced.state==='done',active=done&&photo.file===enhanced.file;
  if(active){const badge=document.createElement('b');badge.className='hd-badge';badge.textContent='HD';button.append(badge);}
  if(photo.enhancing){const mark=document.createElement('small');mark.className='enhance-note';mark.textContent='변환 중…';card.append(mark);return;}
  if(!photo.enhanced)return;
  const row=document.createElement('div');row.className='photo-enhance';
  if(done){const toggle=document.createElement('button');toggle.textContent=active?'HD':'원본';toggle.title=active?'변환본 사용 중 · 눌러서 원본으로':'원본 사용 중 · 눌러서 변환본으로';toggle.setAttribute('aria-label',`${position+1}번 사진 ${active?'원본으로 되돌리기':'변환본 사용'}`);toggle.setAttribute('aria-pressed',String(active));toggle.disabled=enhanceLocked();toggle.onclick=action(()=>photoSource(index,active?'original':'enhanced'));row.append(toggle);}
  const retry=document.createElement('button');retry.textContent='↻';retry.title='다시 변환';retry.setAttribute('aria-label',`${position+1}번 사진 다시 변환`);retry.disabled=enhanceLocked();retry.onclick=action(()=>enhancePhotos([index]));row.append(retry);
  if(done&&enhanced.file){const download=document.createElement('a');download.className='enhance-download';download.textContent='HD 저장 ↓';download.title='자막 없는 고화질 변환 사진 저장';download.setAttribute('aria-label',`${position+1}번 고화질 변환 사진 다운로드`);download.href=`/api/jobs/${job.id}/files/${encodeURIComponent(enhanced.file)}?download=true`;download.download=enhanced.file;row.append(download);}
  card.append(row);
  if(enhanced.error){const note=document.createElement('small');note.className='enhance-note failed';note.textContent=done?'다시 변환 실패 · 이전 변환본 유지':'변환 실패 · 원본 사용';note.title=enhanced.error;card.append(note);}
}
async function photoSource(index,source){
  await planTransaction(async()=>{job=await api(`/api/jobs/${job.id}/photos/${index}/source`,{source});});
  if(index===selectedPhoto){previewMode='photo';await updatePreview();}
}
async function enhancePhotos(photos=null){
  await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();await api(`/api/jobs/${job.id}/enhance`,{photos});job=await api('/api/jobs/'+job.id);});
}
function photoOrder(){return job?.photo_order?.length?job.photo_order:(job?.photos||[]).map((_,i)=>i);}
async function selectPhoto(index){selectedPhoto=index;selectedCaptionCue=0;previewMode='photo';$('video').pause();drawPhotos();const scene=job.storyboard?.scenes.find(s=>s.photos.includes(index));if(scene)$('captionText').value=scene.text;await updatePreview();}
async function stepPhoto(delta){const order=photoOrder(),next=order[order.indexOf(selectedPhoto)+delta];if(next!==undefined)await selectPhoto(next);}
function drawPhotoNav(){
  const order=photoOrder(),position=order.indexOf(selectedPhoto),show=previewMode==='photo'&&order.length>1&&position>=0&&!videoDomain();$('photoNav').hidden=!show;if(!show)return;
  const scene=job.storyboard?.scenes.findIndex(s=>s.photos.includes(selectedPhoto))??-1;
  $('photoPosition').textContent=(scene>=0?`장면 ${scene+1} · `:'')+`사진 ${position+1} / ${order.length}`;
  $('prevPhoto').disabled=position<=0;$('nextPhoto').disabled=position>=order.length-1;
}
function planLocked(){return Boolean(job&&(busyStates.has(job.state)||sourceBusy||job.pending_media||Object.values(job.generated).concat(Object.values(job.narration)).some(g=>['submitting','submitted'].includes(g.state)&&!g.file)||(job.workflow_version<2&&(Object.keys(job.narration).length||Object.values(job.generated).some(g=>g.generation_id||g.idempotency_key)))));}
function planValid(){return Boolean(job?.storyboard&&job.storyboard.scenes.every(s=>s.text.trim().length>0&&(job.domain!=='stay-silent'||s.text.trim().split('\n').length===2&&s.text.trim().split('\n').every(line=>line.trim().length>0&&line.trim().length<=18)))&&job.storyboard.motion.every(m=>m.prompt.trim().length>0&&m.prompt.trim().length<=2000)&&(job.storyboard.segments||[]).every(segmentValid));}
function planChanged(){dirtyScript=true;drawStatus();}
function drawScript(){
  const panel=$('storyboard');panel.replaceChildren();
  if(!job?.storyboard){const p=document.createElement('p');p.className='muted';p.textContent=`${eul(words().noun)} 분석하면 장면별 대본이 여기에 나타납니다.`;panel.append(p);drawMotion();return;}
  drawScriptOptions(panel);
  const made=job.storyboard.provenance;if(made?.version){const note=document.createElement('p');note.className='muted provenance';note.textContent=`${domainInfo(made.domain)?.label||made.domain} 스키마 v${made.version} · ${made.overridden?'사용자 프롬프트':'기본 프롬프트'}로 작성`;panel.append(note);}
  job.storyboard.scenes.forEach((scene,index)=>{
    const row=document.createElement('div');row.className='scene';
    const number=document.createElement('span');number.className='scene-number';number.textContent=String(index+1).padStart(2,'0');
    const body=document.createElement('div');body.className='scene-body';
    const text=document.createElement('textarea');text.value=scene.text;text.maxLength=160;text.disabled=planLocked();text.setAttribute('aria-label',`${index+1}번 장면 대본`);
    text.oninput=()=>{scene.text=text.value;planChanged();if(currentScene()===scene){$('captionText').value=text.value;queuePreview();}};
    const info=document.createElement('small');info.textContent=(job.storyboard.segments?.length?`영상 구간 ${scene.photos.length}개`:'사진 '+scene.photos.map(n=>n+1).join(', '))+(hasNarration()?' · 음성과 화면 자막에 같은 문장 사용':' · 2초 · 두 줄 자막 · 줄마다 18자 이하'+(index===job.storyboard.scenes.length-1?' · 둘째 줄은 댓글 남겨주세요로 저장됩니다.':''))+(job.storyboard.motion.some(m=>m.photos.some(p=>scene.photos.includes(p)))?' · 움직임 포함':'');
    body.append(text,info);if(job.storyboard.segments?.length)body.append(segmentRows(scene,index));if(hasNarration())body.append(mediaControls('voice',index));row.append(number,body);panel.append(row);
  });drawMotion();
}
function drawScriptOptions(panel){
  const options=job.storyboard.script_options||[];if(options.length<2)return;
  const box=document.createElement('div');box.className='script-options';box.setAttribute('role','radiogroup');box.setAttribute('aria-label','대본 후보');
  const heading=document.createElement('h3');heading.textContent=`대본 후보 ${options.length}개 · 하나를 고른 뒤 아래에서 고칠 수 있어요`;box.append(heading);
  options.forEach((option,index)=>{
    const chosen=index===job.storyboard.script_choice,card=document.createElement('button');card.type='button';card.className='script-option';card.setAttribute('role','radio');card.setAttribute('aria-checked',String(chosen));card.disabled=planLocked();
    const title=document.createElement('strong');title.textContent=`${index+1}. ${option.title}`+(chosen?' · 사용 중':'');const text=document.createElement('p');text.textContent=option.texts.join(' ');card.append(title,text);
    card.onclick=()=>chooseScript(index);box.append(card);
  });panel.append(box);
}
function chooseScript(index){
  const plan=job.storyboard,current=plan.script_options[plan.script_choice];
  if(index===plan.script_choice||planLocked())return;
  if(current&&plan.scenes.some((scene,i)=>scene.text!==current.texts[i])&&!window.confirm('직접 고친 대본이 선택한 후보로 바뀌어요. 계속할까요?'))return;
  plan.script_choice=index;plan.scenes.forEach((scene,i)=>{scene.text=plan.script_options[index].texts[i];});
  selectedCaptionCue=0;
  const scene=plan.scenes.find(s=>s.photos.includes(selectedPhoto));if(scene)$('captionText').value=scene.text;
  planChanged();drawScript();queuePreview();
}
function drawMotion(){
  const prompts=$('motionPrompts');prompts.replaceChildren();
  $('savePlan').hidden=!job?.storyboard;
  if(!job?.storyboard){$('motionCount').textContent='클립 0개';return;}
  job.storyboard.motion.forEach((motion,index)=>{
    const label=document.createElement('label');label.className='motion-prompt';label.textContent=`클립 ${index+1} · 사진 ${motion.photos.map(n=>n+1).join(', ')} · ${motion.seconds}초`;
    const strip=document.createElement('div');strip.className='motion-clip-photos';
    for(const photo of motion.photos){const img=document.createElement('img');img.src=`/api/jobs/${job.id}/files/${job.photos[photo].file}`;img.alt=job.photos[photo].name;img.loading='lazy';strip.append(img);}
    const input=document.createElement('textarea');input.value=motion.prompt;input.maxLength=2000;input.dataset.photo=motion.photo;input.setAttribute('aria-label',`클립 ${index+1} 움직임 프롬프트`);
    const info=document.createElement('small');
    const update=()=>{const valid=motion.prompt.trim().length>0&&motion.prompt.trim().length<=2000;input.setAttribute('aria-invalid',String(!valid));info.textContent=valid?`${motion.prompt.trim().length} / 2,000자 · 한국어·영어 입력 가능`:'움직임을 설명하는 프롬프트를 1~2,000자로 입력하세요.';info.className=valid?'muted':'input-error';};
    input.oninput=()=>{motion.prompt=input.value;update();planChanged();};update();
    label.append(strip,input,info,mediaControls('motion',motion.photo));prompts.append(label);
  });
  motionControls();
}
function motionControls(){
  const locked=planLocked(),count=job?.storyboard?.motion.length||0;
  $('motionCount').textContent=`클립 ${count}개`;
  $('motionHelp').textContent=!job?.storyboard?'사진 분석 후 사용 사진을 3~4장씩 묶은 클립과 프롬프트가 여기에 나타나요.':locked?'제출한 생성 작업이 끝난 뒤 수정할 수 있어요.':count?'사용 사진을 모두 3~4장씩 묶어 클립으로 움직입니다. 클립마다 프롬프트를 고칠 수 있어요.':'움직임 클립이 없어요. 사진 분석을 다시 하면 클립이 만들어져요.';
  for(const input of document.querySelectorAll('#motionPrompts textarea, #storyboard textarea'))input.disabled=locked;
  $('savePlan').disabled=locked||!planValid()||!numericValid();
  $('planNotice').textContent=dirtyScript?'변경 사항을 저장하거나 견적을 다시 확인해 주세요. 기존 견적은 사용할 수 없어요.':'';
}
async function savePlan(){
  if(!job||sourceBusy||busyStates.has(job.state)||(dirtyScript&&planLocked()))throw new Error('현재 작업이 끝난 뒤 변경 사항을 저장해 주세요.');
  if(dirtyScript&&!planValid())throw new Error('대본과 움직임 프롬프트를 확인해 주세요.');
  caption();await planTransaction(persistEdits);
  $('statusTitle').textContent='변경 사항을 저장했어요.';
}
async function persistPlan(){job={...await api(`/api/jobs/${job.id}/storyboard`,job.storyboard),preview_stale:true,media_ready:false};dirtyScript=false;drawScript();}
async function persistEdits(){
  const settings=options();
  if(dirtyFacts){job=await api(`/api/jobs/${job.id}/facts`,factsValue());dirtyFacts=false;}
  if(dirtyScript)await persistPlan();
  job={...await api(`/api/jobs/${job.id}/style`,settings),preview_stale:true,media_ready:false};dirtyStyle=false;
  job=await api('/api/jobs/'+job.id);
}
async function planTransaction(fn){
  if(sourceBusy)throw new Error('현재 작업이 끝난 뒤 다시 진행해 주세요.');
  sourceBusy=true;drawStatus();
  try{await fn();}finally{sourceBusy=false;drawPhotos();drawScript();drawStatus();}
}
$('savePlan').onclick=action(savePlan);
$('saveStyle').onclick=action(savePlan);
$('saveEdits').onclick=action(savePlan);
function photosLocked(){return Boolean(job&&(busyStates.has(job.state)||job.pending_media||(job.workflow_version<2&&(job.storyboard||Object.keys(job.generated).length||Object.keys(job.narration).length||job.result))));}
function drawStatus(){
  const busy=sourceBusy||(job&&busyStates.has(job.state));$('photoGrid').classList.toggle('sortable',Boolean(job)&&!planLocked());
  drawDomain();for(const input of document.querySelectorAll('#factsFields [data-fact]'))input.disabled=Boolean(busy);
  const prepareReason=prepareBlockReason();$('prepare').disabled=Boolean(prepareReason);$('prepare').title=prepareReason;
  $('enhanceHelp').textContent=prepareReason||(combinedPreparation()?'사진 고화질 변환·인물 제거와 대본 분석을 함께 진행합니다. 이미 변환한 사진은 재사용합니다.':'선택한 영상을 분석해 대본과 자막을 만듭니다.');
  $('saveStyle').disabled=!job||busy||!numericValid(); $('quote').disabled=!job?.storyboard||busy||!numericValid()||!planValid();
  $('newJob').disabled=sourceBusy; $('files').disabled=sourceBusy||photosLocked();
  $('dropZone').classList.toggle('locked',$('files').disabled); $('photoLockNote').hidden=!photosLocked();
  $('sourceNames').hidden=!job?.sources?.length;
  $('sourceNames').textContent=(job?.sources||[]).map(s=>s.name).join(' / ');
  $('status').className=job?.state==='error'?'error':busy?'busy':'';
  $('statusTitle').textContent=sourceBusy?'작업을 준비하고 있어요.':job?.message||'사진을 기다리고 있어요.';
  $('statusDetail').textContent=job?.error||(hasUnsavedEdits()?'변경 사항이 아직 저장되지 않았어요. 상단에서 한 번에 저장하세요.':`${words().noun} 선택 → 대본·자막 편집 → 견적·영상 생성 → 확인·저장`);drawPreviewStatus(busy);
  $('quoteBox').hidden=job?.state!=='awaiting_approval'||!job?.quote||dirtyScript;
  $('approve').disabled=busy||dirtyScript||!numericValid();
  if(job?.quote){$('credits').textContent=job.quote.total.toLocaleString()+' credits';$('quoteBreakdown').textContent=`움직임 ${job.quote.videos.length}장(${job.video_model} · ${job.video_parameters.resolution})`+(hasNarration()?` · Drama3 음성 ${job.quote.voices.length}장면`:' · 음성 없음');}drawModels();
  const audioReady=job?.storyboard&&(!hasNarration()||job.storyboard.scenes.every((s,i)=>job.narration[String(i)]?.file));
  $('rerender').hidden=!audioReady||busy||job.workflow_version>=2; $('resume').hidden=!job?.approval||busy||['complete','preview_ready'].includes(job.state);
  $('rerender').disabled=!numericValid();$('resume').disabled=dirtyScript||!numericValid();
  if(job?.export_files)exportCache.set(job.id,job.export_files);
  const files=job?(exportCache.get(job.id)||{}):{},saved=Object.keys(files).length>0;
  $('exportNotice').textContent=busy?'작업이 진행 중이에요. 파일이 준비되면 다운로드할 수 있어요.':job?.result?'준비된 파일을 각각 다운로드하세요.':saved?'마지막으로 만든 파일이에요. 이후 고친 내용은 미리보기를 다시 만들어야 반영돼요.':'영상 생성이 끝나면 아래 파일을 각각 다운로드할 수 있어요.';
  for(const [id,name] of [['download','stay-reel.mp4'],['videoDownload','stay-video.mp4'],['scriptDownload','stay-script.txt'],['subtitleDownload','stay-reel.srt'],['audioDownload','stay-voice.mp3']]){
    const ready=Boolean(files[name]&&!busy);
    $(id).hidden=!ready; $(id).removeAttribute('href');
    if(ready){$(id).href=`/api/jobs/${job.id}/files/${files[name]}`;$(id).download=name;}
    if($(id+'Missing')){
      $(id+'Missing').hidden=ready;
      $(id+'Missing').textContent=busy?'생성 중':!saved?'생성 후 다운로드':id==='scriptDownload'?'TXT는 다시 합성해 주세요':'다시 합성해 주세요';
    }
  }
  for(const button of document.querySelectorAll('.history-job'))button.disabled=sourceBusy;
  drawCollection();
  motionControls();
  for(const id of [...numericControls,...numericControls.map(id=>id+'Input'),'captionText','color','outline','bgOpacity','shadow','speed','motion','captionAlign',...badgeControls,...thumbControls,'thumbPick','thumbShow'])$(id).disabled=Boolean(busy);
  $('captionPreset').disabled=Boolean(busy);$('saveCaptionPreset').disabled=Boolean(busy)||!numericValid();
  $('motion').disabled=Boolean(busy)||!job;
  $('captionText').disabled=!job?.storyboard||Boolean(busy)||planLocked();
  drawWorkflow(Boolean(busy));
  if(uiError)showError(new Error(uiError));
}
async function selectJob(id){
  if(sourceBusy)return;
  if(!confirmDiscard())return;
  await planTransaction(()=>loadJob(id));
}
async function loadJob(id){
  const next=await api('/api/jobs/'+id+'?per_photo=true');
  resetPreview();job=next;dirtyScript=Boolean(next.photo_plan_migration);dirtyStyle=dirtyFacts=false;candidateSelected.clear();
  selectedPhoto=job.photo_order?.[0]??job.storyboard?.scenes[0]?.photos[0]??0;previewMode=!next.photo_plan_migration&&(job.preview||job.result)?'video':'photo';
  const first=job.storyboard?.segments?.[0];selectedSegment=first?0:null;cropDraft=null;previewTime=first?first.start:0;if(first)selectedPhoto=first.source;
  loadStyle();const scene=currentScene();if(scene)$('captionText').value=scene.text;await updatePreview();
}
async function upload(files){
  if(!files.length)return;
  if(sourceBusy||photosLocked())throw new Error(`현재 생성 작업이 끝난 뒤 ${eul(words().noun)} 추가해 주세요.`);
  const target=job?.id,selectedSettings=!target?options():null,form=new FormData();for(const file of files)form.append('files',file);if(!target)form.append('domain',chosenDomain);
  sourceBusy=true;drawStatus();
  try{if(hasUnsavedEdits())await persistEdits();job=await api(target?`/api/jobs/${target}/${videoDomain()?'videos':job.workflow_version>=2?'candidates':'photos'}`:'/api/jobs',form);if(selectedSettings){job=await api(`/api/jobs/${job.id}/style`,selectedSettings);}previewMode='photo';selectedPhoto=job.photo_order?.[0]??0;loadStyle();drawPhotos();drawScript();await updatePreview();await history();}
  finally{sourceBusy=false;$('files').value='';drawStatus();}
}
async function history(){const list=await api('/api/jobs');$('history').replaceChildren();for(const item of list.slice(0,10)){const button=document.createElement('button');button.className='history-job';const kind=domainList.find(d=>d.id===item.domain);button.textContent=`[${kind?.label||item.domain}] ${item.name?item.name+' · ':''}${kind?.ui.noun||'사진'} ${item.photos}${kind?.ui.unit||'장'} · ${item.message}`;button.disabled=sourceBusy;button.onclick=action(()=>selectJob(item.id));$('history').append(button);}}
async function connections(){const value=await api('/api/connections');connectionState=value;$('codexState').textContent=value.codex?'ChatGPT 구독 연결됨':'로그인이 필요합니다';$('fishState').textContent=value.fish?'Fish MCP 연결됨 · 움직임·Drama3 음성':'패키지 크레딧 계정 연결 필요';$('loginMessage').textContent=value.codex_login;$('connectMessage').textContent=value.message;if(value.workspaces.length){const old=$('workspace').value;$('workspace').replaceChildren();for(const item of value.workspaces){const option=document.createElement('option');option.value=item.workspace_id;option.textContent=item.workspace_name;$('workspace').append(option);}if([...$('workspace').options].some(o=>o.value===old))$('workspace').value=old;}drawWorkflow(Boolean(sourceBusy||job&&busyStates.has(job.state)));return value;}
function collectionControls(){
  const collecting=collection?.status==='in_progress',locked=sourceBusy||photosLocked();
  $('collectPhotos').disabled=locked||collecting; $('stayUrl').disabled=sourceBusy||collecting;
  const remaining=job?.workflow_version>=2?60:60-(job?.photos.length||0),selected=collectionSelected.size;
  $('collectionCount').textContent=`${selected}장 선택 · ${remaining}장까지 추가`;
  $('selectCollection').textContent=`최대 ${remaining}장 선택`;
  $('selectCollection').disabled=locked||collecting||remaining<=0;
  $('clearCollection').disabled=sourceBusy||collecting;
  $('importPhotos').disabled=locked||collecting||!selected||selected>remaining;
  $('importPhotos').textContent=selected?`선택한 ${selected}장 추가`:'선택한 사진 추가';
  $('recommend').disabled=Boolean(sourceBusy||job&&busyStates.has(job.state))||(!job?.candidates?.length&&!selected)||Boolean(job?.pending_media)||!$('targetCount').validity.valid;
}
function drawCollection(){
  $('collectionGallery').hidden=!collection?.photos.length;
  collectionControls();
  if(!collection)return;
  $('collectionName').textContent=collection.name||'수집한 숙소 사진';
  $('collectionStatus').textContent=collection.error?`${collection.message} ${collection.error}`:
    `${collection.message}${collection.status==='in_progress'?' · 수집이 끝나면 사진을 선택할 수 있어요.':''}`;
  const grid=$('collectionGrid');grid.replaceChildren();
  const existing=new Set((job?.photos||[]).map(p=>p.sha256));
  for(const photo of collection.photos){
    const already=existing.has(photo.id),disabled=!photo.usable||already||sourceBusy||photosLocked()||collection.status==='in_progress';
    if(already||!photo.usable)collectionSelected.delete(photo.id);
    const label=document.createElement('label');label.className='collection-photo';label.classList.toggle('unusable',!photo.usable||already);
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.value=photo.id;checkbox.checked=collectionSelected.has(photo.id);checkbox.disabled=disabled;
    const title=[...new Set(photo.labels.map(l=>l.title||l.room_name).filter(Boolean))].join(' · ')||'숙소 사진';
    checkbox.setAttribute('aria-label',`${title} 사진 선택`);
    checkbox.onchange=()=>{if(checkbox.checked)collectionSelected.add(photo.id);else collectionSelected.delete(photo.id);collectionControls();};
    const image=document.createElement('img');image.src=`/api/collections/${collection.id}/photos/${photo.id}`;image.alt=title;image.loading='lazy';
    const text=document.createElement('span');text.textContent=`${title} · ${photo.width}×${photo.height}${already?' · 이미 추가됨':!photo.usable?' · 사용 불가':''}`;
    if(!photo.usable)label.title=photo.reason;
    label.append(image,checkbox,text);grid.append(label);
  }
  $('collectionFailures').hidden=!collection.failures.length;
  $('collectionFailureList').replaceChildren();
  for(const failure of collection.failures){const item=document.createElement('li');item.textContent=`${failure.labels?.map(l=>l.title||l.room_name).filter(Boolean).join(' · ')||'사진'}: ${failure.error}`;$('collectionFailureList').append(item);}
  collectionControls();
}
$('collectPhotos').onclick=action(async()=>{
  const url=$('stayUrl').value.trim();if(!url)throw new Error('여기어때 국내 숙소 상세 링크를 입력해 주세요.');
  sourceBusy=true;drawStatus();$('collectionStatus').textContent='수집을 시작하고 있어요.';
  try{collection=await api('/api/collections',{url});collectionSelected.clear();try{localStorage.setItem('stayCollection',collection.id);}catch{};}
  finally{sourceBusy=false;drawStatus();}
});
$('stayUrl').addEventListener('keydown',event=>{if(event.key==='Enter'&&!$('collectPhotos').disabled){event.preventDefault();$('collectPhotos').click();}});
$('selectCollection').onclick=()=>{
  const existing=new Set((job?.photos||[]).map(p=>p.sha256));
  collectionSelected=new Set(collection.photos.filter(p=>p.usable&&!existing.has(p.id)).slice(0,60-(job?.photos.length||0)).map(p=>p.id));drawCollection();
};
$('clearCollection').onclick=()=>{collectionSelected.clear();drawCollection();};
$('importPhotos').onclick=action(async()=>{
  if(!confirmDiscard())return;
  sourceBusy=true;drawStatus();
  try{job=await api('/api/composition',{collection_id:collection.id,photos:[...collectionSelected],job_id:job?.id,mode:'manual'});dirtyScript=dirtyStyle=dirtyFacts=false;previewMode='photo';collectionSelected.clear();selectedPhoto=0;loadStyle();drawPhotos();drawScript();await updatePreview();await history();}
  finally{sourceBusy=false;drawStatus();}
});
async function restoreCollection(){
  let id;try{id=localStorage.getItem('stayCollection');}catch{return;}
  if(!id)return;
  try{collection=await api('/api/collections/'+id);$('stayUrl').value=collection.source_url;drawCollection();}
  catch{try{localStorage.removeItem('stayCollection');}catch{}}
}
$('files').onchange=action(()=>upload([...$('files').files]));for(const event of ['dragenter','dragover'])$('dropZone').addEventListener(event,e=>{e.preventDefault();if(!$('files').disabled)$('dropZone').classList.add('dragover');});$('dropZone').addEventListener('dragleave',()=>$('dropZone').classList.remove('dragover'));$('dropZone').addEventListener('drop',e=>{e.preventDefault();$('dropZone').classList.remove('dragover');upload([...e.dataTransfer.files]).catch(showError);});
for(const id of ['color','outline','bgOpacity','shadow','captionText','captionAlign',...badgeControls])$(id).addEventListener('input',queuePreview);
for(const id of numericControls){
  $(id).addEventListener('input',()=>{$(id+'Input').value=$(id).value;$(id+'Error').hidden=true;$(id+'Input').removeAttribute('aria-invalid');queuePreview();drawStatus();});
  $(id+'Input').addEventListener('input',()=>{const valid=$(id+'Input').validity.valid;$(id+'Error').hidden=valid;$(id+'Input').setAttribute('aria-invalid',String(!valid));if(valid){$(id).value=$(id+'Input').value;queuePreview();}else{++previewSequence;clearTimeout(previewTimer);}drawStatus();});
}
$('newJob').onclick=()=>{
  if(!confirmDiscard())return;
  resetPreview();job=null;dirtyScript=dirtyStyle=dirtyFacts=false;uiError=null;candidateSelected.clear();
  for(const id of [...numericControls,...numericControls.map(id=>id+'Input'),'color','outline','bgOpacity','shadow','captionText'])$(id).value=$(id).defaultValue;
  $('speed').value='1';for(const id of numericControls){$(id+'Error').hidden=true;$(id+'Input').removeAttribute('aria-invalid');}
  $('files').value='';motionOptions();updateOutputs();drawPhotos();drawScript();drawStatus();
  // Show the last saved caption style right away; the next upload starts from it.
  api('/api/style').then(saved=>{savedDefaultStyle=saved;if(!job&&chosenDomain!=='stay-silent'&&!$('captionPreset').value)loadStyle(saved);}).catch(()=>{});
};
$('prepare').onclick=action(async()=>{await planTransaction(async()=>{await persistEdits();await api(`/api/jobs/${job.id}/${combinedPreparation()?'prepare':'analyze'}`,options());job=await api('/api/jobs/'+job.id);});});
$('quote').onclick=action(async()=>{const workspace=$('workspace').value;if(!planValid())throw new Error('움직임 프롬프트와 대본을 확인해 주세요.');if(!workspace){$('settingsButton').click();throw new Error('크레딧 견적을 확인하려면 계정을 연결하고 사용할 계정을 선택해 주세요.');}await planTransaction(async()=>{await persistEdits();await api(`/api/jobs/${job.id}/quote`,{workspace});job=await api('/api/jobs/'+job.id);drawScript();});});
$('approve').onclick=action(async()=>{if(dirtyScript||!job.quote)throw new Error('대본·움직임이 바뀌었습니다. 견적을 다시 확인해 주세요.');const approval={quote_id:job.quote.id,expected_credits:job.quote.total};await planTransaction(async()=>{await persistEdits();previewMode='video';await api(`/api/jobs/${job.id}/approve`,approval);job=await api('/api/jobs/'+job.id);drawScript();});});
$('rerender').onclick=action(async()=>{await planTransaction(async()=>{await persistEdits();previewMode='video';await api(`/api/jobs/${job.id}/render`,{});job=await api('/api/jobs/'+job.id);});});
$('resume').onclick=action(async()=>{await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();previewMode='video';await api(`/api/jobs/${job.id}/resume`,{});job=await api('/api/jobs/'+job.id);});});

const modelTraits={fast:'빠름','low-cost':'저가',drafting:'시안용','realistic-motion':'사실적 움직임','scene-consistency':'장면 일관성','cinematic-motion':'영화 같은 움직임','reference-aware':'참조 지원','general-purpose':'범용',flexible:'유연함',multimodal:'멀티모달','start-end-frames':'시작·끝 프레임'};
function qualityLabel(row){return row.native?`${row.quality} 원본 화질`:`${row.quality} → 1080p 확대`;}
function drawModels(){
  const resolution=job?.video_parameters?.resolution;
  $('modelSummary').textContent=job?`영상 모델 · ${job.video_model} · ${resolution}${resolution&&resolution.toLowerCase()!=='1080p'?' (1080p로 확대)':''}`:'영상 모델';
  $('compareModels').disabled=!job?.photos?.length||planLocked();
  const list=$('modelList');list.replaceChildren();
  if(!modelComparison||modelComparison.job!==job?.id)return;
  const count=modelComparison.motion_count;
  const rows=[...modelComparison.models].sort((a,b)=>(a.credits??Infinity)-(b.credits??Infinity));
  for(const row of rows){
    const selected=row.model===job.video_model&&JSON.stringify(row.parameters)===JSON.stringify(job.video_parameters);
    const label=document.createElement('label');label.className='model-row'+(selected?' selected':'')+(row.credits==null?' unavailable':'');
    const input=document.createElement('input');input.type='radio';input.name='videoModel';input.checked=selected;input.disabled=row.credits==null||planLocked();
    input.onchange=action(async()=>{await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();job=await api(`/api/jobs/${job.id}/video-model`,{model:row.model,parameters:row.parameters});await loadJob(job.id);});});
    const info=document.createElement('span');const name=document.createElement('strong');name.textContent=row.model;
    const detail=document.createElement('small');detail.textContent=[qualityLabel(row),row.parameters.duration?`${row.parameters.duration} 클립`:'',...row.traits.map(t=>modelTraits[t]||t)].filter(Boolean).join(' · ');
    info.append(name,detail);if(row.description){const about=document.createElement('small');about.textContent=row.description;info.append(about);}
    const credits=document.createElement('span');credits.className='model-credits';
    if(row.credits==null)credits.textContent='견적 불가';
    else{credits.textContent=`${row.credits.toLocaleString()} 크레딧/장`;const total=document.createElement('small');total.textContent=count?`움직임 ${count}장 ≈ ${(row.credits*count).toLocaleString()}`:'움직임 장면 1장 기준';if(!row.can_generate)total.textContent+=' · 잔액 부족';credits.append(total);}
    label.title=row.error||'';label.append(input,info,credits);list.append(label);
  }
}
$('compareModels').onclick=action(async()=>{
  const workspace=$('workspace').value;
  if(!workspace){$('settingsButton').click();throw new Error('모델별 크레딧을 비교하려면 계정을 연결하고 사용할 계정을 선택해 주세요.');}
  const id=job.id;$('compareModels').disabled=true;$('compareModels').textContent='모델별 무료 견적 조회 중…';
  try{const value=await api(`/api/jobs/${id}/video-models`,{workspace});if(job?.id===id)modelComparison={...value,job:id};}
  finally{$('compareModels').textContent='모델별 화질·크레딧 다시 비교';drawModels();}
});
$('settingsButton').onclick=()=>{$('settings').showModal();connections().catch(showError);};$('closeSettings').onclick=()=>$('settings').close();for(const button of document.querySelectorAll('[data-connect]'))button.onclick=action(async()=>{await api('/api/connect/'+button.dataset.connect,{});await connections();});
$('recoverVoice').onclick=action(async()=>{if(!job)throw new Error('작업을 먼저 선택해 주세요.');const file=$('recoverFile').files[0];if(!file)throw new Error('음성 파일을 선택해 주세요.');const data=new FormData();data.append('file',file);await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();await api(`/api/jobs/${job.id}/recover-voice/${Number($('recoverScene').value)-1}`,data);await loadJob(job.id);});$('connectMessage').textContent='음성을 복구했습니다.';});
setInterval(async()=>{
  if(polling)return;polling=true;
  try{
    if(job&&busyStates.has(job.state)){
      const id=job.id,previous=job.state,next=await api('/api/jobs/'+id+'?per_photo=true');
      if(next.photo_plan_migration)dirtyScript=true;
      if(job?.id===id){const before=JSON.stringify(job.photos);job=next;drawStatus();if(previous===job.state&&before!==JSON.stringify(job.photos)){drawPhotos();if(previewMode==='photo')await updatePreview();}if(previous!==job.state){drawScript();drawPhotos();if(job.state==='uploaded'&&job.storyboard){loadStyle();$('captionText').value=job.storyboard.scenes[0].text;await updatePreview();}if(['complete','preview_ready'].includes(job.state))await selectJob(job.id);await history();}}
    }
    if(collection?.status==='in_progress'){
      const id=collection.id,next=await api('/api/collections/'+id);if(collection?.id===id){collection=next;drawCollection();}
    }
    if($('settings').open)await connections();
  }catch(error){showError(error);}finally{polling=false;}
},2000);
function compositionMode(){return document.querySelector('input[name=compositionMode]:checked').value;}
async function mediaUrl(key){const bytes=new TextEncoder().encode(JSON.stringify(key));const hash=await crypto.subtle.digest('SHA-256',bytes);return `/api/jobs/${job.id}/media/${[...new Uint8Array(hash)].map(b=>b.toString(16).padStart(2,'0')).join('')}`;}
function showVideo(){
  const asset=job?.preview||job?.result;if(!asset)return;
  previewMode='video';++previewSequence;
  const url=`/api/jobs/${job.id}/files/${asset.file}`;
  if($('video').getAttribute('src')!==url)$('video').src=url;
  $('preview').hidden=true;$('previewEmpty').hidden=true;$('video').hidden=false;
}
function drawPreviewStatus(busy){
  const stale=Boolean(job?.preview&&(job.preview_stale||!['preview_ready','complete'].includes(job.state)));
  const latest=job?.preview&&!stale&&!dirtyScript&&!dirtyStyle;
  const mediaReady=job?.media_ready??(job?.storyboard&&(!hasNarration()||job.storyboard.scenes.every((s,i)=>job.narration[String(i)]?.file))&&job.storyboard.motion.every(m=>job.generated[String(m.photo)]?.file));
  $('previewVideo').disabled=!mediaReady||busy||!numericValid();
  $('exportFinal').disabled=!latest||busy;
  $('photoPreview').setAttribute('aria-pressed',String(previewMode==='photo'));
  $('photoPreview').disabled=!job?.photos.length;
  $('videoPreview').setAttribute('aria-pressed',String(previewMode==='video'));
  $('videoPreview').disabled=!(job?.preview||job?.result);
  captionCueControls.hidden=previewMode!=='photo'||captionCueCount<2||!job;
  $('previewNote').textContent=previewMode==='photo'?(job?.photos.length?`사진별 화면 자막 ${selectedCaptionCue+1}/${Math.max(captionCueCount,1)} · 추가 비용 없음 · ${words().nav_hint}`:`${eul(words().noun)} 선택하면 자막과 크롭을 바로 확인할 수 있어요.`):
    stale||dirtyScript||dirtyStyle?'변경 전 영상입니다. 변경 내용을 영상에 반영한 뒤 내보내세요.':job?.preview?`1080×1920 · 30fps · ${job.preview.duration.toFixed(1)}초 · ${hasNarration()?'움직임·음성·자막 포함':'움직임·자막 포함 · 음성 없음'}`:'저장된 완성 영상입니다. 사진을 선택하면 편집 미리보기로 전환됩니다.';
  $('paceWarning').textContent=(job?.preview?.warnings||[]).join(' ');
  $('recommend').disabled=busy||(!job?.candidates?.length&&!collectionSelected.size)||Boolean(job?.pending_media)||!$('targetCount').validity.valid;
  $('versionsPanel').hidden=!job?.versions?.length;$('versionsList').replaceChildren();
  for(const [i,v] of (job?.versions||[]).entries()){const button=document.createElement('button');button.className='quiet';button.textContent=`구성 ${i+1} · 사진 ${v.photos.length}장 복원`;button.disabled=busy;button.onclick=action(async()=>{if(!confirmDiscard())return;await planTransaction(async()=>{job=await api(`/api/jobs/${job.id}/versions/${i}`,{});dirtyScript=dirtyStyle=dirtyFacts=false;await loadJob(job.id);});});$('versionsList').append(button);}
  if(previewMode==='video')showVideo();
  drawPhotoNav();
}
function drawCandidates(){
  const pool=job?.candidates||[];$('candidatePanel').hidden=!pool.length;$('candidateCount').textContent=`후보 사진 ${pool.length}장 · 현재 영상 ${job?.photos.length||0}장`;
  $('candidateGrid').replaceChildren();
  for(const photo of pool){const label=document.createElement('label');label.className='collection-photo';const input=document.createElement('input');input.type='checkbox';input.checked=candidateSelected.has(photo.id);input.disabled=planLocked();input.onchange=()=>{if(input.checked)candidateSelected.add(photo.id);else candidateSelected.delete(photo.id);drawCandidates();};input.setAttribute('aria-label',`${photo.name||'후보'} 사진 선택`);const img=document.createElement('img');img.loading='lazy';img.alt=photo.name||'후보 사진';img.src=photo.collection_id?`/api/collections/${photo.collection_id}/photos/${photo.id}`:`/api/jobs/${job.id}/files/${photo.file}`;const text=document.createElement('span');text.textContent=candidateSelected.has(photo.id)?`선택 순서 ${[...candidateSelected].indexOf(photo.id)+1}`:photo.name||'후보 사진';label.append(img,input,text);$('candidateGrid').append(label);}
  $('applyCandidates').disabled=planLocked()||!candidateSelected.size||candidateSelected.size>60||(job?.domain==='stay-silent'&&(candidateSelected.size<8||candidateSelected.size>10));
  $('removeCandidates').disabled=planLocked()||!candidateSelected.size;
}
async function movePhoto(from,to){
  if(planLocked())return;
  await planTransaction(async()=>{
    if(hasUnsavedEdits())await persistEdits();
    const order=job.photo_order?.length?[...job.photo_order]:job.photos.map((_,i)=>i);
    const [photo]=order.splice(from,1);order.splice(to,0,photo);
    job=await api('/api/composition',{job_id:job.id,mode:'manual',order});await loadJob(job.id);
  });
}
function mediaControls(kind,index){
  const box=document.createElement('div');box.className='media-controls';if(job?.workflow_version<2)return box;
  const retry=document.createElement('button');retry.type='button';retry.className='quiet';retry.textContent=kind==='voice'?'이 음성 다시 생성':'이 움직임 다시 생성';retry.disabled=planLocked();retry.onclick=action(async()=>{await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();job=await api(`/api/jobs/${job.id}/media-attempt`,{kind,index});await loadJob(job.id);});$('statusDetail').textContent='새 결과를 생성하려면 변경분 견적을 확인하고 크레딧 사용을 승인하세요.';});box.append(retry);
  const active=(kind==='voice'?job.narration:job.generated)[String(index)];
  const matching=active?.asset_key?active.asset_key.slice(0,active.asset_key.lastIndexOf(':')):Object.keys(job.assets||{}).find(k=>{const r=job.assets[k];return kind==='voice'?r.text===job.storyboard.scenes[index].text:r.photo_sha===job.photos[index].sha256&&r.prompt===job.storyboard.motion.find(m=>m.photo===index)?.prompt;})?.replace(/:\d+$/,'');
  if(matching){
    const select=document.createElement('select');select.setAttribute('aria-label','이전 생성 결과 선택');
    for(const [key,record] of Object.entries(job.assets)){if(key.startsWith(matching+':')&&record.file){const option=document.createElement('option');option.value=key.split(':').at(-1);option.textContent=`생성 결과 ${Number(option.value)+1}`;select.append(option);}}
    if(select.options.length){
      const restore=document.createElement('button');restore.type='button';restore.className='quiet';restore.textContent='결과 복원';restore.disabled=planLocked();restore.onclick=action(async()=>{await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();job=await api(`/api/jobs/${job.id}/media-attempt`,{kind,index,restore:Number(select.value)});await loadJob(job.id);});});
      const player=document.createElement(kind==='voice'?'audio':'video');player.controls=true;player.preload='none';player.className='asset-player';
      const update=()=>mediaUrl(matching+':'+select.value).then(url=>{player.src=url;}).catch(showError);select.onchange=update;update();box.append(select,restore,player);
    }
  }
  return box;
}
for(const radio of document.querySelectorAll('input[name=compositionMode]'))radio.onchange=()=>{$('aiControls').hidden=compositionMode()!=='ai';$('compositionHelp').textContent=compositionMode()==='ai'?(domainInfo()?.ui.ai_help||''):'선택한 순서를 그대로 사용합니다. 사진을 끌거나 이동 버튼으로 순서를 바꾸세요.';drawStatus();};
$('targetCount').oninput=drawStatus;
for(const id of [...numericControls,...numericControls.map(id=>id+'Input'),'color','outline','bgOpacity','shadow','speed','captionAlign',...badgeControls])$(id).addEventListener('input',()=>{if(job)dirtyStyle=true;drawStatus();});
for(const id of thumbControls)$(id).addEventListener('input',()=>{if(!job)return;dirtyStyle=true;drawStatus();clearTimeout(thumbTimer);thumbTimer=setTimeout(()=>showThumbnail().catch(showError),250);});
$('thumbPick').onclick=action(async()=>{if(!selectedSource())throw new Error('왼쪽에서 영상을 고르고, 자막 가림 칸의 시점 막대로 원하는 화면을 찾은 뒤 눌러 주세요.');thumbFrame={source:selectedPhoto,time:previewTime};drawThumbFrame();dirtyStyle=true;drawStatus();await showThumbnail();});
$('thumbShow').onclick=action(showThumbnail);
$('captionText').addEventListener('input',()=>{const scene=currentScene();if(scene&&!planLocked()&&$('captionText').value.trim()){scene.text=$('captionText').value;planChanged();drawScript();}});
$('recommend').onclick=action(async()=>{if(!$('targetCount').validity.valid)throw new Error('추천 장수는 1~60장입니다.');await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();job=await api('/api/composition',{job_id:job?.id,collection_id:collection?.id,photos:[...collectionSelected],mode:'ai',target_count:Number($('targetCount').value)});collectionSelected.clear();drawCollection();job=await api('/api/jobs/'+job.id);});});
$('removeCandidates').onclick=action(async()=>{const active=new Set((job?.photos||[]).map(p=>p.sha256)),inUse=[...candidateSelected].filter(id=>active.has(id)).length;if(!window.confirm(`선택한 ${candidateSelected.size}장을 후보에서 제거할까요? AI 추천과 구성 선택에 더 이상 사용하지 않습니다.`+(inUse?`\n현재 영상에 쓰인 ${inUse}장도 영상 구성에서 빠집니다.`+(job.storyboard?' 대본은 다시 만들어야 합니다.':''):'')))return;await planTransaction(async()=>{if(hasUnsavedEdits())await persistEdits();job=await api(`/api/jobs/${job.id}/candidates/remove`,{ids:[...candidateSelected]});candidateSelected.clear();await loadJob(job.id);});});
$('applyCandidates').onclick=action(async()=>{if(!confirmDiscard())return;await planTransaction(async()=>{job=await api('/api/composition',{job_id:job.id,mode:'manual',photos:[...candidateSelected]});dirtyScript=dirtyStyle=dirtyFacts=false;candidateSelected.clear();await loadJob(job.id);});});
$('previewVideo').onclick=action(async()=>{await planTransaction(async()=>{await persistEdits();previewMode='video';await api(`/api/jobs/${job.id}/preview-video`,{});job=await api('/api/jobs/'+job.id);});});
$('exportFinal').onclick=action(async()=>{await planTransaction(async()=>{await api(`/api/jobs/${job.id}/render`,{});job=await api('/api/jobs/'+job.id);});});
function resetPreview(){
  selectedCaptionCue=0;captionCueCount=0;captionCueControls.replaceChildren();captionCueControls.hidden=true;
  ++previewSequence;clearTimeout(previewTimer);previewMode='photo';
  $('video').pause();$('video').removeAttribute('src');$('video').load();$('video').hidden=true;
  $('preview').hidden=true;$('preview').removeAttribute('src');$('previewEmpty').hidden=false;
  if(previewUrl){URL.revokeObjectURL(previewUrl);previewUrl=null;}
}
function revealControl(id){
  const element=$(id);if(!element)return;
  for(let parent=element;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
  element.scrollIntoView({block:'center'});element.focus({preventScroll:true});
}
function drawWorkflow(busy){
  const dirty=hasUnsavedEdits();
  $('saveStyle').hidden=!dirty;$('savePlan').hidden=!dirty||!job?.storyboard;
  $('saveEdits').hidden=!dirty;$('saveEdits').disabled=busy||!numericValid()||(dirtyScript&&(!planValid()||planLocked()));
  $('saveStyle').disabled=!job||busy||!numericValid()||(dirtyScript&&(!planValid()||planLocked()));
  const step=!job?.photos.length?0:!job.storyboard||dirty?1:['preview_ready','complete'].includes(job.state)?3:2;
  for(const [index,id] of ['stepPhotos','stepStory','stepGenerate','stepExport'].entries()){
    $(id).classList.toggle('done',index<step);if(index===step)$(id).setAttribute('aria-current','step');else $(id).removeAttribute('aria-current');
  }
  let target,label,activate=true;
  if(!job){target='files';label='사진 추가하기 +';}
  else if(!job.storyboard&&connectionState&&!connectionState.codex){target='settingsButton';label='대본 생성 계정 연결';}
  else if(!job.storyboard){const pick=compositionMode()==='ai'&&!Object.keys(job.selection_reasons||{}).length;target=pick?'recommend':'prepare';label=pick?'AI 추천 구성 만들기':combinedPreparation()?'고화질 + 대본 함께 만들기 →':'대본 만들기 →';}
  else if(!numericValid()||!planValid()){target=!numericValid()?numericControls.find(id=>!$(id+'Input').validity.valid)+'Input':job.storyboard.scenes.some(s=>!s.text.trim())?'storyboard':'motionEditor';label='편집 내용 확인하기';activate=false;}
  else if(dirty){target='saveEdits';label='변경 사항 저장';}
  else if(job.state==='awaiting_approval'&&job.quote){target='quoteBox';label='견적 확인하고 승인하기';activate=false;}
  else if(!$('resume').hidden){target='resume';label='중단된 작업 이어가기';}
  else if(!$('exportFinal').disabled){target=previewMode==='video'?'exportFinal':'videoPreview';label=previewMode==='video'?'확인한 영상 내보내기 ↓':'생성된 영상 확인하기';}
  else if(!$('previewVideo').disabled){target='previewVideo';label='변경 내용을 영상에 반영';}
  else if(connectionState&&(!connectionState.codex||!connectionState.fish||!$('workspace').value)){target='settingsButton';label='영상 생성 계정 연결';}
  else{target='quote';label='크레딧 견적 확인 →';}
  $('nextAction').textContent=busy?'작업 진행 중…':label;
  $('nextAction').disabled=busy||Boolean(activate&&$(target)?.disabled);
  $('nextAction').onclick=()=>{if(activate){$(target).click();if(target==='videoPreview')revealControl('photoPreview');}else{revealControl(target);if(target==='quoteBox')$('approve').focus({preventScroll:true});}};
  $('saveEdits').hidden=!dirty||target==='saveEdits';
  $('status').setAttribute('aria-busy',String(busy));
}
$('workspace').onchange=()=>drawWorkflow(Boolean(sourceBusy||job&&busyStates.has(job.state)));
$('photoPreview').onclick=action(async()=>{previewMode='photo';$('video').pause();await updatePreview();drawStatus();});
$('videoPreview').onclick=()=>{showVideo();drawStatus();};
$('prevPhoto').onclick=action(()=>stepPhoto(-1));$('nextPhoto').onclick=action(()=>stepPhoto(1));
document.addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight'].includes(event.key)||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey||$('photoNav').hidden||document.querySelector('dialog[open]'))return;
  if(event.target.closest?.('input,textarea,select,video,[contenteditable]'))return;
  event.preventDefault();action(()=>stepPhoto(event.key==='ArrowLeft'?-1:1))();
});
new ResizeObserver(entries=>{document.documentElement.style.setProperty('--toolbar-offset',`${Math.ceil(entries[0].target.getBoundingClientRect().height)+24}px`);}).observe(document.querySelector('.workflow-toolbar'));
drawStatus();restoreCollection();Promise.all([api('/api/domains'),api('/api/style')]).then(([list,saved])=>{domainList=list;savedDefaultStyle=saved;domainKey='';drawStatus();return history();}).catch(showError);api('/api/caption-presets').then(items=>{captionPresets=items;drawCaptionPresets();}).catch(showError);(async()=>{let value=await connections();for(let i=0;i<20&&value.fish_busy&&!value.fish;i++){await new Promise(r=>setTimeout(r,1000));value=await connections();}return value;})().then(value=>{connectionState=value;drawWorkflow(Boolean(sourceBusy||job&&busyStates.has(job.state)));if(!value.codex||!value.fish)$('settings').showModal();}).catch(showError);
