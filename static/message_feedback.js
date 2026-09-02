// Immutable feedback for settled assistant answers. Likes start a Hermes job
// that summarizes the trusted server-side transcript and uploads it to DataCop.

const _MESSAGE_FEEDBACK_ACTIVE_STATUSES=new Set(['queued','summarizing','uploading']);
const _messageFeedbackPollTimers=new Map();

function _messageFeedbackValue(value){
  return value==='like'||value==='dislike'?value:null;
}

function _messageFeedbackPartText(part){
  if(part==null) return '';
  if(typeof part==='string') return part;
  if(typeof part!=='object') return String(part);
  return String(part.text||part.content||part.input_text||part.output_text||'');
}

function _messageFeedbackPartIsToolUse(part){
  if(!part||typeof part!=='object') return false;
  const type=String(part.type||'').toLowerCase();
  if(type==='tool_use'||type==='tool_call') return true;
  if(type||_messageFeedbackPartText(part).trim()) return false;
  return ['tool_use_id','tool_call_id','call_id'].some(key=>key in part)
    &&['name','tool_name','input','args'].some(key=>key in part);
}

function _messageFeedbackHasFinalVisibleText(message){
  const content=message&&message.content;
  if(Array.isArray(content)){
    let lastToolIndex=-1;
    content.forEach((part,index)=>{
      if(_messageFeedbackPartIsToolUse(part)) lastToolIndex=index;
    });
    if(lastToolIndex>=0){
      return content.slice(lastToolIndex+1).some(part=>_messageFeedbackPartText(part).trim());
    }
    if(Array.isArray(message.tool_calls)&&message.tool_calls.length) return false;
    return content.some(part=>_messageFeedbackPartText(part).trim());
  }
  if(Array.isArray(message&&message.tool_calls)&&message.tool_calls.length) return false;
  return !!String(content||'').trim();
}

function messageFeedbackEligible(message, context){
  return !!(
    message
    &&message.role==='assistant'
    &&!message._live
    &&!message._error
    &&!message._statusCard
    &&context
    &&context.isTurnFinalAssistant
    &&!context.isProcessWakeup
    &&_messageFeedbackHasFinalVisibleText(message)
    &&String(context.displayContent||'').trim()
  );
}

function _messageFeedbackStatusText(message){
  const status=String(message&&message._feedback_status||'');
  if(status==='legacy') return t('feedback_legacy');
  if(status==='received') return t('feedback_received');
  if(status==='queued') return t('feedback_queued');
  if(status==='summarizing') return t('feedback_summarizing');
  if(status==='uploading') return t('feedback_uploading');
  if(status==='succeeded'){
    const problemId=message&&message._feedback_datacop_problem_id;
    if(!Number.isSafeInteger(problemId)||problemId<=0) return t('feedback_invalid_state');
    return t('feedback_datacop_success').replace('{id}',String(problemId));
  }
  if(status==='failed'){
    const error=String(message&&message._feedback_error||'').trim();
    return `${t('feedback_datacop_failed')}: ${error||t('feedback_unknown_error')}`;
  }
  return '';
}

function messageFeedbackButtonsHtml(message, eligible){
  if(!eligible) return '';
  const current=_messageFeedbackValue(message&&message._feedback);
  const status=String(message&&message._feedback_status||'');
  const locked=!!current&&status!=='legacy';
  const button=(feedback,icon,labelKey)=>{
    const selected=current===feedback;
    return `<button class="msg-action-btn msg-feedback-btn" type="button" data-feedback="${feedback}" aria-pressed="${selected?'true':'false'}" title="${esc(t(labelKey))}" aria-label="${esc(t(labelKey))}"${locked?' disabled':''} onclick="toggleMessageFeedback(this,'${feedback}')">${li(icon,13)}</button>`;
  };
  const statusText=_messageFeedbackStatusText(message);
  const statusClass=status==='failed'?' msg-feedback-status-error':'';
  const statusHtml=statusText
    ? `<span class="msg-feedback-status${statusClass}" title="${esc(statusText)}" aria-live="polite">${esc(statusText)}</span>`
    : '';
  return button('like','thumbs-up','feedback_like')
    +button('dislike','thumbs-down','feedback_dislike')
    +statusHtml;
}

function _applyMessageFeedbackPayload(message,payload,expected){
  if(!payload||payload.ok!==true||payload.session_id!==expected.sessionId){
    throw new Error('message feedback response did not match the active session');
  }
  if(payload.feedback!=='like'&&payload.feedback!=='dislike'){
    throw new Error('message feedback response contains an invalid feedback value');
  }
  if(expected.feedback&&payload.feedback!==expected.feedback){
    throw new Error('message feedback response did not match the requested feedback');
  }
  if(!/^[0-9a-f]{64}$/.test(String(payload.message_ref||''))){
    throw new Error('message feedback response contains an invalid message reference');
  }
  if(expected.messageRef&&payload.message_ref!==expected.messageRef){
    throw new Error('message feedback response did not match the requested message');
  }
  if(expected.jobId&&payload.job_id!==expected.jobId){
    throw new Error('message feedback response did not match the requested job');
  }
  const validStatuses=payload.feedback==='like'
    ? ['queued','summarizing','uploading','succeeded','failed']
    : ['received'];
  if(!validStatuses.includes(payload.status)){
    throw new Error('message feedback response contains an invalid status');
  }
  if(payload.feedback==='like'&&typeof payload.job_id!=='string'){
    throw new Error('message feedback response is missing a job id');
  }
  if(payload.status==='succeeded'
    &&(!Number.isSafeInteger(payload.datacop_problem_id)||payload.datacop_problem_id<=0)){
    throw new Error('message feedback response is missing a DataCop problem id');
  }
  if(payload.status==='failed'
    &&(typeof payload.error!=='string'||!payload.error.trim())){
    throw new Error('message feedback response is missing an error');
  }
  message._feedback=payload.feedback;
  message._feedback_status=payload.status;
  message._feedback_job_id=payload.job_id||null;
  message._feedback_message_ref=payload.message_ref;
  message._feedback_datacop_problem_id=payload.datacop_problem_id??null;
  message._feedback_error=payload.error??null;
}

function _scheduleMessageFeedbackPoll(sessionId,messageIndex,anchorRef,messageRef,jobId,delay=1500){
  const key=`${sessionId}:${jobId}`;
  if(_messageFeedbackPollTimers.has(key)) return;
  const timer=setTimeout(()=>{
    _messageFeedbackPollTimers.delete(key);
    pollMessageFeedbackJob(sessionId,messageIndex,anchorRef,messageRef,jobId);
  },delay);
  _messageFeedbackPollTimers.set(key,timer);
}

async function pollMessageFeedbackJob(sessionId,messageIndex,anchorRef,messageRef,jobId){
  if(!S.session||S.session.session_id!==sessionId||!Array.isArray(S.messages)) return;
  const message=S.messages[messageIndex];
  const anchors=window.HermesAssistantTurnAnchors;
  if(!message||!anchors||anchors.assistantTurnMessageRef(message)!==anchorRef) return;
  try{
    const payload=await api(`/api/message-feedback/jobs/${encodeURIComponent(jobId)}`);
    if(!S.session||S.session.session_id!==sessionId) return;
    const current=S.messages[messageIndex];
    if(!current||anchors.assistantTurnMessageRef(current)!==anchorRef) return;
    _applyMessageFeedbackPayload(current,payload,{sessionId,messageRef,jobId,feedback:'like'});
    renderMessages({preserveScroll:true});
    if(_MESSAGE_FEEDBACK_ACTIVE_STATUSES.has(current._feedback_status)){
      _scheduleMessageFeedbackPoll(sessionId,messageIndex,anchorRef,messageRef,jobId);
    }
  }catch(error){
    if(typeof showToast==='function') showToast(`${t('feedback_status_failed')}: ${error.message}`,3600,'error');
  }
}

function resumeMessageFeedbackJobs(){
  if(!S.session||!Array.isArray(S.messages)) return;
  const anchors=window.HermesAssistantTurnAnchors;
  if(!anchors||typeof anchors.assistantTurnMessageRef!=='function') return;
  const sessionId=String(S.session.session_id||'');
  S.messages.forEach((message,messageIndex)=>{
    if(message&&message._feedback==='like'
      &&_MESSAGE_FEEDBACK_ACTIVE_STATUSES.has(message._feedback_status)
      &&typeof message._feedback_job_id==='string'
      &&/^[0-9a-f]{64}$/.test(String(message._feedback_message_ref||''))){
      const anchorRef=anchors.assistantTurnMessageRef(message);
      if(anchorRef){
        _scheduleMessageFeedbackPoll(
          sessionId,messageIndex,anchorRef,message._feedback_message_ref,message._feedback_job_id
        );
      }
    }
  });
}

async function toggleMessageFeedback(button,requestedFeedback){
  if(requestedFeedback!=='like'&&requestedFeedback!=='dislike'){
    throw new TypeError('requested feedback must be like or dislike');
  }
  const row=button&&button.closest?button.closest('[data-msg-idx]'):null;
  const rawIdx=row?Number(row.dataset.msgIdx):NaN;
  if(!Number.isSafeInteger(rawIdx)||rawIdx<0) return;
  if(!S.session||!Array.isArray(S.messages)) return;
  const message=S.messages[rawIdx];
  if(!message||message.role!=='assistant'||message._live) return;
  if(_messageFeedbackValue(message._feedback)&&message._feedback_status!=='legacy') return;
  const anchors=window.HermesAssistantTurnAnchors;
  if(!anchors||typeof anchors.assistantTurnMessageRef!=='function'){
    throw new Error('assistant message reference service is unavailable');
  }
  const sessionId=String(S.session.session_id||'');
  if(!sessionId) throw new Error('active session has no session_id');
  const anchorRef=anchors.assistantTurnMessageRef(message);
  if(!anchorRef) throw new Error('assistant message has no stable reference');
  const controls=row.querySelectorAll('.msg-feedback-btn');
  controls.forEach(control=>{
    control.disabled=true;
    control.setAttribute('aria-busy','true');
  });
  let accepted=false;
  try{
    const payload=await api('/api/message-feedback',{
      method:'POST',
      body:JSON.stringify({
        session_id:sessionId,
        message_ref:anchorRef,
        feedback:requestedFeedback,
      }),
    });
    if(!S.session||S.session.session_id!==sessionId) return;
    const current=S.messages[rawIdx];
    if(!current||anchors.assistantTurnMessageRef(current)!==anchorRef) return;
    _applyMessageFeedbackPayload(current,payload,{sessionId,feedback:requestedFeedback});
    accepted=true;
    renderMessages({preserveScroll:true});
    if(current._feedback==='like'&&_MESSAGE_FEEDBACK_ACTIVE_STATUSES.has(current._feedback_status)){
      _scheduleMessageFeedbackPoll(
        sessionId,rawIdx,anchorRef,current._feedback_message_ref,current._feedback_job_id,0
      );
    }
  }catch(error){
    if(typeof showToast==='function') showToast(`${t('feedback_save_failed')}: ${error.message}`,3600,'error');
  }finally{
    controls.forEach(control=>{
      control.disabled=accepted;
      control.removeAttribute('aria-busy');
    });
  }
}

if(typeof window!=='undefined'){
  window.resumeMessageFeedbackJobs=resumeMessageFeedbackJobs;
  window.__messageFeedbackTest=Object.freeze({
    messageFeedbackEligible,
    messageFeedbackButtonsHtml,
    toggleMessageFeedback,
    pollMessageFeedbackJob,
    resumeMessageFeedbackJobs,
  });
}
